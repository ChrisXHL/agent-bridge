"""Itemized shared rules: direct, audited changes from trusted Feishu messages."""
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from assistant_rules import archive_all, rules_text
from assistant_self_update import applied_rule, capability, explicit_request, instruction, update
from codex_http_bridge import BridgeError
from feishu_groups import atomic_write
from feishu_settings import load
from feishu_worker import FeishuAdapter, Inbox
from test_feishu import FakeBridge, FakeSender, event


class RuleItemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.path = root / 'feishu.json'
        self.inbox = Inbox(root / 'inbox.sqlite3')
        self.groups = SimpleNamespace(config_path=self.path, inbox_path=self.inbox.path)
        self.config = {
            'enabled': True, 'app_id': 'cli_shared', 'connection_key': 'test-key',
            'connection_id': 'con_default', 'bot_open_id': 'ou_bot',
            'auto_group_owner_open_id': 'ou_owner', 'allowed_open_ids': ['ou_owner'],
            'allowed_chat_ids': ['oc_team'], 'prompt_policy_version': 1,
            'prompt_template': 'CANONICAL {{message}}', 'self_update_rule': 'LEGACY_PARAGRAPH',
            'group_configs': {'oc_team': {'enabled': True, 'connection_id': 'con_group'}},
        }
        atomic_write(self.path, self.config)
        self.adapter = FeishuAdapter(self.config, self.inbox, FakeBridge(), FakeSender())

    def tearDown(self):
        self.tmp.cleanup()

    def receive(self, mid, text, sender='ou_member', group=True):
        payload = event(mid, sender=sender, chat='oc_team' if group else 'oc_dm',
                        chat_type='group' if group else 'p2p', text=text,
                        mentions=[{'id': {'open_id': 'ou_bot'}, 'key': '@bot'}] if group else None)
        self.assertTrue(self.adapter.handle_event(payload))
        return self.inbox.linked_source(mid)

    def ops(self, mid, ops, sender='ou_member'):
        return update(self.groups, {'source_id': mid, 'token': capability(self.config, mid, sender), 'ops': ops})

    def listing(self, mid, sender='ou_member'):
        return update(self.groups, {'source_id': mid, 'token': capability(self.config, mid, sender), 'list': True})

    def saved(self):
        return load(self.path)

    def test_instruction_offers_item_operations_and_prompt_lists_items(self):
        item = self.receive('om_rule_r1', '记住：周报先写结论')
        self.assertIn('--list', item['prompt'])
        self.assertIn('--add', item['prompt'])
        self.assertNotIn("--rule '合并旧规则后的完整新规则'", item['prompt'])
        result = self.ops('om_rule_r1', [{'op': 'add', 'text': '周报先写结论，再写细节。'}])
        self.assertEqual(result['results'], [{'op': 'add', 'id': 1}])
        saved = self.saved()
        self.assertEqual(saved['rule_items'][0]['author'], 'ou_member')
        self.assertEqual(saved['rule_items'][0]['chat_id'], 'oc_team')
        self.assertEqual(saved['self_update_rule'], 'LEGACY_PARAGRAPH')
        rendered = applied_rule(saved, 'oc_team')
        self.assertIn('LEGACY_PARAGRAPH', rendered)
        self.assertIn('1. 周报先写结论，再写细节。', rendered)
        # Rules are shared across the assistant, private chat included.
        self.assertEqual(rendered, applied_rule(saved))
        self.adapter.config = saved
        follow = self.receive('om_rule_r_next', '整理资料', sender='ou_owner', group=False)
        self.assertIn('1. 周报先写结论，再写细节。', follow['prompt'])

    def test_exact_duplicate_is_not_added_twice(self):
        self.receive('om_rule_d1', '记住：周报先写结论')
        self.ops('om_rule_d1', [{'op': 'add', 'text': '周报先写结论。'}])
        self.receive('om_rule_d2', '记住，周报先写结论', sender='ou_other')
        result = self.ops('om_rule_d2', [{'op': 'add', 'text': '周报 先写结论'}], sender='ou_other')
        self.assertEqual(result['results'], [{'op': 'add', 'duplicate_of': 1}])
        self.assertFalse(result['updated'])
        self.assertEqual(len(self.saved()['rule_items']), 1)
        retry = self.ops('om_rule_d2', [{'op': 'add', 'text': '周报 先写结论'}], sender='ou_other')
        self.assertTrue(retry['already_applied'])
        self.assertFalse(retry['updated'])
        self.assertEqual(retry['results'], result['results'])

    def test_direct_group_edit_across_authors_and_atomic_batch(self):
        self.receive('om_rule_a1', '记住：表格只列关键数字')
        self.ops('om_rule_a1', [{'op': 'add', 'text': '表格只列关键数字。'}])
        self.receive('om_rule_a2', '把第1条改成表格要列全部数字', sender='ou_other')
        with self.assertRaises(BridgeError) as error:
            self.ops('om_rule_a2', [{'op': 'replace', 'id': 1, 'text': '表格要列全部数字。'},
                                   {'op': 'remove', 'id': 99}], sender='ou_other')
        self.assertEqual(error.exception.code, 'rule_not_found')
        self.assertEqual([i['text'] for i in self.saved()['rule_items']], ['表格只列关键数字。'])
        result = self.ops('om_rule_a2', [{'op': 'replace', 'id': 1, 'text': '表格要列全部数字。'}],
                          sender='ou_other')
        self.assertTrue(result['updated'])
        self.assertEqual(result['rules'][0]['text'], '表格要列全部数字。')
        first = self.saved()['rule_items'][0]
        self.assertEqual(first['author'], 'ou_member')
        self.assertEqual(first['updated_by'], 'ou_other')
        self.assertEqual(first['history'][0]['text'], '表格只列关键数字。')
        self.receive('om_rule_a3', '删掉第1条', sender='ou_owner', group=False)
        self.ops('om_rule_a3', [{'op': 'remove', 'id': 1}], sender='ou_owner')
        saved = self.saved()
        self.assertEqual(saved['rule_items'], [])
        self.assertEqual(saved['rule_items_removed'][0]['id'], 1)
        self.receive('om_rule_a4', '记住：结论放第一行')
        self.assertEqual(self.ops('om_rule_a4', [{'op': 'add', 'text': '结论放第一行。'}])['results'][0]['id'], 2)

    def test_natural_rule_change_with_caption_and_fresh_queued_and_next_prompt(self):
        self.adapter.config_path = self.path
        self.receive('om_rule_media_old', '记住：从第一电动取信源')
        self.ops('om_rule_media_old', [{'op': 'add', 'text': '从第一电动取信源。'}])
        queued = self.receive('om_rule_media_queued', '开始扫描', sender='ou_owner', group=False)
        self.assertIn('从第一电动取信源。', queued['prompt'])
        payload = event('om_rule_media_new', sender='ou_member', chat='oc_team', chat_type='group',
                        mentions=[{'id': {'open_id': 'ou_bot'}, 'key': '@bot'}], message_type='post')
        payload['event']['message']['content'] = json.dumps({'content': [[
            {'tag': 'text', 'text': '第一电动拉黑不看'},
            {'tag': 'img', 'image_key': 'img_example'},
        ]]})
        self.assertTrue(self.adapter.handle_event(payload))
        row = self.inbox.linked_source('om_rule_media_new')
        self.assertIn('--replace', row['prompt'])
        self.assertNotIn('--propose', row['prompt'])
        result = self.ops('om_rule_media_new', [{'op': 'replace', 'id': 1, 'text': '屏蔽第一电动及其转述。'}])
        self.assertTrue(result['updated'])
        self.assertEqual(result['rules'][0]['text'], '屏蔽第一电动及其转述。')
        self.adapter.media.prepare = lambda item: item['prompt'] if item['message_id'] == queued['message_id'] else None
        self.adapter.pump_once()
        prompt = self.adapter.bridge.submissions[-1][0]
        self.assertIn('排队期间规则已更新', prompt)
        self.assertIn('屏蔽第一电动及其转述。', prompt)
        next_message = self.receive('om_rule_media_next', '开始扫描')
        self.assertIn('屏蔽第一电动及其转述。', next_message['prompt'])
        self.assertNotIn('从第一电动取信源。', next_message['prompt'])

    def test_contextual_reply_can_directly_update_existing_rule(self):
        self.adapter.config_path = self.path
        self.receive('om_context_add', '记住：第一电动列入长期信源', sender='ou_owner', group=False)
        self.ops('om_context_add', [{'op': 'add', 'text': '长期信源包括第一电动。'}], sender='ou_owner')
        self.adapter.pump_once()
        previous = self.inbox.linked_source('om_context_add')
        tid = previous['task_id']
        self.adapter.bridge.tasks[tid].update(status='已完成', result='第一电动已列入长期信源。',
                                              thread_id='same_thread', execution_backend='codex')
        self.receive('om_context_remove', '把它去掉', sender='ou_owner', group=False)
        self.adapter.pump_once()
        prompt, _ = self.adapter.bridge.submissions[-1]
        self.assertIn('把它去掉', prompt)
        self.assertIn('--remove', prompt)
        self.assertEqual(self.adapter.bridge.continuations[-1], tid)
        result = self.ops('om_context_remove', [{'op': 'remove', 'id': 1}], sender='ou_owner')
        self.assertTrue(result['updated'])
        following = self.receive('om_context_check', '开始扫描')
        self.assertNotIn('长期信源包括第一电动。', following['prompt'])

    def test_listing_marks_own_items_and_does_not_consume_the_entry(self):
        self.receive('om_rule_l1', '记住：先给结论')
        self.ops('om_rule_l1', [{'op': 'add', 'text': '先给结论再展开。'}])
        self.receive('om_rule_l2', '记住：回复要短', sender='ou_other')
        listed = self.listing('om_rule_l2', sender='ou_other')
        self.assertEqual(listed['rules'][0]['mine'], False)
        self.assertTrue(listed['has_legacy_rule'])
        result = self.ops('om_rule_l2', [{'op': 'add', 'text': '回复尽量简短。'}], sender='ou_other')
        self.assertTrue(result['rules'][1]['mine'])
        self.assertTrue(self.ops('om_rule_l2', [{'op': 'add', 'text': '回复尽量简短。'}],
                                 sender='ou_other')['already_applied'])
        with self.assertRaises(BridgeError):
            self.ops('om_rule_l2', [{'op': 'add', 'text': '换成另一条规则。'}], sender='ou_other')

    def test_private_chat_is_owner_only_and_legacy_assistants_keep_paragraph_mode(self):
        self.receive('om_rule_p1', '记住：用中文回答', sender='ou_owner', group=False)
        self.assertEqual(self.ops('om_rule_p1', [{'op': 'add', 'text': '一律用中文回答。'}], sender='ou_owner')['results'],
                         [{'op': 'add', 'id': 1}])
        self.assertEqual(self.saved()['rule_items'][0]['chat_type'], 'p2p')
        legacy = dict(self.saved())
        legacy.pop('prompt_policy_version')
        atomic_write(self.path, legacy)
        self.adapter.config = legacy
        item = self.receive('om_rule_p2', '记住：用英文回答', sender='ou_owner', group=False)
        self.assertIn("--rule '合并旧规则后的完整新规则'", item['prompt'])
        with self.assertRaises(BridgeError) as error:
            self.ops('om_rule_p2', [{'op': 'add', 'text': '一律用英文回答。'}], sender='ou_owner')
        self.assertEqual(error.exception.code, 'rule_items_unsupported')

    def test_numbered_requests_are_rule_updates(self):
        for text in ('删掉第3条', '请删除第 2 条规则', '把第2条改成先写结论', '修改第十条'):
            self.assertTrue(explicit_request(text), text)
        self.assertFalse(explicit_request('第3条新闻讲了什么'))

    def test_exports_include_items_and_template_apply_archives_them(self):
        config = {**self.config, 'rule_items': [{'id': 1, 'text': '先给结论。', 'author': 'ou_member'}]}
        self.assertEqual(rules_text(config), 'LEGACY_PARAGRAPH\n\n逐条保存的规则：\n1. 先给结论。')
        archive_all(config, 'template_apply')
        self.assertEqual(config['rule_items'], [])
        self.assertEqual(config['rule_items_removed'][0]['removed_by'], 'template_apply')
        self.assertEqual(rules_text(config), 'LEGACY_PARAGRAPH')


class RuleCliTests(unittest.TestCase):
    def run_cli(self, *args):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                body = b'{"updated": true}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            script = Path(__file__).resolve().parent / 'scripts' / 'update-assistant-self.py'
            done = subprocess.run([sys.executable, str(script), '--source-id', 'om_x', '--token', 'a' * 64,
                                   '--bridge-url', f'http://127.0.0.1:{server.server_port}', *args],
                                  capture_output=True, text=True, timeout=30)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        return done, seen

    def test_cli_builds_item_operations(self):
        done, seen = self.run_cli('--add', '规则甲内容。', '--add', '规则乙内容。',
                                  '--replace', '2', '新的第二条。', '--remove', '3')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(seen[0]['ops'], [{'op': 'replace', 'id': 2, 'text': '新的第二条。'},
                                          {'op': 'remove', 'id': 3, 'text': ''},
                                          {'op': 'add', 'text': '规则甲内容。'},
                                          {'op': 'add', 'text': '规则乙内容。'}])
        done, seen = self.run_cli('--list')
        self.assertEqual(seen[0], {'source_id': 'om_x', 'token': 'a' * 64, 'list': True})
        done, seen = self.run_cli('--rule', '旧版整段规则。')
        self.assertEqual(seen[0]['rule'], '旧版整段规则。')
        done, _ = self.run_cli('--list', '--add', '冲突的参数组合。')
        self.assertNotEqual(done.returncode, 0)


if __name__ == '__main__':
    unittest.main()
