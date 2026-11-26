"""Rule proposals: ask the authors, apply only when everyone asked agreed."""
import io
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import rule_proposals
from assistant_self_update import capability, update
from codex_http_bridge import BridgeError
from feishu_groups import atomic_write
from feishu_settings import load
from feishu_worker import FeishuAdapter, Inbox
from test_feishu import FakeBridge, FakeSender, event


class ProposalSender(FakeSender):
    def send_text(self, open_id, request_id, body):
        self.sent.append((open_id, request_id, body))
        return 'om_private_' + str(len(self.sent))


class ProposalBridge(FakeBridge):
    def __init__(self, groups):
        super().__init__()
        self.groups = groups

    def rule_answer(self, payload):
        try:
            return update(self.groups, payload)
        except BridgeError as exc:
            body = json.dumps({'error': {'code': exc.code, 'message': str(exc)}}).encode()
            raise HTTPError('http://127.0.0.1', exc.status, str(exc), {}, io.BytesIO(body))


class RuleProposalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.path = root / 'feishu.json'
        self.inbox = Inbox(root / 'inbox.sqlite3')
        self.groups = SimpleNamespace(config_path=self.path, inbox_path=self.inbox.path)
        atomic_write(self.path, {
            'enabled': True, 'app_id': 'cli_shared', 'connection_key': 'test-key',
            'connection_id': 'con_default', 'bot_open_id': 'ou_bot',
            'auto_group_owner_open_id': 'ou_owner', 'allowed_open_ids': ['ou_owner'],
            'allowed_chat_ids': ['oc_team'], 'prompt_policy_version': 1,
            'prompt_template': 'CANONICAL {{message}}', 'self_update_rule': '',
            'group_configs': {'oc_team': {'enabled': True}},
        })
        self.sender = ProposalSender()
        self.bridge = ProposalBridge(self.groups)
        self.adapter = FeishuAdapter(load(self.path), self.inbox, self.bridge, self.sender)

    def tearDown(self):
        self.tmp.cleanup()

    def refresh(self):
        # A real receiver restarts with the new config whenever the file changes.
        self.adapter.config = load(self.path)

    def say(self, mid, text, sender, group=True, parent=None, mention=True):
        self.refresh()
        payload = event(mid, sender=sender, chat='oc_team' if group else 'oc_dm',
                        chat_type='group' if group else 'p2p', text=text, parent=parent,
                        mentions=[{'id': {'open_id': 'ou_bot'}, 'key': '@bot'}] if group and mention else None)
        return self.adapter.handle_event(payload)

    def call(self, mid, sender, **body):
        self.refresh()
        return update(self.groups, {'source_id': mid, 'token': capability(self.adapter.config, mid, sender), **body})

    def rule(self, mid, sender, text, group=True):
        self.assertTrue(self.say(mid, '记住：' + text, sender, group))
        return self.call(mid, sender, ops=[{'op': 'add', 'text': text}])

    def pump(self):
        self.refresh()
        self.adapter.pump_rule_proposals()
        self.refresh()

    def proposal_message(self):
        return next(s for s in reversed(self.sender.sent) if 'proposal:' in str(s[1]))

    def outbound_of(self, number):
        with sqlite3.connect(self.inbox.path) as db:
            return db.execute('SELECT outbound_id FROM rule_proposals WHERE number=?', (number,)).fetchone()[0]

    def test_conflict_asks_author_in_group_and_applies_on_quoted_yes(self):
        self.rule('om_alice_1', 'ou_alice', '表格只列关键数字。')
        self.rule('om_bob_1', 'ou_bob', '表格要列全部数字。')
        created = self.call('om_bob_1', 'ou_bob', ops=[{'op': 'remove', 'id': 1}], propose='第 2 条和第 1 条冲突')
        self.assertEqual(created['where'], '原群')
        self.pump()
        chat, _, body = self.proposal_message()
        self.assertEqual(chat, 'oc_team')
        self.assertIn('<at user_id="ou_alice"></at>', body)
        self.assertIn('#P1', body)
        self.assertIn('删除第 1 条「表格只列关键数字。」', body)
        outbound = self.outbound_of(1)
        # Bob cannot consent for Alice, and Alice's unrelated "同意" is not consent.
        self.assertFalse(self.say('om_bob_yes', '同意', 'ou_bob', parent=outbound, mention=False))
        self.say('om_alice_other', '同意', 'ou_alice')
        self.assertEqual([i['id'] for i in load(self.path)['rule_items']], [1, 2])
        self.assertTrue(self.say('om_alice_yes', '同意', 'ou_alice', parent=outbound, mention=False))
        self.pump()
        self.assertEqual([i['id'] for i in load(self.path)['rule_items']], [2])
        self.assertIn('已生效', self.sender.sent[-1][2])
        self.assertEqual(self.sender.sent[-1][0], 'om_alice_yes')
        # A retried relay does not apply anything twice.
        self.pump()
        self.assertIn('已生效', self.sender.sent[-1][2])

    def test_named_answer_without_quote_and_rejection_is_final(self):
        self.rule('om_alice_2', 'ou_alice', '周报先写结论。')
        self.rule('om_bob_2', 'ou_bob', '周报先写数据。')
        self.call('om_bob_2', 'ou_bob', ops=[{'op': 'replace', 'id': 1, 'text': '周报先写数据再写结论。'}],
                  propose='两条顺序冲突')
        self.pump()
        self.assertTrue(self.say('om_alice_no', '#P1 不同意', 'ou_alice'))
        self.pump()
        self.assertIn('不执行', self.sender.sent[-1][2])
        self.assertEqual(load(self.path)['rule_items'][0]['text'], '周报先写结论。')
        self.assertTrue(self.say('om_bob_3', '记住：周报先写数据', 'ou_bob'))
        with self.assertRaises(BridgeError) as error:
            self.call('om_bob_3', 'ou_bob', ops=[{'op': 'replace', 'id': 1, 'text': '周报先写数据再写结论。'}],
                      propose='再问一次')
        self.assertEqual(error.exception.code, 'proposal_rejected_before')

    def test_every_asked_author_must_agree_and_changes_make_it_stale(self):
        self.rule('om_alice_4', 'ou_alice', '回复先给结论。')
        self.rule('om_carol_4', 'ou_carol', '回复先给结论再给依据。')
        self.rule('om_bob_4', 'ou_bob', '回复要先给出结论。')
        self.call('om_bob_4', 'ou_bob', propose='三条意思重复，合并成一条',
                  ops=[{'op': 'remove', 'id': 1}, {'op': 'remove', 'id': 2}])
        self.pump()
        _, _, body = self.proposal_message()
        self.assertIn('ou_alice', body)
        self.assertIn('ou_carol', body)
        outbound = self.outbound_of(1)
        self.assertTrue(self.say('om_alice_ok', '同意', 'ou_alice', parent=outbound, mention=False))
        self.pump()
        self.assertIn('还在等其他 1 人', self.sender.sent[-1][2])
        # Carol edits her own rule before answering; the old proposal no longer applies.
        self.assertTrue(self.say('om_carol_edit', '把第2条改成先结论后依据', 'ou_carol'))
        self.call('om_carol_edit', 'ou_carol', ops=[{'op': 'replace', 'id': 2, 'text': '先结论，后依据。'}])
        self.assertTrue(self.say('om_carol_ok', '同意', 'ou_carol', parent=outbound, mention=False))
        self.pump()
        self.assertIn('作废', self.sender.sent[-1][2])
        self.assertEqual(len(load(self.path)['rule_items']), 3)

    def test_private_rules_go_to_creator_and_cooldown_limits_noise(self):
        self.rule('om_owner_5', 'ou_owner', '一律用中文回答。', group=False)
        self.rule('om_bob_5', 'ou_bob', '技术名词保留英文。')
        created = self.call('om_bob_5', 'ou_bob', ops=[{'op': 'replace', 'id': 1, 'text': '用中文回答，技术名词保留英文。'}],
                            propose='两条可以合并')
        self.assertEqual(created['where'], '创建者私聊')
        self.pump()
        target, _, body = self.proposal_message()
        self.assertEqual(target, 'ou_owner')
        self.assertNotIn('<at', body)
        self.assertTrue(self.say('om_owner_yes', '同意', 'ou_owner', group=False, parent=self.outbound_of(1)))
        self.pump()
        self.assertEqual(load(self.path)['rule_items'][0]['text'], '用中文回答，技术名词保留英文。')
        self.assertTrue(self.say('om_bob_6', '记住：再合并一次', 'ou_bob'))
        with self.assertRaises(BridgeError) as error:
            self.call('om_bob_6', 'ou_bob', ops=[{'op': 'remove', 'id': 1}], propose='再整理')
        self.assertEqual(error.exception.code, 'proposal_cooldown')

    def test_ignored_proposals_expire_and_stop_after_two(self):
        self.rule('om_alice_7', 'ou_alice', '每条回复带来源链接。')
        ops = [{'op': 'remove', 'id': 1}]
        config = load(self.path)
        for round_ in range(2):
            rule_proposals.create(config, self.inbox.path, ops=ops, reason='来源要求和别的规则重复',
                                  kind='merge', proposer='ou_bob', source_id=None, owner='ou_owner',
                                  now=time.time() - 3 * 86400 + round_ * 1.5 * 86400)
            self.pump()
            with sqlite3.connect(self.inbox.path) as db:
                db.execute("UPDATE rule_proposals SET expires_at=0")
            rule_proposals.due(self.inbox.path, 'cli_shared')
        with self.assertRaises(BridgeError) as error:
            rule_proposals.create(config, self.inbox.path, ops=ops, reason='第三次', kind='merge',
                                  proposer='ou_bob', source_id=None, owner='ou_owner')
        self.assertEqual(error.exception.code, 'proposal_ignored_before')
        self.assertTrue(self.say('om_alice_late', '同意', 'ou_alice', parent=self.outbound_of(2), mention=False)
                        is False)

    def test_own_rules_need_no_proposal(self):
        self.rule('om_alice_8', 'ou_alice', '日报控制在五行以内。')
        self.assertTrue(self.say('om_alice_9', '记住：日报控制在三行以内', 'ou_alice'))
        with self.assertRaises(BridgeError) as error:
            self.call('om_alice_9', 'ou_alice', ops=[{'op': 'replace', 'id': 1, 'text': '日报三行以内。'}],
                      propose='改短')
        self.assertEqual(error.exception.code, 'proposal_not_needed')

    def test_legacy_split_waits_for_creator_and_keeps_old_paragraph(self):
        config = load(self.path)
        config['self_update_rule'] = '回答先给结论。表格只列关键数字。'
        atomic_write(self.path, config)
        rule_proposals.create(config, self.inbox.path, kind='legacy_split', reason='拆成逐条保存',
                              ops=[{'op': 'add', 'text': '回答先给结论。'}, {'op': 'add', 'text': '表格只列关键数字。'}],
                              proposer='ou_owner', source_id=None, owner='ou_owner')
        self.pump()
        target, _, body = self.proposal_message()
        self.assertEqual(target, 'ou_owner')
        self.assertIn('拆成下面这些条目', body)
        self.assertEqual(load(self.path)['self_update_rule'], '回答先给结论。表格只列关键数字。')
        self.assertTrue(self.say('om_owner_split', '同意', 'ou_owner', group=False, parent=self.outbound_of(1)))
        self.pump()
        saved = load(self.path)
        self.assertEqual(saved['self_update_rule'], '')
        self.assertEqual([(i['id'], i['author']) for i in saved['rule_items']], [(1, 'ou_owner'), (2, 'ou_owner')])
        self.assertEqual(saved['legacy_rule_history'][0]['text'], '回答先给结论。表格只列关键数字。')

    def test_unclear_quoted_reply_asks_again_and_keeps_proposal_open(self):
        self.rule('om_alice_10', 'ou_alice', '图表统一用蓝色系。')
        self.rule('om_bob_10', 'ou_bob', '图表配色跟随品牌色。')
        self.call('om_bob_10', 'ou_bob', ops=[{'op': 'remove', 'id': 1}], propose='配色冲突')
        self.pump()
        outbound = self.outbound_of(1)
        self.assertTrue(self.say('om_alice_hmm', '让我想想', 'ou_alice', parent=outbound, mention=False))
        self.pump()
        self.assertIn('没看懂', self.sender.sent[-1][2])
        self.assertTrue(self.say('om_alice_ok2', '可以', 'ou_alice', parent=outbound, mention=False))
        self.pump()
        self.assertEqual([i['id'] for i in load(self.path)['rule_items']], [2])


if __name__ == '__main__':
    unittest.main()
