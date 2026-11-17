"""Agent intake orchestration in temporary directories with a fake local API."""
import copy
import fcntl
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout

import assistant_intake as intake


class FakeAPI:
    def __init__(self, state):
        self.state = state
        self.apps, self.groups, self.projects, self.connections, self.calls = [], [], [], [], []
        self.fail_group_once = False
        self.import_uncertain = False

    def catalog(self):
        return copy.deepcopy({'apps': self.apps, 'groups': self.groups})

    def call(self, path, data=None):
        self.calls.append((path, copy.deepcopy(data)))
        if path == '/api/bridge-instance':
            return {'product': 'codex-bridge', 'source_root': str(intake.ROOT), 'state_dir': str(self.state)}
        if path == '/api/feishu/catalog':
            return self.catalog()
        if path == '/api/projects' and data is None:
            return copy.deepcopy(self.projects)
        if path == '/api/connections':
            return copy.deepcopy(self.connections)
        if path == '/api/projects':
            found = next((p for p in self.projects if p['path'] == data['path']), None)
            if found:
                return copy.deepcopy(found)
            result = {'id': 'project-' + str(len(self.projects)), 'path': data['path']}
            self.projects.append(result)
            return copy.deepcopy(result)
        if path == '/api/feishu/migration/import':
            app = data['bundle']['apps'][0]
            if any(a['app_id'] == app['app_id'] for a in self.apps):
                raise AssertionError('duplicate import')
            ref = 'app_' + ('%016x' % (len(self.apps) + 1))
            project = next(p for p in self.projects if p['id'] == data['project_id'])
            self.apps.append({'ref': ref, 'app_id': app['app_id'], 'revision': 'rev-0', 'enabled': False,
                'bot_ready': True, 'execution_backend': app['backend'], 'project_path': project['path'],
                'prompt_template': app['prompt_template'], 'self_update_rule': app.get('self_update_rule', ''),
                'allowed_open_ids': app['allowed_open_ids']})
            for i, group in enumerate(app['groups']):
                cid = ref + '-group-' + str(i)
                self.groups.append({**group, 'app_ref': ref, 'project_path': project['path'], 'connection_id': cid})
                self.connections.append({'id': cid, 'execution_backend': group['backend']})
            if self.import_uncertain:
                raise intake.IntakeError('local_api_failed')
            return {'created': [{'ref': ref}], 'enabled': False}
        if path == '/api/feishu/groups':
            if self.fail_group_once:
                self.fail_group_once = False
                raise intake.IntakeError('local_api_failed')
            app = next(a for a in self.apps if a['ref'] == data['app_ref'])
            assert app['revision'] == data['revision']
            group = next(g for g in self.groups if g['app_ref'] == app['ref'] and g['chat_id'] == data['chat_id'])
            group.update({k: data[k] for k in ('name', 'prompt_template', 'enabled', 'topic_collaboration')})
            group['project_path'] = next(p['path'] for p in self.projects if p['id'] == data['project_id'])
            app['revision'] += 'x'
            return self.catalog()
        if path == '/api/feishu/apps/enabled':
            app = next(a for a in self.apps if a['ref'] == data['app_ref'])
            assert app['revision'] == data['revision']
            app['enabled'] = data['enabled']
            app['revision'] += 'x'
            return self.catalog()
        raise AssertionError(path)


class AssistantIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir()
        (self.state / 'feishu.json').write_text('{"private":"state-secret"}')
        db = sqlite3.connect(self.state / 'bridge.sqlite3')
        db.execute('CREATE TABLE preserved(value TEXT)')
        db.execute("INSERT INTO preserved VALUES ('private-state')")
        db.commit()
        db.close()
        self.api = FakeAPI(self.state)
        self.app = {'app_id': 'cli_aaaaaaaaaaaaaaaa', 'app_secret': 'NEVER_PRINT_THIS_SECRET',
            'kind': 'standard', 'name': '资料助手', 'backend': 'codex',
            'prompt_template': '业务规则不改 {{message}}', 'self_update_rule': '先核实资料。',
            'allowed_open_ids': ['ou_creator'], 'project_path': '/Users/source/workspace',
            'groups': [self.group('oc_same', '/Users/source/workspace'),
                       self.group('oc_other', '/Users/source/group')]}
        self.bundle = {'format': intake.FORMAT, 'version': 2, 'apps': [self.app]}
        self.file = self.root / 'bundle.json'
        self.save()
        self.which = patch('assistant_intake.shutil.which', side_effect=lambda name: '/fake/bin/' + name)
        self.which.start()
        self.run = patch('assistant_intake.run_cli', return_value=True)
        self.runner = self.run.start()

    def tearDown(self):
        self.run.stop()
        self.which.stop()
        self.tmp.cleanup()

    def group(self, chat, source):
        return {'chat_id': chat, 'name': '测试群', 'project_path': source, 'backend': 'codex',
                'prompt_template': '群独立规则 {{message}}', 'enabled': True, 'topic_collaboration': False}

    def save(self):
        self.file.write_text(json.dumps(self.bundle, ensure_ascii=False))

    def invoke(self, **kwargs):
        return intake.intake(self.file, api=self.api, home=self.home, **kwargs)

    def writes(self):
        return [p for p, data in self.api.calls if data is not None]

    def test_existing_app_is_strict_noop_even_source_stopped(self):
        self.api.apps = [{'app_id': self.app['app_id'], 'ref': 'app_existing', 'enabled': False}]
        before = set(self.state.iterdir())
        for source_stopped in (False, True):
            result = self.invoke(source_stopped=source_stopped)
            self.assertEqual(result['stage'], 'already_present')
            self.assertFalse(result['results'][0]['enabled'])
            self.assertFalse(result['results'][0]['modified'])
            self.assertEqual(result['next_action'], 'agent_manage_existing')
        self.assertEqual(self.writes(), [])
        self.runner.assert_not_called()
        self.assertEqual(set(self.state.iterdir()), before)
        self.assertFalse((self.home / 'AgentBridge').exists())

    def test_inspect_is_read_only_without_lock_backup_or_profile(self):
        before = set(self.state.iterdir())
        result = self.invoke(inspect=True)
        self.assertEqual(result['stage'], 'planned')
        self.assertEqual(self.writes(), [])
        self.assertEqual(set(self.state.iterdir()), before)
        self.assertEqual(list(self.home.iterdir()), [])
        self.runner.assert_not_called()

    def test_new_import_is_disabled_maps_same_workspace_and_distinct_group(self):
        result = self.invoke()
        self.assertEqual(result['stage'], 'prepared')
        self.assertFalse(result['results'][0]['enabled'])
        self.assertTrue(result['results'][0]['verified'])
        self.assertEqual(result['next_action'], 'confirm_source_stopped')
        self.assertEqual(len(self.api.apps), 1)
        app_path = self.api.apps[0]['project_path']
        self.assertEqual(self.api.groups[0]['project_path'], app_path)
        self.assertNotEqual(self.api.groups[1]['project_path'], app_path)
        self.assertTrue(Path(app_path).is_relative_to(self.home / 'AgentBridge'))
        imported = next(data for path, data in self.api.calls if path.endswith('/migration/import'))
        self.assertEqual(imported['bundle']['apps'][0]['prompt_template'], self.app['prompt_template'])
        cli = next(c for c in self.runner.call_args_list if c.args[0][1:3] == ['config', 'init'])
        self.assertNotIn(self.app['app_secret'], ' '.join(cli.args[0]))
        self.assertEqual(cli.args[1], self.app['app_secret'])
        self.assertIn('--app-secret-stdin', cli.args[0])
        receipt = next((self.state / 'assistant-intake').glob('*.json'))
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.app['app_secret'], receipt.read_text())
        backups = list((self.home / 'Library/Application Support/BridgeIntakeBackups').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertFalse(backups[0].is_relative_to(self.state))
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o700)
        self.assertEqual((backups[0] / 'feishu.json').stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(backups[0] / 'bridge.sqlite3') as db:
            self.assertEqual(db.execute('SELECT value FROM preserved').fetchone()[0], 'private-state')
        count = len(self.writes())
        self.invoke()
        self.assertEqual(len(self.writes()), count)

    def test_owned_prepared_app_only_enables_after_explicit_confirmation(self):
        self.invoke()
        self.assertFalse(self.api.apps[0]['enabled'])
        result = self.invoke(source_stopped=True)
        self.assertTrue(self.api.apps[0]['enabled'])
        self.assertEqual(result['stage'], 'ready')
        count = len(self.writes())
        self.invoke(source_stopped=True)
        self.assertEqual(len(self.writes()), count)

    def test_partial_adaptation_resumes_only_matching_owned_revision(self):
        self.api.fail_group_once = True
        result = self.invoke()
        self.assertEqual(result['stage'], 'needs_agent_review')
        self.assertEqual(len(self.api.apps), 1)
        result = self.invoke()
        self.assertEqual(result['stage'], 'prepared')
        self.assertEqual(len(self.api.apps), 1)
        self.api.apps[0]['revision'] = 'user-edited'
        self.api.apps[0]['prompt_template'] = 'USER EDIT {{message}}'
        count = len(self.writes())
        result = self.invoke(source_stopped=True)
        self.assertIn('managed_settings_changed', result['warnings'])
        self.assertFalse(self.api.apps[0]['enabled'])
        self.assertEqual(len(self.writes()), count)

    def test_uncertain_import_preserves_app_without_claiming_or_duplicate(self):
        self.api.import_uncertain = True
        result = self.invoke()
        self.assertIn('import_outcome_uncertain_preserved', result['warnings'])
        self.assertEqual(len(self.api.apps), 1)
        count = len(self.writes())
        self.invoke(source_stopped=True)
        self.assertEqual(len(self.writes()), count)
        self.assertFalse(self.api.apps[0]['enabled'])

    def test_unknown_paths_or_missing_backend_never_enable(self):
        self.app['prompt_template'] += '\n请读取 /Users/source/private/unknown.txt'
        self.save()
        result = self.invoke(source_stopped=True)
        self.assertEqual(result['stage'], 'needs_agent_review')
        self.assertIn('legacy_paths_need_agent_review', result['warnings'])
        self.assertFalse(self.api.apps[0]['enabled'])

    def test_strict_bundle_limits_and_duplicate_json_keys(self):
        for bundle in ([], {'format': 'anything'}, {**self.bundle, 'version': True},
                       {**self.bundle, 'apps': [self.app, self.app]},
                       {**self.bundle, 'apps': [{**self.app, 'command': 'execute-me'}]}):
            self.file.write_text(json.dumps(bundle))
            with self.assertRaises(intake.IntakeError):
                self.invoke()
        self.file.write_text('{"format":"a","format":"b"}')
        with self.assertRaises(intake.IntakeError):
            self.invoke()
        self.file.write_bytes(b'x' * (intake.MAX_BYTES + 1))
        with self.assertRaises(intake.IntakeError):
            self.invoke()
        self.assertEqual(self.api.calls, [])

    def test_template_and_legacy_are_identified_without_writes(self):
        self.file.write_text(json.dumps({'format': intake.FORMAT, 'version': 1, 'data': 'not-disclosed'}))
        self.assertEqual(self.invoke()['stage'], 'needs_password')
        self.file.write_text(json.dumps({'format': 'codex-bridge-assistant-template', 'version': 1,
            'assistant': {'name': '模板', 'prompt_template': '{{message}}', 'self_update_rule': '',
                          'backend': 'codex', 'workspace_hint': 'source'}}))
        self.assertEqual(self.invoke()['stage'], 'needs_target')
        self.assertEqual(self.api.calls, [])
        self.runner.assert_not_called()

    def test_url_instance_profile_and_symlink_checks_fail_closed(self):
        for url in ('https://127.0.0.1:8797', 'http://evil.test:8797', 'http://127.0.0.1:8797/path',
                    'http://user:secret@localhost:8797', 'http://localhost:8797?x=1'):
            with self.assertRaises(intake.IntakeError):
                intake.bridge_url(url)
        with patch.object(self.api, 'call', return_value={'product': 'other'}):
            with self.assertRaises(intake.IntakeError):
                self.invoke()
        folder = self.home / '.lark-cli'
        folder.mkdir()
        (folder / 'config.json').write_text(json.dumps({'apps': [
            {'name': 'bridge-import-' + self.app['app_id'], 'appId': 'cli_other'}]}))
        result = self.invoke()
        self.assertIn('cli_profile_conflict', result['warnings'])
        self.assertEqual(self.writes(), [])
        link = self.state / 'assistant-intake'
        target = self.root / 'external'
        target.mkdir()
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(intake.IntakeError):
            self.invoke()

    def test_private_lock_rejects_parallel_intake(self):
        with intake.lock(self.state):
            with self.assertRaises(intake.IntakeError) as error:
                self.invoke()
            self.assertEqual(error.exception.code, 'intake_busy')
        self.assertEqual(self.writes(), [])

    def test_known_runtime_adaptation_keeps_business_text(self):
        old = ('BUSINESS /Users/source/unknown.txt {{message}}\n'
            '- 飞书操作使用本应用 ' + self.app['app_id'] + '，命令前缀为 '
            '/Users/source/bridge/.tools/node_modules/.bin/lark-cli --profile old-profile --as bot。'
            '操作说明：/Users/source/bridge/skills/codex-bridge/references/feishu-operations.md。先阅读说明。')
        adapted, review = intake.adapt_prompt(old, self.app['app_id'], '/fake/bin/lark-cli', 'local-profile')
        self.assertIn('BUSINESS /Users/source/unknown.txt {{message}}', adapted)
        self.assertIn('/fake/bin/lark-cli --profile local-profile --as bot', adapted)
        self.assertNotIn('--profile old-profile', adapted)
        self.assertTrue(review)

    def test_packet_paths_are_hints_not_permission_to_reuse_home_directories(self):
        sensitive = self.home / '.ssh'
        sensitive.mkdir()
        self.app['project_path'] = str(sensitive)
        self.app['groups'][0]['project_path'] = str(sensitive)
        self.save()
        result = self.invoke()
        self.assertEqual(result['stage'], 'prepared')
        self.assertNotEqual(self.api.apps[0]['project_path'], str(sensitive))
        self.assertEqual(list(sensitive.iterdir()), [])
        text, review = intake.adapt_prompt('读取 ' + str(sensitive) + ' {{message}}',
                                          self.app['app_id'], '/fake/bin/lark-cli', 'local-profile')
        self.assertTrue(review)
        self.assertIn(str(sensitive), text)

    def test_symlink_cli_profile_and_existing_workspace_are_not_overwritten(self):
        outside = self.root / 'outside-cli'
        outside.mkdir()
        (self.home / '.lark-cli').symlink_to(outside, target_is_directory=True)
        result = self.invoke()
        self.assertIn('unsafe_symlink_path', result['warnings'])
        self.assertEqual(self.writes(), [])
        self.runner.assert_not_called()

    def test_main_never_prints_secret_or_exception_arguments(self):
        output = io.StringIO()
        with patch('assistant_intake.intake', side_effect=RuntimeError(self.app['app_secret'])):
            with redirect_stdout(output):
                result = intake.main([str(self.file)])
        self.assertEqual(result, 1)
        self.assertNotIn(self.app['app_secret'], output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['warnings'], ['agent_fix_intake'])


if __name__ == '__main__':
    unittest.main()
