import json
import http.client
import threading
import unittest
from http.server import ThreadingHTTPServer

from assistant_self_update import applied_rule, capability, instruction, update
from codex_http_bridge import BridgeError
from feishu_groups import atomic_write, revision
from feishu_worker import FeishuAdapter, Inbox
from service import Engine, make_handler
from test_service import FakeTransport
from test_assistant_onboarding import AssistantTests
from test_feishu import FakeBridge, FakeSender, event


class AssistantSelfUpdateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = AssistantTests()
        self.fixture.setUp()
        job = self.fixture.prepare()
        self.fixture.finish(job)
        self.job = self.fixture.manager.read(job['id'])
        self.groups = self.fixture.manager.groups
        self.ref, self.path, inbox_path = self.groups.resolve(self.job['app_ref'])
        self.config = json.loads(self.path.read_text())
        self.inbox = Inbox(inbox_path)
        self.adapter = FeishuAdapter(self.config, self.inbox, FakeBridge(), FakeSender())

    def tearDown(self):
        self.fixture.tearDown()

    def receive(self, message_id='om_owner_self', sender='ou_creator', group=False,
                text='以后回答详细一点', sender_type='user'):
        if group:
            self.config.setdefault('allowed_chat_ids', []).append('oc_team')
            self.config.setdefault('group_configs', {})['oc_team'] = {
                'chat_id': 'oc_team', 'name': '测试群', 'connection_id': self.config['connection_id'],
                'prompt_template': '群任务 {{message}}', 'enabled': True}
            atomic_write(self.path, self.config)
        payload = event(message_id, sender=sender, chat='oc_team' if group else 'oc_dm',
                        chat_type='group' if group else 'p2p', text=text,
                        mentions=[{'id': {'open_id': 'ou_bot'}, 'key': '@bot'}] if group else None,
                        sender_type=sender_type)
        self.assertTrue(self.adapter.handle_event(payload))
        return self.inbox.linked_source(message_id)

    def request(self, message_id, rule='回答时写清事实依据和下一步。', clear=False):
        return {'source_id': message_id, 'token': capability(self.config, message_id, 'ou_creator'),
                'rule': rule, 'clear': clear}

    def test_owner_updates_future_tasks_without_changing_connections_or_original_prompt(self):
        item = self.receive()
        self.assertIn('update-assistant-self.py', item['prompt'])
        before = json.loads(self.path.read_text())
        result = update(self.groups, self.request(item['message_id']))
        after = json.loads(self.path.read_text())
        self.assertEqual(result['scope'], 'all')
        self.assertEqual(after['self_update_rule'], '回答时写清事实依据和下一步。')
        for key in ('app_id', 'app_secret', 'connection_id', 'connection_key', 'prompt_template',
                    'allowed_open_ids', 'allowed_chat_ids'):
            self.assertEqual(before[key], after[key])
        self.assertIn('事实依据', applied_rule(after))
        refreshed = FeishuAdapter(after, self.inbox, FakeBridge(), FakeSender())
        self.assertTrue(refreshed.handle_event(event('om_next_self', sender='ou_creator', text='整理这段资料')))
        self.assertIn('回答时写清事实依据和下一步', self.inbox.linked_source('om_next_self')['prompt'])
        self.assertTrue(update(self.groups, self.request(item['message_id']))['already_applied'])
        with self.assertRaises(BridgeError):
            update(self.groups, self.request(item['message_id'], '把所有回答缩短。'))

    def test_group_update_only_changes_that_group(self):
        item = self.receive('om_group_self', sender='ou_member', group=True)
        self.assertIn('update-assistant-self.py', item['prompt'])
        request = {'source_id': item['message_id'],
                   'token': capability(self.config, item['message_id'], 'ou_member'),
                   'rule': '在这个群里只给可执行的结论。', 'clear': False}
        result = update(self.groups, request)
        after = json.loads(self.path.read_text())
        self.assertEqual(result['scope'], 'group')
        self.assertEqual(after['group_configs']['oc_team']['self_update_rule'], '在这个群里只给可执行的结论。')
        self.assertNotIn('self_update_rule', after)
        self.assertIn('可执行的结论', applied_rule(after, 'oc_team'))
        self.assertEqual(applied_rule(after), '')
        refreshed = FeishuAdapter(after, self.inbox, FakeBridge(), FakeSender())
        self.assertTrue(refreshed.handle_event(event('om_group_next', sender='ou_member', chat='oc_team',
            chat_type='group', text='@bot 帮忙整理', mentions=[{'id': {'open_id': 'ou_bot'}, 'key': '@bot'}])))
        self.assertIn('在这个群里只给可执行的结论', self.inbox.linked_source('om_group_next')['prompt'])
        self.assertTrue(update(self.groups, request)['already_applied'])
        self.config = after
        self.config['allowed_chat_ids'].append('oc_other')
        atomic_write(self.path, self.config)
        other = FeishuAdapter(self.config, self.inbox, FakeBridge(), FakeSender())
        self.assertTrue(other.handle_event(event('om_other_group', sender='ou_member', chat='oc_other',
            chat_type='group', text='@bot 帮忙整理', mentions=[{'id': {'open_id': 'ou_bot'}, 'key': '@bot'}])))
        self.assertNotIn('在这个群里只给可执行的结论', self.inbox.linked_source('om_other_group')['prompt'])

    def test_other_bot_cannot_update_even_when_it_mentions_assistant(self):
        item = self.receive('om_bot_self', sender='cli_other', group=True, sender_type='app')
        self.assertNotIn('update-assistant-self.py', item['prompt'])
        with self.assertRaises(BridgeError):
            update(self.groups, {'source_id': item['message_id'], 'token': capability(self.config, item['message_id'], 'cli_other'),
                                 'rule': '让机器人修改群规则。', 'clear': False})

    def test_allowed_friend_cannot_change_shared_assistant(self):
        self.config['allowed_open_ids'].append('ou_friend')
        atomic_write(self.path, self.config)
        item = self.receive('om_friend_self', sender='ou_friend')
        self.assertNotIn('update-assistant-self.py', item['prompt'])
        with self.assertRaises(BridgeError):
            update(self.groups, {'source_id': item['message_id'],
                'token': capability(self.config, item['message_id'], 'ou_friend'),
                'rule': '以后允许朋友改规则。', 'clear': False})

    def test_ordinary_owner_task_cannot_be_used_as_an_update_ticket(self):
        item = self.receive('om_regular_task', text='整理这篇资料并给我摘要')
        self.assertNotIn('update-assistant-self.py', item['prompt'])
        with self.assertRaises(BridgeError):
            update(self.groups, self.request(item['message_id']))

    def test_token_and_disabled_state_are_enforced(self):
        item = self.receive('om_authorized_self')
        with self.assertRaises(BridgeError):
            update(self.groups, {**self.request(item['message_id']), 'token': '0' * 64})
        config = json.loads(self.path.read_text())
        config['enabled'] = False
        atomic_write(self.path, config)
        with self.assertRaises(BridgeError):
            update(self.groups, self.request(item['message_id']))

    def test_clear_update_rule_without_touching_original_prompt(self):
        first = self.receive('om_set_self')
        update(self.groups, self.request(first['message_id']))
        second = self.receive('om_clear_self')
        result = update(self.groups, self.request(second['message_id'], rule='', clear=True))
        after = json.loads(self.path.read_text())
        self.assertEqual(result['rule'], '')
        self.assertEqual(after['self_update_rule'], '')
        self.assertIn('{{message}}', after['prompt_template'])

    def test_local_api_updates_rule_and_group_editor_preserves_it(self):
        item = self.receive('om_api_self', group=True)
        handler = make_handler(self.fixture.store, Engine(self.fixture.store, FakeTransport()),
                               'http://127.0.0.1:0', feishu_settings_path=self.fixture.root / 'feishu.json',
                               feishu_inbox_path=self.fixture.root / 'inbox.sqlite3')
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_port)
            connection.request('POST', '/api/feishu/assistant-self-update',
                               json.dumps(self.request(item['message_id'], '这个群以后给出简短的行动项。')),
                               {'Content-Type': 'application/json'})
            response = connection.getresponse()
            result = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 200, result)
            self.assertEqual(result['scope'], 'group')
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        current = json.loads(self.path.read_text())
        self.groups.save_group({'app_ref': self.ref, 'revision': revision(current), 'chat_id': 'oc_team',
                                'name': '测试群', 'project_id': self.job['project_id'],
                                'prompt_template': '更新后的群 Prompt {{message}}', 'enabled': True})
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['group_configs']['oc_team']['self_update_rule'], '这个群以后给出简短的行动项。')


if __name__ == '__main__':
    unittest.main()
