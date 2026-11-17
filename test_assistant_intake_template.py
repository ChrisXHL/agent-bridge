import copy
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from pathlib import Path
from unittest.mock import patch

from assistant_intake import ROOT, IntakeError
from assistant_intake_template import apply_template_file


class TemplateAPIFake:
    def __init__(self, state, workspace):
        self.state = state
        self.workspace = workspace
        self.posts = []
        self.target = {'ref': 'app_abcdef1234567890', 'app_id': 'cli_target', 'name': '目标助手',
            'revision': 'one', 'migration_supported': True, 'enabled': True,
            'project_id': 'local-project', 'execution_backend': 'codex', 'allowed_open_ids': ['ou_target'],
            'prompt_template': '旧业务\n{{message}}\n运行规则：\n目标本机运行规则，保持身份。', 'self_update_rule': ''}
        self.groups = [{'app_ref': self.target['ref'], 'chat_id': 'oc_target', 'prompt_template': '群规则不改', 'revision': 'one'}]

    def call(self, path, data=None):
        if data is not None:
            self.posts.append((path, copy.deepcopy(data)))
            assert path == '/api/feishu/assistant-template/apply'
            self.target['prompt_template'] = data['bundle']['assistant']['prompt_template']
            self.target['self_update_rule'] = data['bundle']['assistant']['self_update_rule']
            self.target['revision'] = 'two'
            self.groups[0]['revision'] = 'two'
            return {'applied': True}
        if path == '/api/bridge-instance':
            return {'product': 'codex-bridge', 'source_root': str(ROOT), 'state_dir': str(self.state)}
        if path == '/api/feishu/catalog':
            return {'apps': [copy.deepcopy(self.target)], 'groups': copy.deepcopy(self.groups)}
        if path == '/api/projects':
            return [{'id': 'local-project', 'path': str(self.workspace)}]
        raise AssertionError(path)


class TemplateIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name).resolve()
        self.state = root / 'state';self.state.mkdir()
        self.workspace = root / 'workspace';self.workspace.mkdir()
        self.api = TemplateAPIFake(self.state, self.workspace)
        self.bundle = {'format': 'codex-bridge-assistant-template', 'version': 1, 'assistant': {
            'name': '源助手', 'prompt_template': '新业务规则\n{{message}}\n运行规则：\n源机器身份不能复制。',
            'self_update_rule': '列出事实依据', 'backend': 'claude', 'workspace_hint': '旧位置'}}
        self.args = {'base': 'http://127.0.0.1:8797', 'target_ref': self.api.target['ref'], 'api': self.api}

    def tearDown(self):self.temp.cleanup()

    def test_no_target_stops_before_api_or_filesystem(self):
        result = apply_template_file(self.bundle, 'http://127.0.0.1:8797', None)
        self.assertEqual(result['stage'], 'needs_target')
        self.assertEqual(self.api.posts, [])

    def test_inspect_is_read_only(self):
        before = list(self.state.iterdir())
        result = apply_template_file(self.bundle, **self.args, inspect=True)
        self.assertEqual(result['stage'], 'planned')
        self.assertEqual(self.api.posts, [])
        self.assertEqual(before, list(self.state.iterdir()))

    def test_apply_preserves_runtime_identity_backend_groups_and_is_idempotent(self):
        with patch('assistant_intake.backup') as backup:
            result = apply_template_file(self.bundle, **self.args)
            self.assertEqual(result['stage'], 'applied')
            backup.assert_called_once()
        prompt = self.api.target['prompt_template']
        self.assertIn('新业务规则', prompt)
        self.assertIn('目标本机运行规则', prompt)
        self.assertNotIn('源机器身份', prompt)
        self.assertEqual(self.api.target['execution_backend'], 'codex')
        self.assertEqual(self.api.groups[0]['prompt_template'], '群规则不改')
        self.assertTrue(self.api.target['enabled'])
        result = apply_template_file(self.bundle, **self.args)
        self.assertEqual(result['stage'], 'already_applied')
        self.assertEqual(len(self.api.posts), 1)

    def test_plain_business_template_keeps_target_runtime(self):
        self.bundle['assistant']['prompt_template'] = '只更新业务正文\n{{message}}'
        with patch('assistant_intake.backup'):
            result = apply_template_file(self.bundle, **self.args)
        self.assertEqual(result['stage'], 'applied')
        self.assertIn('目标本机运行规则', self.api.target['prompt_template'])

    def test_special_target_or_foreign_business_identity_is_not_written(self):
        self.api.target['migration_supported'] = False
        with self.assertRaises(IntakeError):apply_template_file(self.bundle, **self.args)
        self.api.target['migration_supported'] = True
        self.bundle['assistant']['prompt_template'] = '请给 ou_foreign 分享\n{{message}}'
        with self.assertRaises(IntakeError):apply_template_file(self.bundle, **self.args)
        self.assertEqual(self.api.posts, [])

    def test_unknown_target_does_not_guess_by_name(self):
        self.args['target_ref'] = 'app_0000000000000000'
        with self.assertRaises(IntakeError):apply_template_file(self.bundle, **self.args)
        self.assertEqual(self.api.posts, [])


class TemplateLiveAPITests(unittest.TestCase):
    def test_template_update_uses_real_existing_http_contract(self):
        import test_assistant_onboarding as fixtures
        from service import Engine, make_handler
        from test_service import FakeTransport
        from assistant_intake import intake
        fixture = fixtures.AssistantTests();fixture.setUp()
        server = None
        try:
            job = fixture.prepare();fixture.finish(job)
            manager = fixture.manager
            ready = manager.read(job['id'])
            server = ThreadingHTTPServer(('127.0.0.1', 0), None)
            base = 'http://127.0.0.1:' + str(server.server_port)
            server.RequestHandlerClass = make_handler(fixture.store, Engine(fixture.store, FakeTransport()), base,
                feishu_settings_path=manager.groups.config_path, feishu_inbox_path=manager.groups.inbox_path)
            thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
            def catalog():
                with urlopen(base + '/api/feishu/catalog') as response:return __import__('json').load(response)
            original = next(a for a in catalog()['apps'] if a['ref'] == ready['app_ref'])
            canonical_path = str(Path(original['project_path']).resolve())
            req = Request(base + '/api/projects/' + original['project_id'],
                          data=__import__('json').dumps({'path': canonical_path}).encode(),
                          headers={'Content-Type': 'application/json'}, method='PATCH')
            with urlopen(req) as response:response.read()
            bundle = {'format': 'codex-bridge-assistant-template', 'version': 1, 'assistant': {
                'name': '更新配置', 'prompt_template': '请核实事实并列出来源。\n{{message}}',
                'self_update_rule': '结论写清证据。', 'backend': 'codex', 'workspace_hint': ''}}
            file = fixture.root / 'template-input.json'
            file.write_text(__import__('json').dumps(bundle))
            with patch('assistant_intake.backup'):
                result = intake(file, base=base, target_ref=ready['app_ref'])
            self.assertEqual(result['stage'], 'applied')
            after = next(a for a in catalog()['apps'] if a['ref'] == ready['app_ref'])
            self.assertIn('请核实事实并列出来源', after['prompt_template'])
            self.assertIn('运行规则：', after['prompt_template'])
            self.assertEqual(after['enabled'], original['enabled'])
            self.assertEqual(after['app_id'], original['app_id'])
            self.assertEqual(after['project_id'], original['project_id'])
            self.assertEqual(after['allowed_open_ids'], original['allowed_open_ids'])
        finally:
            if server:
                server.shutdown();server.server_close();thread.join()
            fixture.tearDown()


if __name__ == '__main__':unittest.main()
