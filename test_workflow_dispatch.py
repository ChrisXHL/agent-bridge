"""Regression tests for obsolete workflow prompts and desktop writer ownership."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from codex_http_bridge import RpcError
from feishu_worker import Inbox
from service import Store, Engine
from test_service import FakeTransport


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        project = self.root / 'project'; project.mkdir()
        mapping = self.root / 'projects.json'; mapping.write_text(json.dumps({'p':str(project)}))
        self.store = Store(self.root/'bridge.db', mapping, self.root/'state.db')
        self.conn = self.store.create_connection('fixture','p')
        self.inbox = Inbox(self.root/'inbox.db')
        self.fake = FakeTransport()
        self.engine = Engine(self.store,self.fake,workflow_inbox_path=self.root/'inbox.db')
        self.source = dict(message_id='om_source',chat_id='oc_a',sender_id='ou_a',text='fixture',prompt='fixture',
                           connection_id=self.conn['id'],app_id='cli_fixture',route_json='{}',chat_type='group')
        self.inbox.accept(self.source)
        self.rid = self.inbox.workflows.start(self.source,{'name':'fixture','steps':[
            {'id':'first','name':'First','kind':'agent','prompt':'first','produces':{'first_file':{'kind':'file'}},'next':'second'},
            {'id':'second','name':'Second','kind':'agent','prompt':'second','produces':{'second_file':{'kind':'file'}}}]},
            {'app_id':'cli_fixture','cards':[],'allowed_open_ids':['ou_a']})
        self.item = self.inbox.cards.source('om_source')
        req = 'fs_' + hashlib.sha256(b'cli_fixture:om_source').hexdigest()[:48]
        _, result = self.store.submit(self.conn, {'message':self.item['prompt']}, req)
        self.tid = result['task_id']; self.inbox.mark_submitted('om_source', self.tid)

    def tearDown(self):
        self.tmp.cleanup()

    def advance(self):
        run=self.inbox.workflows.get(self.rid)
        output=self.root/'first.txt';output.write_text('verified fixture')
        self.inbox.workflows.act({'run_id':self.rid,'revision':run['revision'],'action':'complete','outputs':{'first_file':{'path':str(output)}}})

    def test_obsolete_queued_stage_never_sent_and_status_recovers(self):
        # Desktop completed the stage while an older Bridge retry stayed queued.
        with self.store.db() as db: db.execute('UPDATE tasks SET thread_id=? WHERE id=?',('external',self.tid))
        self.fake.turns['external']=[{'status':'completed','items':[{'type':'agentMessage','text':'done'}]}]
        self.advance(); self.engine.tick()
        task=self.store.task(self.tid)
        self.assertEqual(task['messages'][0]['delivery'],'superseded')
        self.assertEqual(task['status'],'已完成')
        self.assertEqual(self.fake.created,0)
        self.assertEqual(self.fake.resumed,[])
        # The next stage can reuse the existing task; it is the only prompt sent.
        item=self.inbox.pending()[0]
        self.store.submit(self.conn,{'task_id':self.tid,'message':item['prompt']},'next-stage-request')
        self.fake.messages['external']=[]
        self.engine.tick()
        self.assertEqual(self.fake.messages['external'],[item['prompt']])

    def test_current_stage_is_not_superseded(self):
        self.engine.tick()
        self.assertEqual(self.store.task(self.tid)['messages'][0]['delivery'],'delivered')

    def test_uncertain_old_stage_is_never_automatically_replayed(self):
        with self.store.db() as db: db.execute("UPDATE messages SET delivery='uncertain' WHERE task_id=?",(self.tid,))
        self.advance();self.engine.tick()
        self.assertEqual(self.store.task(self.tid)['messages'][0]['delivery'],'uncertain')
        self.assertEqual(self.fake.created,0)

    def test_writer_conflict_is_visible_and_keeps_message_queued(self):
        self.engine.tick();t=self.store.task(self.tid);self.fake.complete(t['thread_id'],'done');self.engine.tick()
        self.store.submit(self.conn,{'task_id':self.tid,'message':'continue'},'continue-request')
        with patch.object(self.fake,'resume_thread',side_effect=RpcError(-32600,'thread already has an active writer')):
            self.engine.tick()
        t=self.store.task(self.tid)
        self.assertEqual(t['error_code'],'codex_thread_owned')
        self.assertEqual(t['messages'][-1]['delivery'],'queued')
        self.assertEqual(len(self.fake.messages[t['thread_id']]),1)

    def test_live_workflow_keeps_writer_until_workflow_finishes(self):
        self.engine.tick();t=self.store.task(self.tid)
        self.fake.complete(t['thread_id'],'done');self.engine.tick()
        self.assertEqual(self.fake.released,0)
        with self.inbox.db() as db:db.execute("UPDATE workflow_runs SET state='completed'")
        self.engine.tick();self.assertGreater(self.fake.released,0)

    def test_prior_completed_turn_cannot_finish_new_dispatch(self):
        self.engine.tick(); t=self.store.task(self.tid)
        with self.store.db() as db:
            db.execute("UPDATE messages SET turn_id='new-turn' WHERE task_id=?",(self.tid,))
        self.fake.turns[t['thread_id']]=[{'id':'old-turn','status':'completed','items':[]}]
        self.engine.tick()
        self.assertEqual(self.store.task(self.tid)['status'],'执行中')

    def test_foreign_partial_history_is_not_a_real_interruption(self):
        self.engine.tick()
        with patch.object(self.fake,'read',return_value={'status':{'type':'notLoaded'},'turns':[{'id':'turn-1','status':'interrupted','items':[]}]}):
            self.engine.tick()
        self.assertEqual(self.store.task(self.tid)['status'],'执行中')

    def test_blocked_current_stage_is_held_without_dispatch(self):
        run=self.inbox.workflows.get(self.rid)
        self.inbox.workflows.act({'run_id':self.rid,'revision':run['revision'],'action':'fail','reason':'Known input conflict'})
        self.engine.tick()
        self.assertEqual(self.fake.created,0)
        self.assertEqual(self.store.task(self.tid)['messages'][0]['delivery'],'queued')
        self.assertEqual(self.store.task(self.tid)['status'],'需要你处理')
        self.assertEqual(self.store.task(self.tid)['error_code'],'workflow_blocked')
        self.assertEqual(self.store.task(self.tid)['result'],'Known input conflict')

    def block(self):
        run=self.inbox.workflows.get(self.rid)
        self.inbox.workflows.act({'run_id':self.rid,'revision':run['revision'],'action':'fail','reason':'Known input conflict'})

    def test_blocked_delivered_stage_is_not_overwritten_by_completed_turn(self):
        self.engine.tick(); task=self.store.task(self.tid)
        self.fake.complete(task['thread_id'],'last turn ended')
        self.block(); self.engine.tick()
        updated=self.store.task(self.tid)['updated_at']
        self.engine.tick()
        task=self.store.task(self.tid)
        self.assertEqual(task['status'],'需要你处理')
        self.assertEqual(task['result'],'Known input conflict')
        self.assertEqual(task['updated_at'],updated)
        self.assertEqual(self.fake.released,0)

    def test_blocked_stage_does_not_consume_an_execution_slot(self):
        self.engine.max_active=1
        self.block(); self.engine.tick()
        _, result=self.store.submit(self.conn,{'message':'independent task'},'independent')
        self.engine.tick()
        self.assertEqual(self.store.task(result['task_id'])['status'],'执行中')
        self.assertEqual(self.store.task(self.tid)['status'],'需要你处理')

    def test_retry_releases_pause_and_dispatches_only_new_epoch(self):
        self.block(); self.engine.tick()
        run=self.inbox.workflows.get(self.rid)
        self.inbox.workflows.act({'run_id':self.rid,'revision':run['revision'],'action':'retry'})
        self.engine.tick()
        task=self.store.task(self.tid)
        self.assertEqual(task['status'],'已完成')
        self.assertIsNone(task['error_code'])
        item=self.inbox.pending()[0]
        req='fs_'+hashlib.sha256(('cli_fixture:'+item['message_id']).encode()).hexdigest()[:48]
        self.store.submit(self.conn,{'task_id':self.tid,'message':item['prompt']},req)
        self.inbox.mark_submitted(item['message_id'],self.tid)
        self.engine.tick()
        task=self.store.task(self.tid)
        self.assertEqual([m['delivery'] for m in task['messages']],['superseded','delivered'])
        self.assertEqual(self.fake.messages[task['thread_id']],[item['prompt']])

    def test_cancel_releases_pause_without_dispatch(self):
        self.block(); self.engine.tick()
        run=self.inbox.workflows.get(self.rid)
        self.inbox.workflows.act({'run_id':self.rid,'revision':run['revision'],'action':'cancel'})
        self.engine.tick()
        self.assertEqual(self.store.task(self.tid)['status'],'已停止')
        self.assertEqual(self.fake.created,0)

    def test_pause_notifies_once_and_worker_can_resume_same_task(self):
        from types import SimpleNamespace
        from feishu_worker import FeishuAdapter
        from test_feishu import FakeSender
        bridge=SimpleNamespace(task=self.store.task,submit=lambda message, request_id, task_id=None:
            self.store.submit(self.conn,{'message':message,**({'task_id':task_id} if task_id else {})},request_id)[1])
        sender=FakeSender()
        adapter=FeishuAdapter({'app_id':'cli_fixture','connection_id':self.conn['id']},self.inbox,bridge,sender)
        self.block(); self.engine.tick()
        adapter.pump_once(); adapter.pump_once()
        self.assertEqual(len(sender.sent),1)
        self.assertIn('Known input conflict',sender.sent[0][2])
        run=self.inbox.workflows.get(self.rid)
        self.inbox.workflows.act({'run_id':self.rid,'revision':run['revision'],'action':'retry'})
        self.engine.tick(); adapter.pump_once(); self.engine.tick()
        task=self.store.task(self.tid)
        self.assertEqual(task['status'],'执行中')
        self.assertEqual(len(self.store.tasks()),1)
        self.assertEqual(len(self.fake.messages[task['thread_id']]),1)

    def test_unrelated_task_and_approval_are_unchanged_by_pause(self):
        _, result=self.store.submit(self.conn,{'message':'other'},'other-request')
        self.store.set_task(result['task_id'],'需要你处理','Permission required','approval_pending')
        before=self.store.task(result['task_id'])
        self.block(); self.engine._reconcile_workflow_queue()
        self.assertEqual(self.store.task(result['task_id']),before)

    def test_normal_message_with_workflow_text_is_not_superseded(self):
        self.store.submit(self.conn,{'message':self.item['prompt']},'unrelated-request')
        self.advance(); self.engine._reconcile_workflow_queue()
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT delivery FROM messages WHERE request_id='unrelated-request'").fetchone()[0],'queued')

if __name__ == '__main__':unittest.main()
