"""Task labels use actual chat type and assistant, without workspace arrows."""
import json
import tempfile
import unittest
from pathlib import Path
from service import Store
from feishu_groups import atomic_write
from feishu_worker import Inbox


class TaskLabelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        project = self.root / 'workspace';project.mkdir()
        atomic_write(self.root / 'projects.json', {'workspace': str(project)})
        self.store = Store(self.root / 'bridge.sqlite3', self.root / 'projects.json', self.root / 'no-state')
        self.conn = self.store.create_connection('不能靠此名称猜群聊', 'workspace')
        self.app = self.root / 'feishu-apps/app_aaaaaaaaaaaaaaaa'
        atomic_write(self.app / 'config.json', {'app_id': 'cli_labels', 'app_name': '资料助手',
            'allowed_chat_ids': ['oc_team'], 'connection_id': self.conn['id'],
            'group_configs': {'oc_team': {'name': '研发群'}}})
        self.inbox = Inbox(self.app / 'inbox.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def task(self, chat_type, mid, chat='oc_team'):
        _, created = self.store.submit(self.store.connection(self.conn['id']), {'message': '原有执行内容不变'}, 'label-' + mid)
        tid = created['task_id']
        self.inbox.accept({'message_id': mid, 'chat_id': chat, 'chat_type': chat_type, 'sender_id': 'ou_user',
                          'app_id': 'cli_labels', 'connection_id': self.conn['id'],
                          'text': '原始请求', 'prompt': '执行内容', 'route_json': '{"mode":"prompt"}'})
        self.inbox.mark_submitted(mid, tid)
        return tid

    def test_group_and_private_same_connection_have_distinct_labels(self):
        group = self.task('group', 'om_group')
        private = self.task('p2p', 'om_private', 'oc_private')
        rows = {r['id']: r for r in self.store.tasks()}
        self.assertEqual(rows[group]['source_label'], '研发群（资料助手）')
        self.assertEqual(rows[private]['source_label'], '资料助手')
        self.assertEqual(self.store.task(group, include_source=True)['source_label'], '研发群（资料助手）')
        self.assertEqual(self.store.task(private)['title'], '原有执行内容不变')

    def test_unknown_group_name_does_not_use_workspace(self):
        tid = self.task('group', 'om_missing', 'oc_deleted')
        self.assertEqual(self.store.task(tid, include_source=True)['source_label'], '群聊（名称未记录）（资料助手）')

    def test_non_feishu_source_is_not_guessed_as_group(self):
        _, created = self.store.submit(self.store.connection(self.conn['id']), {'message': '直接API任务'}, 'direct-label')
        self.assertNotIn('source_label', self.store.task(created['task_id']))

    def test_foreign_app_receipt_cannot_label_task(self):
        tid = self.task('group', 'om_wrong_app')
        with self.inbox.db() as db:
            db.execute('UPDATE inbound SET app_id=?', ('cli_other',))
        self.assertNotIn('source_label', self.store.task(tid, include_source=True))


if __name__ == '__main__':
    unittest.main()
