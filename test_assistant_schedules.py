import json
import sqlite3
import time
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from assistant_schedules import _next_run, capability, fire_due, install, manage
from codex_http_bridge import BridgeError
from feishu_groups import atomic_write
from feishu_worker import FeishuAdapter, Inbox
from test_assistant_onboarding import AssistantTests
from test_feishu import FakeBridge, FakeSender, event


class ScheduleSender(FakeSender):
    def send_text(self, open_id, request_id, body):
        self.sent.append(('dm', open_id, body))
        return 'om_schedule_' + str(len(self.sent))

    def send_group(self, chat_id, request_id, body):
        self.sent.append(('group', chat_id, body))
        return 'om_schedule_' + str(len(self.sent))


class AssistantScheduleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = AssistantTests()
        self.fixture.setUp()
        job = self.fixture.prepare()
        self.fixture.finish(job)
        job = self.fixture.manager.read(job['id'])
        self.groups = self.fixture.manager.groups
        _, self.path, inbox_path = self.groups.resolve(job['app_ref'])
        self.config = json.loads(self.path.read_text())
        self.inbox = Inbox(inbox_path)
        self.bridge = FakeBridge()
        self.sender = ScheduleSender()
        self.adapter = FeishuAdapter(self.config, self.inbox, self.bridge, self.sender)

    def tearDown(self):
        self.fixture.tearDown()

    def receive(self, mid='om_schedule_owner', sender='ou_creator', group=False,
                text='明天早上九点定时整理今天的资料'):
        if group:
            self.config.setdefault('allowed_chat_ids', []).append('oc_team')
            self.config.setdefault('group_configs', {})['oc_team'] = {
                'chat_id': 'oc_team', 'name': '测试群', 'connection_id': self.config['connection_id'],
                'prompt_template': '群任务 {{message}}', 'enabled': True}
            atomic_write(self.path, self.config)
        payload = event(mid, sender=sender, chat='oc_team' if group else 'oc_dm',
                        chat_type='group' if group else 'p2p', text=text,
                        mentions=[{'id': {'open_id': 'ou_bot'}, 'key': '@bot'}] if group else None)
        self.assertTrue(self.adapter.handle_event(payload))
        return self.inbox.linked_source(mid)

    def request(self, mid, action='create', **kwargs):
        at = (datetime.now(ZoneInfo('Asia/Taipei')) + timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M')
        return {'source_id': mid, 'token': capability(self.config, mid, 'ou_creator'), 'action': action,
                'at': kwargs.get('at', at), 'timezone': 'Asia/Taipei', 'repeat': kwargs.get('repeat', 'once'),
                'task': kwargs.get('task', '整理新资料并说明重点'), 'id': kwargs.get('id', '')}

    def test_two_hour_interval_is_durable_and_skips_missed_ticks(self):
        item = self.receive('om_interval_schedule', text='每隔2小时找新闻并发给我')
        data = {**self.request(item['message_id'], repeat='interval'), 'interval_minutes': 120}
        created = manage(self.groups, data)
        self.assertEqual(created['interval_minutes'], 120)
        self.assertEqual(manage(self.groups, data)['id'], created['id'])
        with self.inbox.db() as db:
            columns = {r['name'] for r in db.execute('PRAGMA table_info(assistant_schedules)')}
            self.assertIn('interval_minutes', columns)
            old_local = (datetime.now(ZoneInfo('Asia/Taipei')) - timedelta(hours=7)).strftime('%Y-%m-%dT%H:%M')
            db.execute('UPDATE assistant_schedules SET local_at=?,next_run=? WHERE id=?',
                       (old_local, time.time() - 3 * 3600, created['id']))
        fire_due(self.inbox, self.config)
        with self.inbox.db() as db:
            row = db.execute('SELECT next_run,interval_minutes,state FROM assistant_schedules WHERE id=?',
                             (created['id'],)).fetchone()
        self.assertEqual(row['interval_minutes'], 120)
        self.assertEqual(row['state'], 'active')
        self.assertGreater(row['next_run'], time.time())
        self.assertLessEqual(row['next_run'] - time.time(), 7200)
        scheduled = [r for r in self.inbox.pending() if r['message_id'].startswith('sch_')]
        self.assertEqual(len(scheduled), 1)
        self.assertIn('[本轮间隔]', scheduled[0]['prompt'])

    def test_fixed_interval_uses_elapsed_time_not_calendar_day(self):
        anchor = '2026-11-08T09:00'
        zone = ZoneInfo('Asia/Taipei')
        first = datetime(2026, 11, 8, 9, tzinfo=zone).timestamp()
        self.assertEqual(_next_run(anchor, 'Asia/Taipei', 'interval', first - 1, 120), first)
        self.assertEqual(_next_run(anchor, 'Asia/Taipei', 'interval', first + 20 * 3600, 120), first + 22 * 3600)
        with self.assertRaises(BridgeError):
            _next_run(anchor, 'Asia/Taipei', 'interval', first, 0)

    def test_one_minute_interval_fires_once_per_slot(self):
        item = self.receive('om_minute_schedule', text='每隔1分钟检查一次')
        data = {**self.request(item['message_id'], repeat='interval'), 'interval_minutes': 1}
        created = manage(self.groups, data)
        old_local = (datetime.now(ZoneInfo('Asia/Taipei')) - timedelta(minutes=3)).strftime('%Y-%m-%dT%H:%M')
        with self.inbox.db() as db:
            db.execute('UPDATE assistant_schedules SET local_at=?,next_run=? WHERE id=?',
                       (old_local, time.time() - 60, created['id']))
        fire_due(self.inbox, self.config)
        fire_due(self.inbox, self.config)
        scheduled = [row for row in self.inbox.pending() if row['message_id'].startswith('sch_')]
        self.assertEqual(len(scheduled), 1)
        with self.inbox.db() as db:
            db.execute('UPDATE assistant_schedules SET next_run=? WHERE id=?',
                       (time.time() - 1, created['id']))
        fire_due(self.inbox, self.config)
        self.assertEqual(len([row for row in self.inbox.pending() if row['message_id'].startswith('sch_')]), 1)
        self.inbox.mark_notified(scheduled[0]['message_id'], '已完成', 'om_result')
        fire_due(self.inbox, self.config)
        self.assertEqual(len([row for row in self.inbox.pending() if row['message_id'].startswith('sch_')]), 2)
        with self.inbox.db() as db:
            next_run = db.execute('SELECT next_run FROM assistant_schedules WHERE id=?',
                                  (created['id'],)).fetchone()[0]
        self.assertGreater(next_run, time.time())
        self.assertLessEqual(next_run - time.time(), 60)

    def test_existing_schedule_table_gains_interval_column_without_losing_rows(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.execute('''CREATE TABLE assistant_schedules (
            id TEXT PRIMARY KEY, app_id TEXT NOT NULL, owner_id TEXT NOT NULL,
            chat_id TEXT NOT NULL, chat_type TEXT NOT NULL, task_text TEXT NOT NULL,
            timezone TEXT NOT NULL, repeat TEXT NOT NULL, local_at TEXT NOT NULL,
            next_run REAL NOT NULL, state TEXT NOT NULL, source_id TEXT NOT NULL,
            created_at REAL NOT NULL, last_fired REAL, last_error TEXT,
            UNIQUE(source_id, app_id))''')
        db.execute('''INSERT INTO assistant_schedules VALUES
            ('sch_existing','cli_existing','ou_owner','oc_dm','p2p','旧任务','Asia/Taipei',
             'daily','2026-11-08T09:00',1,'active','om_existing',1,NULL,NULL)''')
        install(db)
        self.assertEqual(db.execute("SELECT repeat,interval_minutes FROM assistant_schedules WHERE id='sch_existing'").fetchone()[:],
                         ('daily', 0))
        install(db)
        db.close()

    def test_owner_schedule_fires_once_and_sends_result_to_private_chat(self):
        item = self.receive()
        self.assertIn('assistant-schedule.py', item['prompt'])
        result = manage(self.groups, self.request(item['message_id']))
        self.assertTrue(result['created'])
        with self.inbox.db() as db:
            db.execute('UPDATE assistant_schedules SET next_run=? WHERE id=?', (time.time() - 1, result['id']))
        self.adapter.pump_once()
        self.assertEqual(len(self.bridge.submissions), 2)
        self.assertTrue(any('整理新资料' in prompt for prompt, _ in self.bridge.submissions))
        scheduled = [r for r in self.inbox.submitted() if r['message_id'].startswith('sch_')]
        self.assertEqual(len(scheduled), 1)
        self.bridge.tasks[scheduled[0]['task_id']] = {'status': '已完成', 'result': '重点已整理'}
        self.adapter.pump_once()
        self.assertIn(('dm', 'ou_creator', '重点已整理'), self.sender.sent)
        self.adapter.pump_once()
        self.assertEqual(len(self.sender.sent), 1)
        self.assertEqual(len(self.bridge.submissions), 2)

    def test_group_schedule_returns_to_same_group(self):
        item = self.receive('om_group_schedule', group=True)
        result = manage(self.groups, self.request(item['message_id']))
        with self.inbox.db() as db:
            db.execute('UPDATE assistant_schedules SET next_run=? WHERE id=?', (time.time() - 1, result['id']))
        self.adapter.pump_once()
        scheduled = [r for r in self.inbox.submitted() if r['message_id'].startswith('sch_')]
        self.bridge.tasks[scheduled[0]['task_id']] = {'status': '已完成', 'result': '群里的结果'}
        self.adapter.pump_once()
        self.assertIn(('group', 'oc_team', '群里的结果'), self.sender.sent)

    def test_other_member_and_ordinary_message_cannot_create_schedule(self):
        member = self.receive('om_member_schedule', sender='ou_member', group=True)
        self.assertNotIn('assistant-schedule.py', member['prompt'])
        with self.assertRaises(BridgeError):
            manage(self.groups, self.request(member['message_id']))
        ordinary = self.receive('om_ordinary', text='帮我整理资料')
        self.assertNotIn('assistant-schedule.py', ordinary['prompt'])
        with self.assertRaises(BridgeError):
            manage(self.groups, self.request(ordinary['message_id']))

    def test_repeat_list_cancel_and_no_duplicate_creation(self):
        item = self.receive('om_daily_schedule', text='每天早上定时整理资料')
        request = self.request(item['message_id'], repeat='daily')
        created = manage(self.groups, request)
        self.assertEqual(manage(self.groups, request)['id'], created['id'])
        listing = manage(self.groups, self.request(item['message_id'], action='list'))
        self.assertEqual(listing['schedules'][0]['id'], created['id'])
        with self.inbox.db() as db:
            db.execute('UPDATE assistant_schedules SET next_run=? WHERE id=?', (time.time() - 1, created['id']))
        fire_due(self.inbox, self.config)
        with self.inbox.db() as db:
            row = db.execute('SELECT next_run,state FROM assistant_schedules WHERE id=?', (created['id'],)).fetchone()
        self.assertEqual(row['state'], 'active')
        self.assertGreater(row['next_run'], time.time())
        self.assertTrue(manage(self.groups, self.request(item['message_id'], action='cancel', id=created['id']))['cancelled'])
        self.assertEqual(manage(self.groups, self.request(item['message_id'], action='list'))['schedules'], [])

    def test_disabled_group_keeps_due_schedule_without_firing(self):
        item = self.receive('om_paused_schedule', group=True)
        created = manage(self.groups, self.request(item['message_id']))
        with self.inbox.db() as db:
            db.execute('UPDATE assistant_schedules SET next_run=? WHERE id=?', (time.time() - 1, created['id']))
        self.config['group_configs']['oc_team']['enabled'] = False
        fire_due(self.inbox, self.config)
        self.assertFalse([r for r in self.inbox.pending() if r['message_id'].startswith('sch_')])

    def test_paused_group_does_not_block_private_schedule(self):
        group_item = self.receive('om_paused_group', group=True)
        private_item = self.receive('om_active_private')
        group_schedule = manage(self.groups, self.request(group_item['message_id']))
        private_schedule = manage(self.groups, self.request(private_item['message_id']))
        with self.inbox.db() as db:
            db.execute('UPDATE assistant_schedules SET next_run=? WHERE id=?',
                       (time.time() - 3, group_schedule['id']))
            db.execute('UPDATE assistant_schedules SET next_run=? WHERE id=?',
                       (time.time() - 1, private_schedule['id']))
        self.config['group_configs']['oc_team']['enabled'] = False
        fire_due(self.inbox, self.config)
        scheduled = [r for r in self.inbox.pending() if r['message_id'].startswith('sch_')]
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0]['chat_type'], 'p2p')


if __name__ == '__main__':
    unittest.main()
