"""Real loopback preference API plus worker dispatch; no external messages/models."""
import http.client
import json
import subprocess
import sys
import threading
from pathlib import Path
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import test_assistant_onboarding as fixtures
from feishu_worker import Inbox, FeishuAdapter
from test_feishu import FakeBridge, FakeSender, event
from test_service import FakeTransport
from service import Engine, make_handler
from personal_preferences import capability, manage
from assistant_self_update import update, capability as rule_capability


class PreferenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.AssistantTests();self.f.setUp()
        job = self.f.prepare();self.f.finish(job)
        self.groups = self.f.manager.groups
        self.job = self.f.manager.read(job['id'])
        _, self.path, inbox = self.groups.resolve(self.job['app_ref'])
        self.config = json.loads(self.path.read_text())
        self.inbox = Inbox(inbox)
        self.bridge = FakeBridge()
        self.adapter = FeishuAdapter(self.config, self.inbox, self.bridge, FakeSender())

    def tearDown(self):
        self.f.tearDown()

    def receive(self, mid, text):
        self.assertTrue(self.adapter.handle_event(event(mid, sender='ou_creator', text=text)))
        return self.inbox.linked_source(mid)

    def request(self, item, action='set', **extra):
        return {'source_id': item['message_id'], 'token': capability(self.config, item['message_id'], 'ou_creator'),
                'action': action, **extra}

    def test_actual_api_stores_preference_then_worker_loads_for_next_message(self):
        item = self.receive('om_pref_api', '以后回复简短一点')
        self.assertNotIn('update-assistant-self.py', item['prompt'])
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.f.store, Engine(self.f.store, FakeTransport()),
            'http://127.0.0.1:0', feishu_settings_path=self.groups.config_path, feishu_inbox_path=self.groups.inbox_path))
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        try:
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
            conn.request('POST', '/api/feishu/personal-preferences', json.dumps(self.request(item, key='verbosity', value='concise')),
                         {'Content-Type': 'application/json'})
            response = conn.getresponse();result = json.loads(response.read());conn.close()
            self.assertEqual(response.status, 200, result)
            self.assertTrue(result['updated'])
        finally:
            server.shutdown();server.server_close();thread.join()
        self.adapter.pump_once()
        first_task = self.inbox.linked_source(item['message_id'])['task_id']
        self.bridge.tasks[first_task] = {'status': '已完成', 'result': '已记住'}
        self.receive('om_pref_follow', '帮我解释这个概念')
        self.adapter.pump_once()
        prompt = self.bridge.submissions[-1][0]
        self.assertIn('"verbosity": "concise"', prompt)
        self.assertNotIn('personal-preference.py', prompt)
        self.assertIn('当前', prompt)
        with self.assertRaises(Exception) as error:
            update(self.groups, {'source_id': item['message_id'], 'token': rule_capability(self.config, item['message_id'], 'ou_creator'),
                                 'rule': '以后全部回答简短。', 'clear': False})
        self.assertEqual(error.exception.code, 'personal_preference_only')
        self.assertNotIn('self_update_rule', json.loads(self.path.read_text()))

    def test_actual_helper_cli_persists_reads_and_deletes_through_http(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.f.store, Engine(self.f.store, FakeTransport()),
            'http://127.0.0.1:0', feishu_settings_path=self.groups.config_path, feishu_inbox_path=self.groups.inbox_path))
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        script = Path(__file__).parent / 'scripts/personal-preference.py'
        def run(item, *action):
            process = subprocess.run([sys.executable, str(script), '--bridge-url',
                'http://127.0.0.1:' + str(server.server_port), '--source-id', item['message_id'],
                '--token', capability(self.config, item['message_id'], 'ou_creator'), *action],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr)
            return json.loads(process.stdout)
        try:
            setting = self.receive('om_cli_save', '以后用表格回复')
            self.assertTrue(run(setting, '--action', 'set', '--key', 'format', '--value', 'table')['updated'])
            # Reopen the inbox to verify persistence rather than in-memory state.
            reopened = Inbox(self.inbox.path)
            with reopened.db() as db:
                value = db.execute('SELECT value FROM personal_preferences WHERE app_id=? AND user_open_id=? AND key=?',
                                   (self.config['app_id'], 'ou_creator', 'format')).fetchone()
                self.assertEqual(value['value'], 'table')
            listing = self.receive('om_cli_list', '查看我的个人偏好')
            self.assertEqual(run(listing, '--action', 'list')['preferences'], {'format': 'table'})
            deletion = self.receive('om_cli_delete', '忘记我的格式偏好')
            self.assertTrue(run(deletion, '--action', 'delete', '--key', 'format')['updated'])
            listing = self.receive('om_cli_empty', '查看我的个人偏好')
            self.assertEqual(run(listing, '--action', 'list')['preferences'], {})
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_retry_freezes_preference_snapshot(self):
        item = self.receive('om_pref_freeze', '以后用中文回复')
        manage(self.groups, self.request(item, key='language', value='zh'))
        self.bridge.fail_next = True
        self.adapter.pump_once()
        before = self.inbox.private_submission(item['message_id'])['message']
        with patch.object(self.bridge, 'submit', wraps=self.bridge.submit) as submit:
            self.adapter.pump_once()
            self.assertEqual(submit.call_args.args[0], before)

    def test_deleting_preference_takes_effect_on_next_message(self):
        item = self.receive('om_save_before_clear', '以后用中文回复')
        manage(self.groups, self.request(item, key='language', value='zh'))
        self.adapter.pump_once()
        task = self.inbox.linked_source(item['message_id'])['task_id']
        self.bridge.tasks[task] = {'status': '已完成', 'result': '已保存'}
        deletion = self.receive('om_clear_pref', '清除我的个人偏好')
        manage(self.groups, self.request(deletion, action='clear'))
        self.adapter.pump_once()
        task = self.inbox.linked_source(deletion['message_id'])['task_id']
        self.bridge.tasks[task] = {'status': '已完成', 'result': '已清除'}
        self.receive('om_follow_after_clear', '介绍一下这个模型')
        self.adapter.pump_once()
        prompt = self.bridge.submissions[-1][0]
        self.assertIn('当前限定偏好：{}', prompt)
        self.assertNotIn('"language": "zh"', prompt)

    def test_temporary_request_is_not_a_persistent_preference(self):
        item = self.receive('om_temporary_pref', '这次简短一点')
        self.adapter.pump_once()
        self.assertNotIn('personal-preference.py', self.bridge.submissions[0][0])
        self.assertIn('单次要求不写进业务规则', item['prompt'])
        self.assertFalse(json.loads(self.path.read_text()).get('rule_items'))


if __name__ == '__main__':
    unittest.main()
