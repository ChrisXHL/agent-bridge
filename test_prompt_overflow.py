"""Long runtime context must survive intake, attachment preparation and retries."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from feishu_settings import bounded_task_prompt, render_prompt
from feishu_worker import FeishuAdapter, Inbox
from test_feishu import FakeBridge, FakeSender, event


class PromptOverflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.inbox = Inbox(self.root / 'inbox.sqlite3')
        self.config = {'app_id': 'cli_test', 'connection_id': 'conn_test',
                       'allowed_open_ids': ['ou_owner'], 'allowed_chat_ids': ['oc_team'],
                       'bot_open_id': 'ou_bot', 'prompt_template': '规则' * 1900 + '{{message}}',
                       'self_update_rule': '通用' * 790,
                       'group_configs': {'oc_team': {'self_update_rule': '群内' * 790}}}
        self.bridge, self.sender = FakeBridge(), FakeSender()
        self.adapter = FeishuAdapter(self.config, self.inbox, self.bridge, self.sender)

    def tearDown(self):
        self.tmp.cleanup()

    def receive(self, mid='om_long', text='先检查哪些没完成', group=True, **kwargs):
        return self.adapter.handle_event(event(mid, text=('@_user_1 ' if group else '') + text,
            chat='oc_team' if group else 'oc_private', chat_type='group' if group else 'p2p',
            mentions=[{'id': {'open_id': 'ou_bot'}, 'key': '@_user_1'}] if group else [], **kwargs))

    def requests(self):
        return [json.loads(p.read_text())['current_request']
                for p in (self.root / 'conversations').glob('*.request.json')]

    def test_saved_global_and_group_rules_preserved_and_acked(self):
        self.assertTrue(self.receive())
        source = self.inbox.linked_source('om_long')
        self.assertEqual(source['ack_status'], 'sent')
        self.assertIn('规则' * 1900, self.requests()[0])
        self.assertIn('通用' * 790, self.requests()[0])
        self.assertIn('群内' * 790, self.requests()[0])
        self.assertIn('先检查哪些没完成', self.requests()[0])
        self.adapter.pump_once()
        self.assertEqual(len(self.bridge.submissions), 1)
        self.assertLess(len(json.dumps({'message': self.bridge.submissions[0][0]}, ensure_ascii=False).encode()), 16384)
        self.assertFalse(self.receive())
        self.adapter.pump_once()
        self.assertEqual(len(self.bridge.submissions), 1)
        for p in (self.root / 'conversations').glob('*.request.json'):
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_editor_limit_does_not_apply_to_augmented_template(self):
        self.config['prompt_template'] = 'a' * 7000 + '{{message}}'
        self.config['self_update_rule'] = 'b' * 1500
        self.assertTrue(self.receive(group=False))
        self.adapter.pump_once()
        self.assertEqual(len(self.bridge.submissions), 1)
        self.assertIn('b' * 1500, self.bridge.submissions[0][0])

    def test_group_late_context_spills_and_retry_is_frozen(self):
        self.config['prompt_template'] = '{{message}}'
        self.config.pop('self_update_rule')
        self.config['group_configs'] = {}
        self.assertTrue(self.receive())
        self.bridge.fail_next = True
        with patch('personal_preferences.context', return_value='后加规则' * 6000):
            self.adapter.pump_once()
        saved = self.inbox.private_submission('group:om_long')
        self.assertIn('后加规则' * 6000, self.requests()[0])
        with patch('personal_preferences.context', return_value='changed'):
            self.adapter.pump_once()
        self.assertEqual(self.bridge.submissions[0][0], saved['message'])
        self.assertEqual(len(self.bridge.submissions), 1)

    def test_attachment_context_survives_long_runtime_rules(self):
        self.sender.download_resource = lambda mid, key, kind, out, limit: out.write(b'image-fixture')
        payload = event('om_media', chat='oc_team', chat_type='group', message_type='post',
            mentions=[{'id': {'open_id': 'ou_bot'}, 'key': '@_user_1'}])
        payload['event']['message']['content'] = json.dumps({'content': [[
            {'tag': 'text', 'text': '@_user_1 看图'}, {'tag': 'img', 'image_key': 'img_test'}]]})
        self.assertTrue(self.adapter.handle_event(payload))
        self.adapter.pump_once()
        self.assertEqual(len(self.bridge.submissions), 1)
        media = self.adapter.media.record('om_media')
        self.assertEqual(media['state'], 'ready')
        self.assertTrue(Path(json.loads(media['manifest'])[0]['path']).is_file())
        prepared = media['prepared_prompt']
        self.assertTrue('媒体附件' in prepared or any('媒体附件' in s for s in self.requests()))

    def test_reply_to_completed_group_result_uses_current_long_rules(self):
        self.config['prompt_template'] = 'old {{message}}'
        self.config.pop('self_update_rule')
        self.config['group_configs'] = {}
        self.assertTrue(self.receive(mid='om_first'))
        self.adapter.pump_once()
        tid = self.inbox.linked_source('om_first')['task_id']
        self.bridge.tasks[tid] = {'status': '已完成', 'result': '读完了'}
        self.adapter.pump_once()
        parent = self.inbox.linked_source('om_first')['outbound_id']
        self.config['self_update_rule'] = '通用' * 790
        self.config['group_configs'] = {'oc_team': {'self_update_rule': '群内' * 790}}
        # A valid frozen base plus new global/group rules must not drop replies.
        with self.inbox.db() as db:
            row = db.execute('SELECT route_json FROM inbound WHERE message_id=?', ('om_first',)).fetchone()
            route = json.loads(row[0]);route['prompt_template'] = '规则' * 1900 + '{{message}}'
            db.execute('UPDATE inbound SET route_json=? WHERE message_id=?', (json.dumps(route), 'om_first'))
        self.assertTrue(self.receive(mid='om_follow', parent=parent))
        self.adapter.pump_once()
        self.assertEqual(self.inbox.linked_source('om_follow')['task_id'], tid)
        self.assertEqual(len(self.bridge.submissions), 2)
        self.assertTrue(any('通用' * 790 in s and '群内' * 790 in s for s in self.requests()))

    def test_unauthorized_long_requests_stay_silent(self):
        self.assertFalse(self.receive(group=False, sender='ou_other'))
        self.assertEqual(self.sender.sent, [])
        self.assertEqual(self.inbox.pending(), [])
        self.assertFalse((self.root / 'conversations').exists())

    def test_short_render_and_public_validation_still_enforce_contract(self):
        self.assertEqual(render_prompt('Task: {{message}}', 'hi'), 'Task: hi')
        from codex_http_bridge import BridgeError
        with self.assertRaises(BridgeError):
            render_prompt('{{message}}', '中' * 6000)
        self.assertEqual(bounded_task_prompt('short', self.inbox.path, 'om_x'), 'short')

    def test_content_addressed_files_do_not_overwrite_referenced_request(self):
        first = bounded_task_prompt('甲' * 6000, self.inbox.path, 'om_same')
        second = bounded_task_prompt(first + '乙' * 6000, self.inbox.path, 'om_same')
        self.assertNotEqual(first, second)
        self.assertEqual(len(self.requests()), 2)
        self.assertIn('甲' * 6000, self.requests())
        self.assertIn(first + '乙' * 6000, self.requests())


if __name__ == '__main__':
    unittest.main()
