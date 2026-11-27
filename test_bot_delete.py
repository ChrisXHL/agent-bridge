"""Per-bot 删除 on the 机器人与群 cards: recoverable archive for bots without an assistant job."""
import json
import unittest
import test_feishu_groups as fixtures
from feishu_groups import revision
from feishu_settings import load
from codex_http_bridge import BridgeError


class BotDeleteTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.GroupTests(); self.f.setUp()
        self.manager, self.settings_path, self.store = self.f.manager, self.f.settings_path, self.f.store

    def tearDown(self):
        self.f.tearDown()

    def remove(self):
        return self.manager.dispatch('/api/feishu/apps/remove', 'POST',
                                     {'app_ref': 'default', 'revision': revision(load(self.settings_path))})

    def test_delete_then_restore_round_trips_config_and_connection(self):
        before = load(self.settings_path)
        cid = before['connection_id']
        status = self.store.connection(cid)['status']
        catalog = self.remove()
        self.assertFalse(any(a['ref'] == 'default' for a in catalog['apps']))
        self.assertEqual([x['ref'] for x in catalog['removed_apps']], ['default'])
        archived = load(self.settings_path)
        self.assertFalse(archived['enabled'])
        self.assertEqual(archived['assistant_archived'], 'bot:default')
        self.assertEqual(self.store.connection(cid)['status'], '已停用')
        catalog = self.manager.dispatch('/api/feishu/apps/restore', 'POST', {'app_ref': 'default'})
        self.assertTrue(any(a['ref'] == 'default' for a in catalog['apps']))
        self.assertEqual(catalog['removed_apps'], [])
        self.assertEqual(load(self.settings_path), before)
        self.assertEqual(self.store.connection(cid)['status'], status)

    def test_busy_task_blocks_delete(self):
        cid = load(self.settings_path)['connection_id']
        self.store.submit(self.store.connection(cid), {'message': '执行中'}, 'bot-delete-busy')
        with self.assertRaises(BridgeError) as e:
            self.remove()
        self.assertEqual(e.exception.code, 'bot_busy')
        self.assertNotIn('assistant_archived', load(self.settings_path))

    def test_stale_revision_rejected(self):
        with self.assertRaises(BridgeError) as e:
            self.manager.dispatch('/api/feishu/apps/remove', 'POST', {'app_ref': 'default', 'revision': 'x'})
        self.assertEqual(e.exception.code, 'settings_changed')


if __name__ == '__main__':
    unittest.main()
