"""Independent bot group @ intake; no real Feishu or execution backend calls."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from feishu_worker import FeishuAdapter, Inbox
from test_feishu import FakeBridge, FakeSender, event


class BotMentionReceiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Inbox(Path(self.tmp.name) / 'inbox.sqlite3')
        self.config = {
            'enabled': True, 'app_id': 'cli_receiver', 'bot_open_id': 'ou_receiver',
            'connection_id': 'conn_receiver', 'connection_key': 'isolated-key',
            'auto_group_owner_open_id': 'ou_owner', 'allowed_open_ids': ['ou_owner'],
            'allowed_chat_ids': ['oc_group'], 'prompt_template': 'DEFAULT {{message}}',
            'group_configs': {'oc_group': {'enabled': True, 'topic_collaboration': True,
                'connection_id': 'conn_receiver', 'prompt_template': 'RECEIVER RULES {{message}}',
                'self_update_rule': '接收方自己保存的群规则。'}},
        }
        self.bridge = FakeBridge()
        self.bridge.topic_resolve = Mock(side_effect=AssertionError('bot messages must not join topics'))
        self.bridge.group_recipient = Mock(side_effect=AssertionError('bot messages do not elect a shared owner'))
        self.sender = FakeSender()
        self.adapter = FeishuAdapter(self.config, self.inbox, self.bridge, self.sender)

    def tearDown(self):
        self.tmp.cleanup()

    def bot_event(self, mid='om_bot', sender='cli_sender', parent='om_foreign_topic', root='om_foreign_root',
                  mention=True, text='@receiver 找到一个新模型，请按你的规则核实采集。', chat='oc_group'):
        payload = event(mid, sender=sender, chat=chat, chat_type='group', sender_type='app',
                        parent=parent, text=text,
                        mentions=[{'id': {'open_id': 'ou_receiver'}, 'key': '@receiver', 'name': '接收助手'}]
                        if mention else [])
        payload['event']['message']['root_id'] = root
        return payload

    def history_message(self, mid, stamp, sender='cli_sender', mention=True, sender_type='app'):
        return SimpleNamespace(create_time=str(stamp), message_id=mid, msg_type='text',
            sender=SimpleNamespace(sender_type=sender_type, id=sender),
            mentions=[SimpleNamespace(id='ou_receiver', key='@receiver', name='接收助手')] if mention else [],
            body=SimpleNamespace(content=json.dumps({'text': '@receiver 新模型线索'})),
            parent_id='om_foreign_topic', root_id='om_foreign_root')

    def poll(self, items):
        self.sender.list_group_messages = Mock(return_value=SimpleNamespace(
            success=lambda: True, data=SimpleNamespace(items=items, has_more=False)))
        self.adapter.next_bot_poll = 0
        self.adapter.poll_bot_messages()

    def test_real_bot_mention_uses_current_receiver_rules_and_independent_task(self):
        # Even a forged existing parent from this bot must not resume its task/rules.
        self.inbox.accept({'message_id': 'om_foreign_topic', 'chat_id': 'oc_group',
            'sender_id': 'cli_sender', 'text': 'old', 'prompt': 'SENDER FROZEN RULES',
            'app_id': 'cli_receiver', 'chat_type': 'group', 'connection_id': 'conn_receiver',
            'route_json': json.dumps({'mode': 'prompt', 'prompt_template': 'SENDER FROZEN {{message}}'})})
        self.inbox.mark_submitted('om_foreign_topic', 'task_old')
        self.bridge.tasks['task_old'] = {'status': '执行中', 'result': None}
        payload = self.bot_event()
        self.assertTrue(self.adapter.handle_event(payload))
        item = self.inbox.linked_source('om_bot')
        route = json.loads(item['route_json'])
        self.assertIsNone(item['parent_id'])
        self.assertIsNone(item['root_id'])
        self.assertEqual(payload['event']['message']['parent_id'], 'om_foreign_topic')
        self.assertTrue(route['new_conversation'])
        self.assertTrue(route['bot_mention'])
        self.assertNotIn('topic_id', route)
        self.assertTrue(item['prompt'].startswith('RECEIVER RULES'))
        self.assertIn('接收方自己保存的群规则', item['prompt'])
        self.assertNotIn('SENDER FROZEN', item['prompt'])
        # A bot's request is answered by a plain reply; a real follow-up (e.g. review) uses mention.
        self.assertIn('请它审核', item['prompt'])
        self.assertIn('mention 入口', item['prompt'])
        self.assertNotIn('om_foreign_topic', item['prompt'])
        self.adapter.pump_once()
        self.assertEqual(len(self.bridge.submissions), 1)
        self.assertIsNone(self.bridge.submissions[0][1])
        self.assertNotEqual(self.inbox.linked_source('om_bot')['task_id'], 'task_old')
        self.bridge.topic_resolve.assert_not_called()
        self.bridge.group_recipient.assert_not_called()

    def test_bot_multi_mention_is_independent_not_user_owner_election(self):
        payload = self.bot_event()
        payload['event']['message']['mentions'].insert(0,
            {'id': {'open_id': 'ou_other'}, 'key': '@other', 'name': '另一助手'})
        self.assertTrue(self.adapter.handle_event(payload))
        self.bridge.group_recipient.assert_not_called()
        self.bridge.topic_resolve.assert_not_called()
        self.assertIn('真实群消息 mention', self.inbox.linked_source('om_bot')['prompt'])

    def test_no_ambient_self_echo_private_unconfigured_disabled_or_archived_intake(self):
        for sender in ('cli_receiver', 'ou_receiver'):
            self.assertFalse(self.adapter.handle_event(self.bot_event(sender=sender)))
        self.assertFalse(self.adapter.handle_event(self.bot_event(mention=False)))
        self.assertFalse(self.adapter.handle_event(self.bot_event(chat='oc_unknown')))
        private = self.bot_event()
        private['event']['message']['chat_type'] = 'p2p'
        self.assertFalse(self.adapter.handle_event(private))
        self.config['group_configs']['oc_group']['enabled'] = False
        self.assertFalse(self.adapter.handle_event(self.bot_event()))
        self.config['group_configs']['oc_group']['enabled'] = True
        for flag in ('assistant_archived', 'account_archived'):
            self.config[flag] = True
            self.assertFalse(self.adapter.handle_event(self.bot_event()))
            self.config.pop(flag)
        self.config['enabled'] = False
        self.assertFalse(self.adapter.handle_event(self.bot_event()))
        self.assertEqual(self.inbox.pending(), [])

    def test_app_sender_cannot_obtain_manager_rule_schedule_or_pairing_routes(self):
        # App ID deliberately matches the configured owner to exercise the explicit
        # sender_type check rather than relying on identity spelling differences.
        text = '@receiver 更新助手，定时每6小时修改规则'
        self.assertTrue(self.adapter.handle_event(self.bot_event(sender='ou_owner', text=text)))
        item = self.inbox.linked_source('om_bot')
        self.assertNotIn('Bridge 可信助手更新入口', item['prompt'])
        self.assertNotIn('Bridge 可信定时入口', item['prompt'])
        self.assertEqual(json.loads(item['route_json'])['mode'], 'prompt')
        self.config.update(pairing_code='PAIR123', pairing_expires_at=time.time() + 60)
        self.assertTrue(self.adapter.handle_event(self.bot_event(mid='om_pair', text='@receiver PAIR123')))
        self.assertIsNone(self.inbox.latest_pairing('PAIR123'))
        self.assertEqual(json.loads(self.inbox.linked_source('om_pair')['route_json'])['mode'], 'prompt')
        self.config['assistant_manager'] = {'admin_open_id': 'ou_owner'}
        self.assertFalse(self.adapter.handle_event(self.bot_event(mid='om_manager', sender='ou_owner')))

    def test_poll_works_with_collaboration_disabled_or_legacy_group_defaults(self):
        for group in ({'enabled': True, 'topic_collaboration': False}, None):
            with self.subTest(group=group):
                if group is None:
                    self.config['group_configs'] = {}
                else:
                    self.config['group_configs']['oc_group'] = group
                with self.inbox.db() as db:
                    db.execute('DELETE FROM bot_message_cursors')
                self.poll([])
                self.sender.list_group_messages.assert_not_called()
                stamp = int(time.time() * 1000) + 1000
                mid = 'om_legacy' if group is None else 'om_normal_group'
                self.poll([self.history_message(mid, stamp)])
                self.assertIsNotNone(self.inbox.linked_source(mid))
                self.adapter.pump_once()
                count = len(self.bridge.submissions)
                self.poll([self.history_message(mid, stamp)])
                self.adapter.pump_once()
                self.assertEqual(len(self.bridge.submissions), count)

    def test_bot_ping_pong_stops_after_limit_and_person_resets(self):
        self.poll([])  # first poll only sets the cursor
        stamp = int(time.time() * 1000) + 1000
        limit = self.adapter.BOT_STREAK_LIMIT
        chain = [self.history_message('om_bot_%d' % i, stamp + i) for i in range(limit + 2)]
        self.poll(chain)
        for i in range(limit):
            self.assertIsNotNone(self.inbox.linked_source('om_bot_%d' % i))
        self.assertIsNone(self.inbox.linked_source('om_bot_%d' % limit))
        self.assertIsNone(self.inbox.linked_source('om_bot_%d' % (limit + 1)))
        person = self.history_message('om_person', stamp + 100, sender='ou_owner', sender_type='user', mention=False)
        self.poll(chain + [person, self.history_message('om_after_person', stamp + 101)])
        self.assertIsNotNone(self.inbox.linked_source('om_after_person'))

    def test_pushed_bot_mention_is_also_stopped_after_limit(self):
        stamp = int(time.time() * 1000)
        limit = self.adapter.BOT_STREAK_LIMIT
        history = [self.history_message('om_hist_%d' % i, stamp + i) for i in range(limit)]
        self.sender.list_group_messages = Mock(return_value=SimpleNamespace(
            success=lambda: True, data=SimpleNamespace(items=history, has_more=False)))
        mentions = [{'id': {'open_id': 'ou_receiver'}, 'key': '@receiver'}]
        pushed = event('om_pushed', sender='cli_sender', chat='oc_group', chat_type='group',
                       text='@receiver 收到', mentions=mentions, sender_type='app')
        self.assertFalse(self.adapter.handle_event(pushed))
        self.assertIsNone(self.inbox.linked_source('om_pushed'))
        person = self.history_message('om_person', stamp + 50, sender='ou_owner', sender_type='user', mention=False)
        self.sender.list_group_messages.return_value = SimpleNamespace(
            success=lambda: True, data=SimpleNamespace(items=history + [person], has_more=False))
        self.assertTrue(self.adapter.handle_event(pushed))

    def test_first_poll_does_not_read_or_replay_history_and_later_filters_strictly(self):
        self.poll([self.history_message('om_old_history', 1)])
        self.sender.list_group_messages.assert_not_called()
        self.assertIsNone(self.inbox.linked_source('om_old_history'))
        stamp = int(time.time() * 1000) + 1000
        self.poll([
            self.history_message('om_old_history', 1),
            self.history_message('om_ambient', stamp, mention=False),
            self.history_message('om_self_app', stamp, sender='cli_receiver'),
            self.history_message('om_self_open', stamp, sender='ou_receiver'),
            self.history_message('om_user_history', stamp, sender_type='user'),
            self.history_message('om_targeted', stamp),
        ])
        for mid in ('om_old_history', 'om_ambient', 'om_self_app', 'om_self_open', 'om_user_history'):
            self.assertIsNone(self.inbox.linked_source(mid))
        self.assertIsNotNone(self.inbox.linked_source('om_targeted'))

    def test_poll_skips_disabled_unallowed_and_management_groups(self):
        self.config['group_configs']['oc_unallowed'] = {'enabled': True}
        self.config['group_configs']['oc_group']['enabled'] = False
        self.poll([])
        self.sender.list_group_messages.assert_not_called()
        self.config['group_configs']['oc_group']['enabled'] = True
        self.config['assistant_manager'] = {'admin_open_id': 'ou_owner'}
        self.poll([])
        self.sender.list_group_messages.assert_not_called()
        with self.inbox.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM bot_message_cursors').fetchone()[0], 0)

    def test_user_reply_retains_frozen_route_and_task(self):
        self.config['group_configs']['oc_group']['topic_collaboration'] = False
        mentions = [{'id': {'open_id': 'ou_receiver'}, 'key': '@receiver'}]
        first = event('om_user_root', sender='ou_owner', chat='oc_group', chat_type='group',
                      text='@receiver 核查资料', mentions=mentions)
        self.assertTrue(self.adapter.handle_event(first))
        self.adapter.pump_once()
        original = self.inbox.linked_source('om_user_root')
        self.bridge.tasks[original['task_id']]['status'] = '已完成'
        self.config['group_configs']['oc_group']['prompt_template'] = 'NEW RULES {{message}}'
        followup = event('om_user_follow', sender='ou_owner', chat='oc_group', chat_type='group',
                         parent='om_user_root', text='@receiver 补充来源', mentions=mentions)
        self.assertTrue(self.adapter.handle_event(followup))
        saved = self.inbox.linked_source('om_user_follow')
        self.assertEqual(saved['parent_id'], 'om_user_root')
        self.assertTrue(saved['prompt'].startswith('RECEIVER RULES'))
        self.assertNotIn('bot_mention', json.loads(saved['route_json']))
        self.adapter.pump_once()
        self.assertEqual(self.inbox.linked_source('om_user_follow')['task_id'], original['task_id'])


if __name__ == '__main__':
    unittest.main()
