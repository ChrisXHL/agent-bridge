"""Bounded same-schedule history and failed-window recovery, without network calls."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from assistant_schedules import fire_due, install
from feishu_worker import Inbox, FeishuAdapter
from test_feishu import FakeBridge, FakeSender


class ScheduleHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Inbox(Path(self.tmp.name) / 'inbox.sqlite3')
        self.bridge = FakeBridge()
        self.adapter = FeishuAdapter({}, self.inbox, self.bridge, FakeSender())
        self.base = {'app_id': 'cli_test', 'chat_id': 'oc_team', 'chat_type': 'group',
                     'sender_id': 'ou_owner', 'connection_id': 'conn_test', 'text': '查找资料',
                     'prompt': '查找资料'}

    def tearDown(self):
        self.tmp.cleanup()

    def accept(self, mid, previous=None, **fields):
        route = {'mode': 'prompt', 'scheduled': True, 'schedule_id': 'sch_test',
                 'previous_message_id': previous, 'window_start': time.time() - 7200}
        item = {**self.base, 'message_id': mid, 'route_json': json.dumps(route), **fields}
        self.inbox.accept(item)
        return self.inbox.linked_source(mid), route

    def history_path(self, item):
        import hashlib
        return Path(self.tmp.name) / 'conversations' / (hashlib.sha256(item['message_id'].encode()).hexdigest() + '.schedule-history.json')

    def test_empty_history_is_frozen_even_when_prior_becomes_available(self):
        prior, _ = self.accept('sch_initially_unsubmitted')
        current, route = self.accept('sch_retry', prior['message_id'])
        suffix = self.adapter.scheduled_history(current, route, self.bridge)
        before = self.history_path(current).read_bytes()
        self.inbox.mark_submitted(prior['message_id'], 'task_later')
        self.bridge.tasks['task_later'] = {'status': '已完成', 'result': '后来可读'}
        self.assertEqual(self.adapter.scheduled_history(current, route, self.bridge), suffix)
        self.assertEqual(self.history_path(current).read_bytes(), before)

    def test_corrupt_route_and_missing_task_do_not_block_current_round(self):
        for i, corrupt in enumerate(('[]', '{broken', None)):
            prior, _ = self.accept('sch_corrupt_' + str(i))
            self.inbox.mark_submitted(prior['message_id'], 'task_missing')
            if corrupt:
                with self.inbox.db() as db:
                    db.execute('UPDATE inbound SET route_json=? WHERE message_id=?', (corrupt, prior['message_id']))
            current, route = self.accept('sch_current_corrupt_' + str(i), prior['message_id'])
            self.assertIn('读取', self.adapter.scheduled_history(current, route, self.bridge))
            data = json.loads(self.history_path(current).read_text())
            self.assertEqual(data['occurrences'], [])
            self.assertTrue(data['gaps'])

    def test_real_status_is_bounded_frozen_and_not_confused_with_delivery(self):
        prior, _ = self.accept('sch_previous')
        self.inbox.mark_submitted('sch_previous', 'task_previous')
        self.bridge.tasks['task_previous'] = {'status': '已完成', 'result': '已找到线索，但尚未入库'}
        current, route = self.accept('sch_current', 'sch_previous')
        suffix = self.adapter.scheduled_history(current, route, self.bridge)
        self.assertIn('读取', suffix)
        path = next((Path(self.tmp.name) / 'conversations').glob('*.schedule-history.json'))
        data = json.loads(path.read_text())
        self.assertFalse(data['occurrences'][0]['delivered'])
        self.assertEqual(data['occurrences'][0]['status'], '已完成')
        self.assertNotIn('prompt', data['occurrences'][0])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        before = path.read_bytes()
        self.bridge.tasks['task_previous']['result'] = '后来的不同结果'
        self.assertEqual(self.adapter.scheduled_history(current, route, self.bridge), suffix)
        self.assertEqual(path.read_bytes(), before)

    def test_foreign_identity_and_schedule_are_never_read(self):
        for field in ('app_id', 'chat_id', 'chat_type', 'sender_id', 'connection_id'):
            with self.subTest(field=field):
                prior, _ = self.accept('sch_foreign_' + field, **{field: 'foreign'})
                current, route = self.accept('sch_current_' + field, prior['message_id'])
                self.assertIn('读取', self.adapter.scheduled_history(current, route, self.bridge))
                path = self.history_path(current)
                self.assertEqual(json.loads(path.read_text())['occurrences'], [])
        prior, route = self.accept('sch_other_schedule')
        route['schedule_id'] = 'sch_foreign'
        with self.inbox.db() as db:
            db.execute('UPDATE inbound SET route_json=? WHERE message_id=?', (json.dumps(route), prior['message_id']))
        current, route = self.accept('sch_other_current', prior['message_id'])
        self.assertIn('读取', self.adapter.scheduled_history(current, route, self.bridge))
        self.assertEqual(json.loads(self.history_path(current).read_text())['occurrences'], [])

    def test_five_round_limit_and_cycle_protection(self):
        previous = None
        for i in range(7):
            mid = 'sch_round_' + str(i)
            self.accept(mid, previous)
            self.inbox.mark_submitted(mid, 'task_' + str(i))
            self.bridge.tasks['task_' + str(i)] = {'status': '失败', 'result': '未完成'}
            previous = mid
        current, route = self.accept('sch_latest', previous)
        self.adapter.scheduled_history(current, route, self.bridge)
        path = next((Path(self.tmp.name) / 'conversations').glob('*.schedule-history.json'))
        self.assertEqual(len(json.loads(path.read_text())['occurrences']), 5)
        current, route = self.accept('sch_self_cycle', 'sch_self_cycle')
        self.assertIn('读取', self.adapter.scheduled_history(current, route, self.bridge))
        self.assertEqual(json.loads(self.history_path(current).read_text())['occurrences'], [])

    def test_failed_interval_retains_search_window_for_next_occurrence(self):
        cfg = {'app_id': 'cli_test', 'enabled': True, 'auto_group_owner_open_id': 'ou_owner',
               'allowed_chat_ids': ['oc_team'], 'connection_id': 'conn_test', 'prompt_template': '{{message}}'}
        now = time.time()
        with self.inbox.db() as db:
            install(db)
            db.execute('''INSERT INTO assistant_schedules
                (id,app_id,owner_id,chat_id,chat_type,task_text,timezone,repeat,local_at,next_run,state,source_id,created_at,interval_minutes)
                VALUES ('sch_test','cli_test','ou_owner','oc_team','group','查找资料','Asia/Taipei','interval','2026-11-01T09:00',?,'active','om_authorized',?,360)''', (now - 1, now))
        fire_due(self.inbox, cfg)
        first = next(r for r in self.inbox.pending() if r['message_id'].startswith('sch_'))
        first_route = json.loads(first['route_json'])
        self.inbox.mark_notified(first['message_id'], '失败', 'om_failed')
        with self.inbox.db() as db:
            db.execute('UPDATE assistant_schedules SET next_run=?', (now - 2,))
        fire_due(self.inbox, cfg)
        second = next(r for r in self.inbox.pending() if r['message_id'] != first['message_id'])
        second_route = json.loads(second['route_json'])
        self.assertEqual(second_route['previous_message_id'], first['message_id'])
        self.assertEqual(second_route['window_start'], first_route['window_start'])
        self.assertEqual(second_route['schedule_id'], 'sch_test')


if __name__ == '__main__':
    unittest.main()
