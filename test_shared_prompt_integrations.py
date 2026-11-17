"""Isolated creation/template/migration/delegation gates for shared prompt policy."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_assistant_onboarding as onboarding_fixtures
import test_assistant_manager as manager_fixtures
import test_assistant_intake_template as intake_fixtures
from assistant_collaboration import AssistantCollaboration
from assistant_intake import IntakeError
from assistant_intake_template import apply_template_file
from assistant_prompt import is_shared
from assistant_template import _validate_bundle, apply_template, export_template
from codex_http_bridge import BridgeError
from feishu_groups import GroupSettings, atomic_write, group_routes, revision
from feishu_migration import export_bundle, import_bundle
from feishu_settings import load
from service import Store


class SharedIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        workspace = self.root / 'workspace'; workspace.mkdir()
        atomic_write(self.root / 'projects.json', {'local': str(workspace)})
        self.store = Store(self.root / 'store.sqlite3', self.root / 'projects.json', self.root / 'no-state')
        connection = self.store.create_connection('助手', 'local')
        self.path = self.root / 'feishu.json'
        self.cfg = {'app_id': 'cli_aaaaaaaaaaaaaaaa', 'app_secret': 'isolated-secret',
            'app_name': '助手', 'connection_id': connection['id'], 'connection_key': connection['api_key'],
            'enabled': True, 'allowed_open_ids': ['ou_owner'], 'allowed_chat_ids': ['oc_one', 'oc_two'],
            'prompt_policy_version': 1, 'prompt_template': '共享业务 {{message}}',
            'self_update_rule': '保留事实来源', 'auto_group_owner_open_id': 'ou_owner',
            'group_configs': {chat: {'chat_id': chat, 'name': chat, 'connection_id': connection['id'],
                'enabled': True, 'topic_collaboration': False, 'prompt_template': '旧群专属 oc_secret {{message}}',
                'self_update_rule': '旧群追加'} for chat in ('oc_one', 'oc_two')}}
        atomic_write(self.path, self.cfg)
        self.groups = GroupSettings(self.path, self.root / 'inbox.sqlite3', self.store)
        self.bundle = {'format': 'codex-bridge-assistant-template', 'version': 1, 'assistant': {
            'name': '复制配置', 'prompt_template': '新业务 {{message}}', 'self_update_rule': '先讲结论',
            'backend': 'codex', 'workspace_hint': '', 'prompt_policy_version': 1}}

    def test_export_template_uses_canonical_fields_and_no_group_identity(self):
        bundle = export_template(self.groups, {'ref': 'default', 'revision': revision(self.cfg)})['bundle']
        assistant = bundle['assistant']
        self.assertEqual(assistant['prompt_template'], self.cfg['prompt_template'])
        self.assertEqual(assistant['self_update_rule'], self.cfg['self_update_rule'])
        self.assertTrue(is_shared(assistant))
        for private in ('oc_secret', 'oc_one', 'oc_two', 'ou_owner', 'isolated-secret', 'cli_aaaaaaaaaaaaaaaa'):
            self.assertNotIn(private, json.dumps(bundle))
        self.assertEqual(load(self.path), self.cfg)

    def test_shared_template_apply_updates_all_chats_but_preserves_route_boundaries(self):
        apply_template(self.groups, {'bundle': self.bundle, 'target_ref': 'default',
            'revision': revision(self.cfg), 'project_id': 'local'})
        after = load(self.path)
        self.assertEqual(after['prompt_template'], '新业务 {{message}}')
        self.assertEqual(after['self_update_rule'], '先讲结论')
        for chat, route in after['group_configs'].items():
            self.assertNotIn('prompt_template', route)
            self.assertNotIn('self_update_rule', route)
            self.assertEqual(route, {k: v for k, v in self.cfg['group_configs'][chat].items()
                                     if k not in ('prompt_template', 'self_update_rule')})
        for route in group_routes(after):
            self.assertEqual(route['prompt_template'], after['prompt_template'])
            self.assertEqual(route['self_update_rule'], after['self_update_rule'])
        for key in ('app_id', 'app_secret', 'allowed_open_ids', 'allowed_chat_ids', 'enabled'):
            self.assertEqual(after[key], self.cfg[key])

    def test_shared_source_template_never_migrates_legacy_target(self):
        legacy = copy.deepcopy(self.cfg); legacy.pop('prompt_policy_version')
        atomic_write(self.path, legacy)
        apply_template(self.groups, {'bundle': self.bundle, 'target_ref': 'default',
            'revision': revision(legacy), 'project_id': 'local'})
        after = load(self.path)
        self.assertNotIn('prompt_policy_version', after)
        self.assertEqual(after['group_configs'], legacy['group_configs'])
        self.assertEqual(after['prompt_template'], self.bundle['assistant']['prompt_template'])

    def test_template_optional_policy_rejects_wrong_type_and_unknown_version(self):
        for invalid in (True, '1', 0, 2, None):
            bundle = copy.deepcopy(self.bundle); bundle['assistant']['prompt_policy_version'] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(BridgeError):_validate_bundle(bundle)
        legacy = copy.deepcopy(self.bundle); legacy['assistant'].pop('prompt_policy_version')
        self.assertEqual(_validate_bundle(legacy), legacy['assistant'])

    def exported_migration(self):
        return export_bundle(self.groups, {'selected': [{'ref': 'default', 'revision': revision(self.cfg)}]})['bundle']

    def destination(self):
        return GroupSettings(self.root / 'destination' / 'feishu.json', self.root / 'destination' / 'inbox.sqlite3', self.store)

    def test_migration_preserves_policy_and_routes_without_stored_prompt_forks(self):
        bundle = self.exported_migration()
        self.assertEqual(bundle['apps'][0]['prompt_policy_version'], 1)
        target = self.destination()
        with patch('feishu_groups.probe', return_value={'bot_ready': True, 'bot_open_id': 'ou_bot', 'bot_name': '助手'}):
            result = import_bundle(target, {'bundle': bundle, 'project_id': 'local'})
        after = load(target.resolve(result['created'][0]['ref'])[1])
        self.assertTrue(is_shared(after)); self.assertFalse(after['enabled'])
        self.assertEqual(after['self_update_rule'], self.cfg['self_update_rule'])
        self.assertEqual(after['auto_group_owner_open_id'], 'ou_owner')
        self.assertEqual(after['allowed_chat_ids'], self.cfg['allowed_chat_ids'])
        self.assertEqual(after['migration_project_paths']['groups'].keys(), self.cfg['group_configs'].keys())
        for route in after['group_configs'].values():
            self.assertNotIn('prompt_template', route); self.assertNotIn('self_update_rule', route)
        self.assertEqual(load(self.path), self.cfg)

    def test_migration_invalid_policy_rejected_before_probe_or_write(self):
        for invalid in (True, '1', 0, 2, None):
            bundle = self.exported_migration(); bundle['apps'][0]['prompt_policy_version'] = invalid
            with self.subTest(invalid=invalid), patch('feishu_groups.probe') as probe:
                before = len(self.store.connections())
                with self.assertRaises(BridgeError):import_bundle(self.destination(), {'bundle': bundle, 'project_id': 'local'})
                probe.assert_not_called(); self.assertEqual(len(self.store.connections()), before)

    def test_shared_identity_intake_real_http_import_and_group_adaptation(self):
        from http.server import ThreadingHTTPServer
        import threading
        from assistant_intake import intake
        from service import Engine, make_handler
        from test_service import FakeTransport
        target = self.destination()
        target.config_path.parent.mkdir(parents=True)
        home = self.root / 'home'; home.mkdir()
        file = self.root / 'migration.json'; atomic_write(file, self.exported_migration())
        server = ThreadingHTTPServer(('127.0.0.1', 0), None)
        base = 'http://127.0.0.1:' + str(server.server_port)
        server.RequestHandlerClass = make_handler(self.store, Engine(self.store, FakeTransport()), base,
            feishu_settings_path=target.config_path, feishu_inbox_path=target.inbox_path)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with patch('assistant_intake.shutil.which', return_value='/isolated/bin/lark-cli'), \
                 patch('assistant_intake.backup'), \
                 patch('assistant_intake.backend_ready', return_value=True), \
                 patch('assistant_intake.run_cli', return_value=True), \
                 patch('feishu_groups.AccountIdentity.resolve', return_value={}), \
                 patch('feishu_groups.probe', return_value={'bot_ready': True, 'bot_open_id': 'ou_bot', 'bot_name': '助手'}):
                result = intake(file, base=base, home=home)
            self.assertEqual(result['stage'], 'prepared')
            self.assertTrue(result['results'][0]['verified'])
            imported_path = next((target.config_path.parent / 'feishu-apps').glob('*/config.json'))
            after = load(imported_path)
            self.assertTrue(is_shared(after)); self.assertFalse(after['enabled'])
            self.assertEqual(after['prompt_template'], self.cfg['prompt_template'])
            for group in after['group_configs'].values():
                self.assertNotIn('prompt_template', group); self.assertNotIn('self_update_rule', group)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_unflagged_migration_retains_legacy_group_rule(self):
        legacy = copy.deepcopy(self.cfg); legacy.pop('prompt_policy_version')
        atomic_write(self.path, legacy)
        bundle = export_bundle(self.groups, {'selected': [{'ref': 'default', 'revision': revision(legacy)}]})['bundle']
        self.assertNotIn('prompt_policy_version', bundle['apps'][0])
        target = self.destination()
        with patch('feishu_groups.probe', return_value={'bot_ready': True, 'bot_open_id': 'ou_bot', 'bot_name': '助手'}):
            result = import_bundle(target, {'bundle': bundle, 'project_id': 'local'})
        after = load(target.resolve(result['created'][0]['ref'])[1])
        self.assertNotIn('prompt_policy_version', after)
        self.assertEqual(after['group_configs']['oc_one']['self_update_rule'], '旧群追加')

    def test_delegation_inherits_shared_append_rule_without_publish_authority(self):
        job = {'name': '助手', 'requirements': '核实资料', 'profile': 'isolated-profile', 'plan': {'instructions': '备用'}}
        prompt = AssistantCollaboration.child_prompt(None, job, self.cfg, '明确的子任务')
        self.assertIn('共享业务', prompt); self.assertIn('保留事实来源', prompt)
        self.assertIn('不要向任何用户或群发消息，不创建/发布云端文档', prompt)
        self.assertNotIn('旧群追加', prompt); self.assertNotIn('{{message}}', prompt)
        legacy = copy.deepcopy(self.cfg); legacy.pop('prompt_policy_version')
        self.assertNotIn('助手共享追加规则', AssistantCollaboration.child_prompt(None, job, legacy, '子任务'))


class SharedCreationAndManagerTests(unittest.TestCase):
    def setUp(self):
        # No CLI runs: launch/registration are fixture fakes. Only advertise the
        # prerequisites required to enter the isolated creation state machine.
        available = patch('assistant_onboarding.shutil.which', return_value='/isolated/bin/tool')
        available.start(); self.addCleanup(available.stop)

    def test_new_business_and_existing_link_are_shared_and_scene_neutral(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                f = onboarding_fixtures.AssistantTests(); f.setUp()
                try:
                    if existing:
                        job = f.link()
                        atomic_write(f.manager.directory(job['id']) / 'credentials.json', onboarding_fixtures.CREDENTIAL)
                        f.manager.update(job['id'], profile_saved=True)
                        info = {'bot_ready': True, 'bot_open_id': 'ou_bot', 'bot_name': '助手'}
                        with patch('feishu_probe.probe', return_value=info), patch('feishu_groups.probe', return_value=info):
                            f.manager.finish(job['id'])
                    else:
                        job = f.prepare(audience='group'); f.finish(job)
                    live = f.manager.read(job['id']); cfg = load(f.manager.groups.resolve(live['app_ref'])[1])
                    self.assertTrue(is_shared(cfg)); self.assertEqual(cfg['auto_group_owner_open_id'], 'ou_creator')
                    self.assertNotIn('本连接仅允许创建者私聊', cfg['prompt_template'])
                    self.assertNotIn('本次消息来自已授权用户的私聊', cfg['prompt_template'])
                    self.assertEqual(cfg['prompt_template'].count('{{message}}'), 1)
                    self.assertEqual((f.root / 'feishu.json').read_bytes(), f.old)
                finally:f.tearDown()

    def test_special_dispatcher_remains_legacy(self):
        f = onboarding_fixtures.AssistantTests(); f.setUp()
        try:
            job = f.prepare(audience='group'); f.manager.update(job['id'], kind='dispatcher')
            f.finish(job); live = f.manager.read(job['id'])
            self.assertFalse(is_shared(load(f.manager.groups.resolve(live['app_ref'])[1])))
        finally:f.tearDown()

    def test_manager_updates_shared_purpose_across_multiple_groups(self):
        f = manager_fixtures.ManagerTests(); f.setUp()
        try:
            job = f.child(); path = f.onboard.groups.resolve(job['app_ref'])[1]; cfg = load(path)
            cfg.update(allowed_chat_ids=['oc_one', 'oc_two'], self_update_rule='保留用户追加规则',
                group_configs={chat: {'chat_id': chat, 'connection_id': cfg['connection_id'],
                    'prompt_template': '过期 {{message}}'} for chat in ('oc_one', 'oc_two')})
            atomic_write(path, cfg)
            f.onboard.update(job['id'], audience='group')
            request = f.receipt(text='修改用途')
            f.intent.update(action='update', target_id=job['id'], requirements='整理会议记录并提取待办')
            with patch('assistant_manager.plan_with_codex', return_value={**onboarding_fixtures.PLAN,
                        'instructions': '会议纪要新规则'}):f.manager.process(request['id'])
            after = load(path)
            self.assertEqual(f.manager.read(request['id'])['status'], 'done')
            self.assertIn('会议纪要新规则', after['prompt_template'])
            self.assertEqual(after['self_update_rule'], cfg['self_update_rule'])
            self.assertNotIn('本连接仅允许创建者私聊', after['prompt_template'])
            self.assertEqual(len(group_routes(after)), 2)
            for route in after['group_configs'].values():self.assertNotIn('prompt_template', route)
        finally:f.tearDown()


class SharedTemplateIntakeTests(unittest.TestCase):
    def test_intake_allows_only_expected_shared_group_updates_and_is_idempotent(self):
        f = intake_fixtures.TemplateIntakeTests(); f.setUp()
        try:
            f.api.target['prompt_policy_version'] = 1
            old_call = f.api.call
            def call(path, data=None):
                result = old_call(path, data)
                if data is not None:
                    f.api.groups[0].update(prompt_template=f.api.target['prompt_template'],
                        self_update_rule=f.api.target['self_update_rule'])
                return result
            f.api.call = call
            with patch('assistant_intake.backup'):
                result = apply_template_file(f.bundle, **f.args)
            self.assertEqual(result['stage'], 'applied')
            self.assertEqual(apply_template_file(f.bundle, **f.args)['stage'], 'already_applied')
            self.assertIn('目标本机运行规则', f.api.target['prompt_template'])
            self.assertNotIn('源机器身份', f.api.target['prompt_template'])
        finally:f.tearDown()

    def test_intake_shared_readback_still_rejects_route_changes(self):
        f = intake_fixtures.TemplateIntakeTests(); f.setUp()
        try:
            f.api.target['prompt_policy_version'] = 1
            old_call = f.api.call
            def call(path, data=None):
                result = old_call(path, data)
                if data is not None:
                    f.api.groups[0].update(prompt_template=f.api.target['prompt_template'],
                        self_update_rule=f.api.target['self_update_rule'], chat_id='oc_foreign')
                return result
            f.api.call = call
            with patch('assistant_intake.backup'), self.assertRaisesRegex(IntakeError, 'template_readback_needs_review'):
                apply_template_file(f.bundle, **f.args)
        finally:f.tearDown()


class SharedIdentityIntakeTests(unittest.TestCase):
    def setUp(self):
        import test_assistant_intake as fixtures
        self.f = fixtures.AssistantIntakeTests(); self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.f.app['prompt_policy_version'] = 1
        self.f.save()
        old_call = self.f.api.call
        def call(path, data=None):
            result = old_call(path, data)
            if path == '/api/feishu/migration/import' and data is not None:
                source = data['bundle']['apps'][0]
                target = self.f.api.apps[-1]
                target['prompt_policy_version'] = source['prompt_policy_version']
                for group in self.f.api.groups:
                    if group['app_ref'] == target['ref']:
                        group.update(prompt_template=target['prompt_template'],
                                     self_update_rule=target['self_update_rule'])
            return result
        self.f.api.call = call

    def test_identity_intake_preserves_flag_and_uses_only_canonical_group_business(self):
        self.f.app['prompt_template'] += ('\n- 飞书操作使用本应用 ' + self.f.app['app_id']
            + '，命令前缀为 /Users/source/bin/lark-cli --profile old --as bot。操作说明：'
            + '/Users/source/bridge/skills/codex-bridge/references/feishu-operations.md。不要输出密钥。')
        self.f.save()
        result = self.f.invoke()
        self.assertEqual(result['stage'], 'prepared')
        self.assertTrue(result['results'][0]['verified'])
        imported = next(data for path, data in self.f.api.calls if path.endswith('/migration/import'))['bundle']['apps'][0]
        self.assertEqual(imported['prompt_policy_version'], 1)
        self.assertNotIn('/Users/source/', imported['prompt_template'])
        for group in imported['groups']:
            self.assertEqual(group['prompt_template'], imported['prompt_template'])
            self.assertEqual(group['self_update_rule'], imported['self_update_rule'])
        for path, data in self.f.api.calls:
            if path == '/api/feishu/groups' and data is not None:
                self.assertEqual(data['prompt_template'], imported['prompt_template'])
        self.assertEqual(self.f.invoke(source_stopped=True)['stage'], 'ready')
        self.assertTrue(self.f.api.apps[0]['enabled'])

    def test_identity_intake_rejects_invalid_policy_before_any_api_call(self):
        for invalid in (True, '1', 0, 2, None):
            self.f.app['prompt_policy_version'] = invalid; self.f.save()
            with self.subTest(invalid=invalid), self.assertRaisesRegex(IntakeError, 'invalid_bundle'):
                self.f.invoke()
        self.assertEqual(self.f.api.calls, [])

    def test_identity_intake_policy_readback_mismatch_never_enables(self):
        original = self.f.api.call
        def call(path, data=None):
            result = original(path, data)
            if path == '/api/feishu/catalog':
                for app in result['apps']:app.pop('prompt_policy_version', None)
            return result
        self.f.api.call = call
        result = self.f.invoke(source_stopped=True)
        self.assertEqual(result['stage'], 'needs_agent_review')
        self.assertFalse(self.f.api.apps[0]['enabled'])
        self.assertNotIn('/api/feishu/apps/enabled', self.f.writes())


if __name__ == '__main__':unittest.main()
