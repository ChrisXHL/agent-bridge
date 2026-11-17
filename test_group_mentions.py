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

    def test_target_requires_group_binding_and_authorization(self):
        _, session = self.start()
        with self.assertRaises(BridgeError):
            self.send_mention(session, assistant_name=self.outside['name'])
        self.c.configure_policy({'mode': 'confirm'})
        with self.assertRaises(BridgeError) as error:
            self.send_mention(session)
        self.assertEqual(error.exception.code, 'mention_authorization_required')
        self.assertEqual(self.sent, [])

    def test_changed_request_and_extra_native_tag_are_rejected(self):
        _, session = self.start()
        self.send_mention(session)
        with self.assertRaises(BridgeError) as error:
            self.send_mention(session, text='换一条不同内容')
        self.assertEqual(error.exception.code, 'request_changed')
        with self.assertRaises(BridgeError):
            self.send_mention(session, request_id='another-mention-id', text='<at user_id="all">所有人</at>')

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

    def test_real_outbox_prevents_receiver_mentioning_ancestor(self):
        _, session = self.start()
        out = self.send_mention(session)
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
        receiver = self.c.authenticate('Bearer ' + context['token'])
        with self.assertRaises(BridgeError) as error:
            self.c.mentions.send(receiver, {'request_id': 'return-mention-001', 'assistant_name': self.source['name'], 'text': '再派给原助手'})
        self.assertEqual(error.exception.code, 'mention_cycle')


if __name__ == '__main__':
    unittest.main()
