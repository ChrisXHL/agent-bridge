"""Native group @ messages, no child task or business rule rewriting."""
import json
import unittest
from unittest.mock import patch
import test_scheduled_collaboration as scheduled_fixtures
from test_group_topics import Sender
from codex_http_bridge import BridgeError
from feishu_worker import Inbox


class GroupMentionTests(scheduled_fixtures.ScheduledCollaborationTests):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.c.topics.sender_factory = lambda app, secret: Sender(app, self.sent)
        for i, job in enumerate(self.f.jobs):
            cfg = self.configs[job['id']]
            # Preserve legacy per-group prompt coverage independently of new
            # shared-policy creation defaults.
            cfg.pop('prompt_policy_version', None)
            cfg['bot_open_id'] = 'ou_mention_' + str(i)
            from feishu_groups import atomic_write
            atomic_write(self.o.groups.resolve(job['app_ref'])[1], cfg)

    def send_mention(self, session, **kwargs):
        return self.c.mentions.send(session, {'request_id': 'native-mention-001',
            'assistant_name': self.peer['name'], 'text': '找到一条官方模型更新，请按你的规则处理。', **kwargs})

    def test_native_mention_uses_actor_identity_and_no_child_task(self):
        task, session = self.start()
        _, target_path, _ = self.o.groups.resolve(self.peer['app_ref'])
        rules_before = target_path.read_bytes()
        with self.store.db() as db:
            before = db.execute('SELECT count(*) FROM tasks').fetchone()[0]
        out = self.send_mention(session)
        self.assertEqual(out['state'], 'sent')
        self.assertFalse(out['receiver_started'])
        self.assertEqual(self.sent[-1]['app'], self.configs[self.source['id']]['app_id'])
        self.assertEqual(self.sent[-1]['chat'], 'oc_handoff')
        self.assertIn('<at user_id="ou_mention_1">', self.sent[-1]['body'])
        self.assertEqual(self.send_mention(session)['outbound_id'], out['outbound_id'])
        self.assertEqual(len(self.sent), 1)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], before)
            self.assertEqual(db.execute('SELECT count(*) FROM assistant_calls').fetchone()[0], 0)
        prompt = self.f.fake.messages[task['thread_id']][0]
        self.assertIn(' mention --request-id ', prompt)
        self.assertIn('独立按自身已保存规则', prompt)
        self.assertEqual(target_path.read_bytes(), rules_before)

    def test_sent_mention_is_polled_into_target_independent_task_with_own_rules(self):
        from types import SimpleNamespace
        import time
        from feishu_worker import FeishuAdapter, Inbox
        from test_feishu import FakeBridge, FakeSender
        source_task, session = self.start()
        out = self.send_mention(session)
        cfg = self.configs[self.peer['id']]
        bridge, sender = FakeBridge(), FakeSender()
        _, _, path = self.o.groups.resolve(self.peer['app_ref'])
        adapter = FeishuAdapter(cfg, Inbox(path), bridge, sender)
        self.assertIsNone(adapter.inbox.bot_message_cursor('oc_handoff'))
        native_body = self.sent[-1]['body']
        marker = native_body.split('</at>', 1)[0] + '</at>'
        message = SimpleNamespace(create_time=str(int(time.time() * 1000) + 100),
            message_id=out['outbound_id'], msg_type='text', parent_id=None, root_id=None,
            sender=SimpleNamespace(sender_type='app', id=self.configs[self.source['id']]['app_id']),
            mentions=[SimpleNamespace(id=cfg['bot_open_id'], key=marker, name=self.peer['name'])],
            body=SimpleNamespace(content=json.dumps({'text': native_body})))
        sender.list_group_messages = lambda *args: SimpleNamespace(success=lambda: True,
            data=SimpleNamespace(items=[message], has_more=False))
        adapter.poll_bot_messages();adapter.pump_once()
        item = adapter.inbox.linked_source(out['outbound_id'])
        self.assertIsNotNone(item['task_id'])
        self.assertNotEqual(item['task_id'], source_task['id'])
        self.assertTrue(json.loads(item['route_json'])['bot_mention'])
        self.assertIn('测试群规则', bridge.submissions[0][0])
        self.assertNotIn('协作子助手', bridge.submissions[0][0])
        self.assertEqual(bridge.continuations, [None])
        adapter.next_bot_poll=0;adapter.poll_bot_messages();adapter.pump_once()
        self.assertEqual(len(bridge.submissions), 1)

    def test_internal_child_cannot_bypass_ancestry_by_public_mention(self):
        _, session = self.start()
        batch = self.call(session)
        self.f.engine.tick()
        child = self.c.result(session, batch['batch_id'])['results'][0]['task_id']
        context = json.loads((self.c.root / (child + '.json')).read_text())
        child_session = self.c.authenticate('Bearer ' + context['token'])
        with self.assertRaises(BridgeError) as error:
            self.c.mentions.send(child_session, {'request_id': 'child-mention-001',
                'assistant_name': self.source['name'], 'text': '反向派发给主助手'})
        self.assertEqual(error.exception.code, 'mention_child_forbidden')
        self.assertEqual(self.sent, [])

    def test_any_assistant_member_or_everyone_can_be_mentioned_without_authorization(self):
        _, session = self.start()
        self.c.configure_policy({'mode': 'confirm'})
        out = self.send_mention(session, assistant_name=self.outside['name'])
        self.assertEqual(out['state'], 'sent')
        self.assertIn('<at user_id="ou_mention_', self.sent[-1]['body'])
        member_sender = Sender(self.configs[self.source['id']]['app_id'], self.sent)
        member_sender.list_chat_members = lambda chat: [{'open_id': 'ou_human_1', 'name': '张三'}]
        with patch.object(self.c.topics, 'sender_factory', return_value=member_sender):
            out = self.c.mentions.send(session, {'request_id': 'open-mention-002', 'names': ['张三'],
                'open_ids': ['ou_external_bot'], 'all': True, 'text': '请大家看一下'})
            self.assertEqual(out['state'], 'sent')
            body = self.sent[-1]['body']
            for tag in ('<at user_id="all">', '<at user_id="ou_human_1">张三</at>', '<at user_id="ou_external_bot">'):
                self.assertIn(tag, body)
            with self.assertRaises(BridgeError) as error:
                self.c.mentions.send(session, {'request_id': 'open-mention-003', 'names': ['不存在的人'], 'text': 'x'})
            self.assertEqual(error.exception.code, 'mention_target_unknown')
            self.assertIn('张三', str(error.exception))

    def test_changed_request_is_rejected_and_inline_native_tag_allowed(self):
        _, session = self.start()
        self.send_mention(session)
        with self.assertRaises(BridgeError) as error:
            self.send_mention(session, text='换一条不同内容')
        self.assertEqual(error.exception.code, 'request_changed')
        out = self.c.mentions.send(session, {'request_id': 'inline-mention-id',
            'text': '<at user_id="all">所有人</at> 注意一下'})
        self.assertEqual(out['state'], 'sent')
        self.assertTrue(self.sent[-1]['body'].startswith('<at user_id="all">'))

    def test_failed_send_retries_same_stable_id_without_claiming_success(self):
        _, session = self.start()
        sender = Sender(self.configs[self.source['id']]['app_id'], self.sent)
        with patch.object(sender, 'send_group', side_effect=OSError('response lost')):
            with patch.object(self.c.topics, 'sender_factory', return_value=sender):
                out = self.send_mention(session)
        self.assertEqual(out['state'], 'pending')
        with self.store.db() as db:
            db.execute('UPDATE assistant_group_mentions SET next_attempt=0')
        self.c.mentions.deliver()
        self.assertEqual(self.sent[0]['key'], out['id'])
        self.assertEqual(self.send_mention(session)['state'], 'sent')

    def receiver_session(self, out):
        """Deliver a sent @ into the peer's inbox as its own task, like the real receiver."""
        cfg = self.configs[self.peer['id']]
        _, _, path = self.o.groups.resolve(self.peer['app_ref'])
        box = Inbox(path)
        box.accept({'message_id': out['outbound_id'], 'app_id': cfg['app_id'],
            'connection_id': cfg['connection_id'], 'chat_id': 'oc_handoff', 'chat_type': 'group',
            'sender_id': self.configs[self.source['id']]['app_id'], 'text': '真实@资料',
            'prompt': '按自己的规则处理', 'route_json': '{"mode":"prompt","sender_type":"app"}'})
        import hashlib
        rid = 'fs_' + hashlib.sha256((cfg['app_id'] + ':' + out['outbound_id']).encode()).hexdigest()[:48]
        _, created = self.store.submit(self.store.connection(cfg['connection_id']), {'message': '按自己的规则处理'}, rid)
        box.mark_submitted(out['outbound_id'], created['task_id'])
        self.f.engine.tick()
        context = json.loads((self.c.root / (created['task_id'] + '.json')).read_text())
        return self.c.authenticate('Bearer ' + context['token']), box, cfg, created['task_id']

    def test_receiver_can_mention_back_but_chain_is_capped(self):
        _, session = self.start()
        out = self.send_mention(session)
        receiver, _, _, _ = self.receiver_session(out)
        back = self.c.mentions.send(receiver, {'request_id': 'return-mention-001', 'assistant_name': self.source['name'], 'text': '做完了，交回给你'})
        self.assertEqual(back['state'], 'sent')
        from group_mentions import MAX_HOPS
        with self.store.db() as db:
            db.execute('UPDATE assistant_group_mentions SET ancestry=? WHERE outbound_id=?',
                       (json.dumps(['x'] * MAX_HOPS), out['outbound_id']))
        with self.assertRaises(BridgeError) as error:
            self.c.mentions.send(receiver, {'request_id': 'return-mention-002', 'assistant_name': self.source['name'], 'text': '再来一轮'})
        self.assertEqual(error.exception.code, 'mention_chain_too_long')

    def test_multi_target_mention_counts_each_branch_separately(self):
        _, session = self.start()
        out = self.c.mentions.send(session, {'request_id': 'fanout-mention-01',
            'names': [self.peer['name'], self.outside['name']], 'text': '你们各做一部分'})
        with self.store.db() as db:
            stored = json.loads(db.execute('SELECT ancestry FROM assistant_group_mentions WHERE id=?',
                                           (out['id'],)).fetchone()[0])
        self.assertEqual(stored, [self.source['id']])
        receiver, _, cfg, _ = self.receiver_session(out)
        self.assertEqual(self.c.mentions.chain(receiver, cfg)[0], [self.source['id'], self.peer['id']])

    def test_recipients_say_whether_each_target_will_pick_up(self):
        _, session = self.start()
        out = self.c.mentions.send(session, {'request_id': 'reach-mention-01', 'all': True,
            'names': [self.peer['name'], self.outside['name']], 'open_ids': ['ou_someone'], 'text': '看一下'})
        kinds = {r['name']: r['kind'] for r in out['recipients']}
        self.assertEqual(kinds[self.peer['name']], 'assistant')
        self.assertEqual(kinds[self.outside['name']], 'assistant_not_listening')
        self.assertEqual(kinds['所有人'], 'everyone')
        self.assertEqual(kinds['ou_someone'], 'member_or_external')

    def test_person_interjecting_in_same_task_restarts_the_chain(self):
        _, session = self.start()
        out = self.send_mention(session)
        receiver, box, cfg, task_id = self.receiver_session(out)
        from group_mentions import MAX_HOPS
        with self.store.db() as db:
            db.execute('UPDATE assistant_group_mentions SET ancestry=? WHERE outbound_id=?',
                       (json.dumps(['x'] * MAX_HOPS), out['outbound_id']))
        with self.assertRaises(BridgeError):
            self.c.mentions.send(receiver, {'request_id': 'capped-mention-01', 'assistant_name': self.source['name'], 'text': '再来'})
        box.accept({'message_id': 'om_user_interjects', 'app_id': cfg['app_id'],
            'connection_id': cfg['connection_id'], 'chat_id': 'oc_handoff', 'chat_type': 'group',
            'sender_id': 'ou_real_person', 'text': '继续讨论', 'prompt': '继续',
            'route_json': '{"mode":"prompt","sender_type":"user"}'})
        box.mark_submitted('om_user_interjects', task_id)
        self.assertEqual(self.c.mentions.chain(receiver, cfg)[0], [self.peer['id']])
        again = self.c.mentions.send(receiver, {'request_id': 'fresh-mention-01', 'assistant_name': self.source['name'], 'text': '用户让继续'})
        self.assertEqual(again['state'], 'sent')


if __name__ == '__main__':
    unittest.main()
