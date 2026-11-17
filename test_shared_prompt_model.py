"""Canonical prompt model and controller writes; no live services or credentials."""
import copy
import unittest
from unittest.mock import patch

from assistant_prompt import (is_shared, business_template, sync_shared,
                              neutral_template, migrate_config, delivery_context)
from codex_http_bridge import BridgeError
from feishu_groups import GroupSettings, group_routes, atomic_write, revision
from feishu_settings import load, save
from test_feishu_scenarios import FeishuScenarioTests


class SharedPromptModelTests(unittest.TestCase):
    def config(self):
        return {'prompt_template': 'PRIVATE {{message}}', 'self_update_rule': '旧通用规则',
                'allowed_chat_ids': ['oc_a'], 'group_configs': {'oc_a': {
                    'prompt_template': 'GROUP {{message}}', 'self_update_rule': '以群规则为准',
                    'connection_id': 'conn_group', 'enabled': True}}}

    def test_migration_group_primary_one_copy_and_no_mutation(self):
        cfg = self.config();before = copy.deepcopy(cfg)
        shared = migrate_config(cfg)
        self.assertEqual(cfg, before)
        self.assertTrue(is_shared(shared))
        self.assertTrue(shared['prompt_template'].startswith('GROUP {{message}}'))
        self.assertIn('旧通用规则', shared['prompt_template'])
        self.assertEqual(shared['self_update_rule'], '以群规则为准')
        self.assertEqual(shared['group_configs']['oc_a'], {'connection_id': 'conn_group', 'enabled': True})
        self.assertEqual(group_routes(shared)[0]['prompt_template'], shared['prompt_template'])

    def test_conflicting_groups_require_explicit_source(self):
        cfg = self.config();cfg['allowed_chat_ids'].append('oc_b')
        cfg['group_configs']['oc_b'] = {'prompt_template': 'OTHER {{message}}'}
        with self.assertRaises(BridgeError) as e:migrate_config(cfg)
        self.assertEqual(e.exception.code, 'prompt_source_ambiguous')
        shared = migrate_config(cfg, source_chat_id='oc_a')
        self.assertTrue(shared['prompt_template'].startswith('GROUP {{message}}'))
        self.assertIn('旧通用规则', shared['prompt_template'])
        self.assertTrue(all('prompt_template' not in g for g in shared['group_configs'].values()))

    def test_missing_group_override_participates_in_conflict_detection(self):
        cfg = self.config();cfg['allowed_chat_ids'].append('oc_unconfigured')
        with self.assertRaises(BridgeError) as e:migrate_config(cfg)
        self.assertEqual(e.exception.code, 'prompt_source_ambiguous')

    def test_legacy_fallback_and_shared_frozen_override(self):
        cfg = self.config()
        self.assertEqual(business_template(cfg, 'FROZEN {{message}}'), 'FROZEN {{message}}')
        cfg['prompt_policy_version'] = 1
        self.assertEqual(business_template(cfg, 'FROZEN {{message}}'), 'PRIVATE {{message}}')
        sync_shared(cfg, 'NEW {{message}}')
        self.assertEqual(cfg['prompt_template'], 'NEW {{message}}')
        self.assertNotIn('prompt_template', cfg['group_configs']['oc_a'])

    def test_neutralization_and_current_scene_are_separate(self):
        base = 'BUSINESS {{message}}\n- 本任务来自已绑定的群。用 Bridge 末尾可信来源中的 chat_id，给新资料加权限。\nKEEP custom'
        common = neutral_template(base)
        self.assertIn('BUSINESS', common);self.assertIn('KEEP custom', common)
        self.assertNotIn('本任务来自', common)
        cfg = {'prompt_policy_version': 1}
        dm = delivery_context(cfg, 'p2p', 'oc_dm', 'ou_member')
        group = delivery_context(cfg, 'group', 'oc_team', 'ou_member')
        self.assertIn('open_id 为 ou_member', dm);self.assertNotIn('chat_id 为 oc_team', dm)
        self.assertIn('chat_id 为 oc_team', group)
        self.assertEqual(delivery_context({}, 'p2p', 'oc_dm', 'ou_member'), '')


class SharedPromptControllerTests(unittest.TestCase):
    def setUp(self):
        self.f = FeishuScenarioTests();self.f.setUp();self.f.setup_app()
        self.groups = GroupSettings(self.f.settings_path, self.f.inbox_path, self.f.store)
        cfg = load(self.f.settings_path)
        cfg.update(prompt_policy_version=1, prompt_template='CANONICAL {{message}}',
                   allowed_chat_ids=['oc_team'], group_configs={'oc_team': {
                       'chat_id': 'oc_team', 'name': 'Team', 'connection_id': cfg['connection_id'],
                       'enabled': True, 'topic_collaboration': False}}, self_update_rule='共享追加规则')
        atomic_write(self.f.settings_path, cfg)

    def tearDown(self):self.f.tearDown()

    def test_group_editor_and_private_patch_write_same_root(self):
        cfg = load(self.f.settings_path)
        self.groups.save_group({'app_ref': 'default', 'revision': revision(cfg), 'chat_id': 'oc_team',
            'name': 'Team', 'project_id': 'project-1', 'prompt_template': 'GROUP EDIT {{message}}', 'enabled': True})
        cfg = load(self.f.settings_path)
        self.assertEqual(cfg['prompt_template'], 'GROUP EDIT {{message}}')
        self.assertNotIn('prompt_template', cfg['group_configs']['oc_team'])
        save(self.f.settings_path, {'prompt_template': 'PRIVATE EDIT {{message}}'}, self.f.store)
        cfg = load(self.f.settings_path)
        self.assertEqual(group_routes(cfg)[0]['prompt_template'], 'PRIVATE EDIT {{message}}')
        self.assertEqual(self.groups.group_defaults({'app_ref': 'default'})['prompt_template'], cfg['prompt_template'])

    def test_app_editor_does_not_materialize_group_forks(self):
        cfg = load(self.f.settings_path)
        self.groups.save_app({'ref': 'default', 'revision': revision(cfg), 'name': 'Bot',
                             'prompt_template': 'APP EDIT {{message}}'})
        cfg = load(self.f.settings_path)
        self.assertEqual(cfg['prompt_template'], 'APP EDIT {{message}}')
        self.assertNotIn('prompt_template', cfg['group_configs']['oc_team'])
        self.assertNotIn('self_update_rule', cfg['group_configs']['oc_team'])
        self.assertEqual(group_routes(cfg)[0]['self_update_rule'], '共享追加规则')


if __name__ == '__main__':unittest.main()
