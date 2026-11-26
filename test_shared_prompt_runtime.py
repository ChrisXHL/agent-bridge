"""Shared business policy never changes conversation or owner boundaries."""
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from assistant_schedules import capability as schedule_capability, fire_due, manage
from assistant_self_update import applied_rule, capability, instruction, owner_for, update
from codex_http_bridge import BridgeError
from feishu_groups import atomic_write
from feishu_settings import load
from feishu_worker import FeishuAdapter, Inbox
from test_feishu import FakeBridge, FakeSender, event


class SharedPromptRuntimeTests(unittest.TestCase):
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
            'prompt_template': 'CANONICAL {{message}}', 'self_update_rule': 'GLOBAL_RULE',
            # Deliberately stale forks must never become runtime business policy.
            'group_configs': {'oc_team': {'enabled': True, 'connection_id': 'con_group',
                'prompt_template': 'STALE_GROUP {{message}}', 'self_update_rule': 'STALE_RULE'}},
        }
        self.save()
        self.bridge = FakeBridge()
        self.adapter = FeishuAdapter(self.config, self.inbox, self.bridge, FakeSender())
        self.adapter.client_factory = lambda connection_id: self.bridge

    def tearDown(self):
        self.tmp.cleanup()

    def save(self):
        atomic_write(self.path, self.config)

    def receive(self, mid, group=False, sender='ou_owner', text='整理资料', parent=None):
        payload = event(mid, sender=sender, chat='oc_team' if group else 'oc_dm',
            chat_type='group' if group else 'p2p', text=text, parent=parent,
            mentions=[{'id': {'open_id': 'ou_bot'}, 'key': '@bot'}] if group else None)
        self.assertTrue(self.adapter.handle_event(payload))
        return self.inbox.linked_source(mid)

    def request(self, mid, sender='ou_owner', rule='完整的共享业务规则。', clear=False):
        return {'source_id': mid, 'token': capability(self.config, mid, sender), 'rule': rule, 'clear': clear}

    def schedule_request(self, mid, task='整理新资料并说明重点'):
        at = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M')
        return {'source_id': mid, 'token': schedule_capability(self.config, mid, 'ou_owner'),
                'action': 'create', 'at': at, 'timezone': 'UTC', 'repeat': 'once', 'task': task, 'id': ''}

    def test_group_private_share_business_not_delivery_or_routing(self):
        group = self.receive('om_shared_group', True, sender='ou_member')
        private = self.receive('om_shared_private')
        for item in (group, private):
            self.assertIn('CANONICAL', item['prompt'])
            self.assertIn('GLOBAL_RULE', item['prompt'])
            self.assertNotIn('STALE_', item['prompt'])
            self.assertEqual(json.loads(item['route_json'])['prompt_policy_version'], 1)
        self.assertIn('member-type openchat，chat_id 为 oc_team', group['prompt'])
        self.assertIn('member-type openid，open_id 为 ou_owner', private['prompt'])
        self.assertEqual(group['connection_id'], 'con_group')
        self.assertEqual(private['connection_id'], 'con_default')
        self.assertEqual(applied_rule(self.config, 'oc_team'), applied_rule(self.config))
        self.assertFalse(self.adapter.handle_event(event('om_member_private', sender='ou_member')))

    def test_current_policy_wins_after_frozen_parent_route(self):
        first = self.receive('om_frozen_first', True)
        route = json.loads(first['route_json'])
        route['prompt_template'] = 'FROZEN_OLD {{message}}'
        with self.inbox.db() as db:
            db.execute('UPDATE inbound SET route_json=? WHERE message_id=?', (json.dumps(route), first['message_id']))
        self.config['prompt_template'] = 'CURRENT_SHARED {{message}}'
        follow = self.receive('om_frozen_follow', True, parent=first['message_id'])
        self.assertIn('CURRENT_SHARED', follow['prompt'])
        self.assertNotIn('FROZEN_OLD', follow['prompt'])
        self.assertEqual(follow['connection_id'], 'con_group')
        self.assertEqual(json.loads(follow['route_json'])['prompt_template'], 'CURRENT_SHARED {{message}}')

    def test_current_policy_wins_after_topic_route(self):
        self.config['group_configs']['oc_team']['topic_collaboration'] = True
        self.bridge.topic_resolve = lambda data: {'topic': {'route': {
            'mode': 'prompt', 'topic_id': 'topic_verified', 'topic_collaboration': True,
            'connection_id': 'con_topic', 'prompt_template': 'TOPIC_OLD {{message}}'}}}
        item = self.receive('om_shared_topic', True)
        self.assertIn('CANONICAL', item['prompt'])
        self.assertNotIn('TOPIC_OLD', item['prompt'])
        self.assertEqual(item['connection_id'], 'con_topic')

    def test_only_verified_owner_gets_shared_rule_update_in_either_scene(self):
        for group in (False, True):
            mid = 'om_shared_owner_' + str(group)
            item = self.receive(mid, group, text='更新规则：以后列出来源')
            self.assertIn('全部后续新任务', item['prompt'])
            result = update(self.groups, self.request(mid))
            self.assertEqual(result['scope'], 'all')
            after = load(self.path)
            self.assertEqual(after['self_update_rule'], '完整的共享业务规则。')
            self.assertEqual(after['group_configs']['oc_team']['self_update_rule'], 'STALE_RULE')
            self.assertTrue(update(self.groups, self.request(mid))['already_applied'])
        # Group members may save shared rules too ("群内其他人说记住，也要记住").
        member = self.receive('om_shared_member_rule', True, sender='ou_member', text='更新规则：以后列出来源')
        self.assertIn('update-assistant-self.py', member['prompt'])
        self.assertEqual(update(self.groups, self.request(member['message_id'], sender='ou_member'))['scope'], 'all')
        # Private chats stay owner-only.
        self.assertEqual(instruction(load(self.path), self.path, 'om_p2p_member', 'ou_member', 'p2p', None,
                                     '更新规则：以后列出来源'), '')

    def test_sole_paired_user_is_shared_owner_without_creator_record(self):
        # Local patch: migrated assistants lack a creator record; the sole paired
        # user must still be able to save rules from private chat.
        config = {**self.config}
        config.pop('auto_group_owner_open_id')
        self.assertEqual(owner_for(config, verified_only=True), 'ou_owner')
        self.assertIn('update-assistant-self.py', instruction(
            config, None, 'om_unverified', 'ou_owner', 'p2p', None, '更新规则：以后列出来源'))
        self.assertEqual(instruction(config, None, 'om_other', 'ou_member', 'p2p', None,
                                     '更新规则：以后列出来源'), '')
        config['allowed_open_ids'] = ['ou_owner', 'ou_member']
        self.assertIsNone(owner_for(config, verified_only=True))

    def test_legacy_consumed_group_receipt_stays_read_only_after_migration(self):
        self.config.pop('prompt_policy_version')
        self.save()
        item = self.receive('om_consumed_legacy', True, sender='ou_member', text='更新规则：以后列出来源')
        data = self.request(item['message_id'], sender='ou_member')
        self.assertEqual(update(self.groups, data)['scope'], 'group')
        self.config = load(self.path)
        old_hash = self.config['self_update_receipts'][item['message_id']]
        self.config['prompt_policy_version'] = 1
        self.config['self_update_rule'] = '迁移后的全局规则保持不变。'
        self.config['group_configs']['oc_team'].pop('self_update_rule')
        self.save()
        before = self.path.read_bytes()
        result = update(self.groups, data)
        self.assertEqual(result['scope'], 'group')
        self.assertTrue(result['already_applied'])
        self.assertEqual(load(self.path)['self_update_receipts'][item['message_id']], old_hash)
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(BridgeError) as error:
            update(self.groups, {**data, 'rule': '不能用旧入口覆写共享规则。'})
        self.assertEqual(error.exception.code, 'self_update_conflict')
        self.assertEqual(self.path.read_bytes(), before)

    def test_unconsumed_legacy_group_owner_capability_cannot_gain_shared_scope(self):
        self.config.pop('prompt_policy_version')
        self.save()
        item = self.receive('om_unused_legacy', True, text='更新规则：以后列出来源')
        self.config['prompt_policy_version'] = 1
        self.save()
        with self.assertRaises(BridgeError) as error:
            update(self.groups, self.request(item['message_id']))
        self.assertEqual(error.exception.code, 'self_update_forbidden')

    def test_schedules_use_current_shared_policy_in_both_scenes(self):
        for group in (False, True):
            mid = 'om_shared_schedule_' + str(group)
            self.receive(mid, group, text='每天定时整理资料')
            result = manage(self.groups, self.schedule_request(mid))
            with self.inbox.db() as db:
                db.execute('UPDATE assistant_schedules SET next_run=? WHERE id=?', (time.time() - 1, result['id']))
        self.config['prompt_template'] = 'CURRENT_AT_FIRE {{message}}'
        fire_due(self.inbox, self.config)
        with self.inbox.db() as db:
            rows = db.execute("SELECT * FROM inbound WHERE message_id LIKE 'sch_%'").fetchall()
        self.assertEqual(len(rows), 2)
        for item in rows:
            self.assertIn('CURRENT_AT_FIRE', item['prompt'])
            self.assertNotIn('STALE_', item['prompt'])
            self.assertIn('GLOBAL_RULE', item['prompt'])
            self.assertIn('不要再次创建定时', item['prompt'])
            self.assertEqual(json.loads(item['route_json'])['prompt_policy_version'], 1)

    def test_schedule_base_validation_and_large_runtime_spill(self):
        # Base stays within 8000, augmentation and task exceed the wire limit.
        self.config['prompt_template'] = '业' * 7980 + '{{message}}'
        self.config['self_update_rule'] = '规' * 1600
        self.save()
        self.receive('om_large_schedule', True, text='每天定时整理资料')
        request = self.schedule_request('om_large_schedule', task='事' * 4000)
        created = manage(self.groups, request)
        with self.inbox.db() as db:
            db.execute('UPDATE assistant_schedules SET next_run=? WHERE id=?', (time.time() - 1, created['id']))
        fire_due(self.inbox, self.config)
        with self.inbox.db() as db:
            item = db.execute("SELECT * FROM inbound WHERE message_id LIKE 'sch_%'").fetchone()
        self.assertIsNotNone(item)
        self.assertIn('current_request', item['prompt'])
        files = list((Path(self.inbox.path).parent / 'conversations').glob('*.request.json'))
        self.assertTrue(any('事' * 4000 in load(path)['current_request'] for path in files))
        # Invalid canonical cannot be bypassed by a valid group fallback.
        self.receive('om_invalid_base_schedule', True, text='每天定时整理资料')
        self.config['prompt_template'] = 'missing message placeholder'
        self.save()
        with self.assertRaises(BridgeError) as error:
            manage(self.groups, self.schedule_request('om_invalid_base_schedule'))
        self.assertEqual(error.exception.code, 'invalid_prompt_template')

    def test_invalid_current_schedule_base_becomes_error_not_group_fallback(self):
        self.receive('om_invalid_at_fire', True, text='每天定时整理资料')
        created = manage(self.groups, self.schedule_request('om_invalid_at_fire'))
        with self.inbox.db() as db:
            db.execute('UPDATE assistant_schedules SET next_run=? WHERE id=?', (time.time() - 1, created['id']))
        self.config['prompt_template'] = 'invalid current canonical'
        fire_due(self.inbox, self.config)
        with self.inbox.db() as db:
            row = db.execute('SELECT state FROM assistant_schedules WHERE id=?', (created['id'],)).fetchone()
            self.assertEqual(row['state'], 'error')
            self.assertIsNone(db.execute("SELECT 1 FROM inbound WHERE message_id LIKE 'sch_%'").fetchone())

    def test_group_schedule_management_never_lists_or_cancels_other_scenes(self):
        self.config['allowed_chat_ids'].append('oc_other')
        self.config['group_configs']['oc_other'] = {'enabled': True}
        self.save()
        private = self.receive('om_private_schedule_manage', text='每天定时整理资料')
        team = self.receive('om_team_schedule_manage', True, text='每天定时整理资料')
        payload = event('om_other_schedule_manage', sender='ou_owner', chat='oc_other',
            chat_type='group', text='每天定时整理资料',
            mentions=[{'id': {'open_id': 'ou_bot'}, 'key': '@bot'}])
        self.assertTrue(self.adapter.handle_event(payload))
        created = {}
        for item, secret in ((private, '私聊任务机密'), (team, '当前群任务'),
                             (self.inbox.linked_source('om_other_schedule_manage'), '其他群任务机密')):
            created[item['chat_id']] = manage(self.groups,
                self.schedule_request(item['message_id'], task=secret))['id']
        group_request = self.schedule_request(team['message_id'])
        listing = manage(self.groups, {**group_request, 'action': 'list'})
        self.assertEqual([row['id'] for row in listing['schedules']], [created['oc_team']])
        self.assertNotIn('机密', json.dumps(listing, ensure_ascii=False))
        for foreign_chat in ('oc_dm', 'oc_other'):
            with self.assertRaises(BridgeError) as error:
                manage(self.groups, {**group_request, 'action': 'cancel', 'id': created[foreign_chat]})
            self.assertEqual(error.exception.code, 'schedule_not_found')
        self.assertTrue(manage(self.groups,
            {**group_request, 'action': 'cancel', 'id': created['oc_team']})['cancelled'])
        # Private creator management remains deliberately global.
        private_request = self.schedule_request(private['message_id'])
        listing = manage(self.groups, {**private_request, 'action': 'list'})
        self.assertEqual({row['id'] for row in listing['schedules']},
                         {created['oc_dm'], created['oc_other']})
        self.assertTrue(manage(self.groups,
            {**private_request, 'action': 'cancel', 'id': created['oc_other']})['cancelled'])

    def test_private_schedule_ignores_group_route_with_same_chat_identifier(self):
        # Even malformed/stale group metadata keyed by a private chat ID cannot
        # redirect execution or supply legacy group business policy to a DM.
        self.config.pop('prompt_policy_version')
        self.config['group_configs']['oc_dm'] = {'enabled': False,
            'connection_id': 'con_private_hijack', 'prompt_template': 'GROUP_LEAK {{message}}',
            'self_update_rule': 'GROUP_RULE_LEAK'}
        self.save()
        self.receive('om_private_route_schedule', text='每天定时整理资料')
        created = manage(self.groups, self.schedule_request('om_private_route_schedule'))
        with self.inbox.db() as db:
            db.execute('UPDATE assistant_schedules SET next_run=? WHERE id=?', (time.time() - 1, created['id']))
        fire_due(self.inbox, self.config)
        with self.inbox.db() as db:
            item = db.execute("SELECT * FROM inbound WHERE message_id LIKE 'sch_%'").fetchone()
        self.assertEqual(item['connection_id'], 'con_default')
        self.assertIn('CANONICAL', item['prompt'])
        self.assertNotIn('GROUP_LEAK', item['prompt'])
        self.assertNotIn('GROUP_RULE_LEAK', item['prompt'])

    def test_group_member_has_no_shared_schedule_authority(self):
        item = self.receive('om_member_shared_schedule', True, sender='ou_member', text='每天定时整理资料')
        self.assertNotIn('assistant-schedule.py', item['prompt'])
        data = self.schedule_request(item['message_id'])
        data['token'] = schedule_capability(self.config, item['message_id'], 'ou_member')
        with self.assertRaises(BridgeError) as error:
            manage(self.groups, data)
        self.assertEqual(error.exception.code, 'schedule_forbidden')


if __name__ == '__main__':
    unittest.main()
