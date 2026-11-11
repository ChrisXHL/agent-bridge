"""Receiver -> real task store -> engine regression tests, no live Feishu sends."""
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from codex_http_bridge import BridgeError
from feishu_worker import FeishuAdapter, Inbox
from service import Store, Engine
from test_feishu import FakeSender, event
from test_service import FakeTransport


class StoreClient:
    def __init__(self, store, connection):
        self.store,self.connection=store,connection
        self.lose_receipt=False

    def submit(self,message,request_id,task_id=None,*,continue_from=None):
        payload={'message':message}
        if task_id:payload['task_id']=task_id
        if continue_from:payload['continue_from']=continue_from
        result=self.store.submit(self.store.connection(self.connection['id'],reveal=True),payload,request_id)[1]
        if self.lose_receipt:
            self.lose_receipt=False
            raise RuntimeError('response lost after durable acceptance')
        return result

    def task(self,tid):return self.store.task(tid,self.connection['id'])

    def backend(self):return self.store.connection(self.connection['id'])['execution_backend']


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        project=self.root/'project';project.mkdir()
        mapping=self.root/'projects.json';mapping.write_text(json.dumps({'p':str(project)}))
        self.store=Store(self.root/'bridge.sqlite3',mapping,self.root/'missing.sqlite3')
        self.conn=self.store.create_connection('private fixture','p')
        self.config={'app_id':'cli_fixture','connection_id':self.conn['id'],'bot_open_id':'ou_bot',
            'allowed_open_ids':['ou_owner','ou_second'],'allowed_chat_ids':['oc_team']}
        self.client=StoreClient(self.store,self.conn);self.sender=FakeSender()
        self.inbox=Inbox(self.root/'inbox.sqlite3')
        self.adapter=FeishuAdapter(self.config,self.inbox,self.client,self.sender)
        self.transport=FakeTransport();self.engine=Engine(self.store,self.transport)

    def tearDown(self):self.tmp.cleanup()

    def receive(self,mid,text='继续',**kw):
        self.assertTrue(self.adapter.handle_event(event(mid,text=text,**kw)))
        self.adapter.pump_once()
        r=self.inbox.linked_source(mid)
        return self.store.task(r['task_id']) if r['task_id'] else None

    def complete(self,task,result='完成'):
        self.engine.tick()
        task=self.store.task(task['id'])
        self.transport.complete(task['thread_id'],result);self.engine.tick()
        self.adapter.pump_once()
        return self.store.task(task['id'])

    def test_unquoted_turns_keep_native_thread_without_changing_completed_records(self):
        first=self.complete(self.receive('om_1','我的项目叫白鹭'),'记住了')
        second=self.receive('om_2','刚才的项目叫什么？')
        self.assertNotEqual(first['id'],second['id'])
        self.assertEqual(first['thread_id'],second['thread_id'])
        self.complete(second,'白鹭')
        third=self.complete(self.receive('om_3','给它写简介'),'项目简介')
        self.assertEqual(third['thread_id'],first['thread_id'])
        self.assertEqual(self.transport.created,1)
        self.assertEqual(self.store.task(first['id']),first)
        self.assertEqual([x[1] for x in self.sender.sent].count('received'),3)
        self.assertEqual([x[1] for x in self.sender.sent].count('已完成'),3)

    def test_switch_backend_keeps_context_without_resuming_old_cli(self):
        first=self.complete(self.receive('om_before_switch','项目叫白鹭'),'已记住白鹭')
        self.store.set_execution_backend([self.conn['id']],'claude')
        self.engine.claude_transport=FakeTransport()
        second=self.receive('om_after_switch','刚才项目叫什么？')
        self.engine.tick()
        second=self.store.task(second['id'])
        self.assertEqual(second['execution_backend'],'claude')
        self.assertEqual(self.engine.claude_transport.created,1)
        self.assertEqual(self.transport.created,1)
        self.assertIn('连续对话修复',second['messages'][0]['content'])
        self.assertEqual(self.store.task(first['id']),first)

    def test_text_and_image_burst_is_serialized_and_image_context_survives(self):
        self.sender.download_resource=lambda mid,key,kind,out,limit:out.write(b'fixture-image')
        image=event('om_image',message_type='image')
        image['event']['message']['content']=json.dumps({'image_key':'img_fixture'})
        self.adapter.handle_event(image)
        self.adapter.handle_event(event('om_caption',text='照刚才的人像做封面'))
        self.adapter.pump_once()
        first=self.store.task(self.inbox.linked_source('om_image')['task_id'])
        self.assertIsNone(self.inbox.linked_source('om_caption')['task_id'])
        self.complete(first,'已看过人像')
        caption=self.store.task(self.inbox.linked_source('om_caption')['task_id'])
        self.assertEqual(caption['thread_id'],self.store.task(first['id'])['thread_id'])
        self.assertIn('媒体附件',self.transport.messages[caption['thread_id']][0])

    def test_restart_and_lost_http_receipt_do_not_duplicate_or_change_payload(self):
        first=self.complete(self.receive('om_1'))
        self.client.lose_receipt=True
        self.receive('om_2','记得我刚才说什么吗？')
        frozen=self.inbox.private_submission('om_2')
        self.config['prompt_template']='新的规则 {{message}}'
        self.adapter=FeishuAdapter(self.config,Inbox(self.inbox.path),self.client,self.sender)
        self.adapter.pump_once()
        second=self.store.task(self.inbox.linked_source('om_2')['task_id'])
        self.assertEqual(second['thread_id'],first['thread_id'])
        self.assertEqual(second['messages'][0]['content'],frozen['message'])
        self.assertEqual(len(self.store.tasks()),2)

    def test_new_conversation_resets_and_followup_stays_in_new_thread(self):
        first=self.complete(self.receive('om_1','旧材料'))
        new=self.complete(self.receive('om_reset','新对话'))
        self.assertNotEqual(new['thread_id'],first['thread_id'])
        follow=self.receive('om_new','你好')
        self.assertEqual(follow['thread_id'],new['thread_id'])
        self.assertNotIn('连续对话修复',follow['messages'][0]['content'])

    def test_private_users_chats_apps_and_connections_are_isolated(self):
        first=self.complete(self.receive('om_1','不应串给别人的内容'))
        for mid,kw in [('om_user',{'sender':'ou_second'}),('om_chat',{'chat':'oc_other'})]:
            other=self.complete(self.receive(mid,**kw))
            self.assertNotEqual(first['thread_id'],other['thread_id'])
        self.config['app_id']='cli_other'
        other=self.complete(self.receive('om_app'))
        self.assertNotEqual(other['thread_id'],first['thread_id'])
        self.config['app_id']='cli_fixture'
        conn=self.store.create_connection('other','p')
        self.config['connection_id']=conn['id'];self.client.connection=conn
        other=self.complete(self.receive('om_connection'))
        self.assertNotEqual(other['thread_id'],first['thread_id'])

    def test_quote_branches_without_mutating_original_task(self):
        first=self.complete(self.receive('om_1','第一件事'),'第一件结果')
        second=self.complete(self.receive('om_2','第二件事'),'第二件结果')
        branch=self.receive('om_branch','改第一件事',parent='om_1')
        self.assertEqual(branch['thread_id'],first['thread_id'])
        self.complete(branch,'修改结果')
        self.assertEqual(self.store.task(first['id']),first)
        self.assertEqual(self.store.task(second['id']),second)

    def test_migration_reads_split_history_and_attachments_without_replaying(self):
        self.sender.download_resource=lambda mid,key,kind,out,limit:out.write(b'old-image-fixture')
        post=event('om_old_1',message_type='post')
        post['event']['message']['content']=json.dumps({'content':[
            [{'tag':'text','text':'最初的问题'}],[{'tag':'img','image_key':'img_old'}]]})
        self.assertTrue(self.adapter.handle_event(post));self.adapter.pump_once()
        first=self.complete(self.store.task(self.inbox.linked_source('om_old_1')['task_id']),'第一轮结果')
        # Simulate pre-fix ordinary messages arriving in independent Codex chats.
        with self.inbox.db() as db:
            route=json.loads(db.execute('SELECT route_json FROM inbound WHERE message_id=?',('om_old_1',)).fetchone()[0])
            route.pop('private_conversation_v1')
            db.execute('UPDATE inbound SET route_json=? WHERE message_id=?',(json.dumps(route),'om_old_1'))
        self.adapter.handle_event(event('om_old_2',text='第二轮的问题'))
        _,second=self.store.submit(self.conn,{'message':'第二轮的问题'},'legacy-split-002')
        self.inbox.mark_submitted('om_old_2',second['task_id'])
        second=self.complete(self.store.task(second['task_id']),'第二轮结果')
        with self.inbox.db() as db:
            db.execute('UPDATE inbound SET route_json=? WHERE message_id=?',(json.dumps(route),'om_old_2'))
        new=self.receive('om_after','综合前面的问题')
        self.assertEqual(new['thread_id'],second['thread_id'])
        files=list((self.root/'conversations').glob('*.json'))
        self.assertEqual(len(files),1)
        history=json.loads(files[0].read_text())
        self.assertEqual(history['messages'][0]['user'],'最初的问题')
        self.assertEqual(history['messages'][0]['assistant'],'第一轮结果')
        attachment=history['messages'][0]['attachments'][0]
        self.assertEqual(Path(attachment['path']).read_bytes(),b'old-image-fixture')
        self.assertIn(str(files[0]),new['messages'][0]['content'])
        self.assertEqual(files[0].stat().st_mode&0o777,0o600)
        self.assertEqual(self.store.task(first['id']),first)
        self.assertEqual(self.store.task(second['id']),second)

    def test_rules_are_current_and_bare_new_word_is_not_a_reset(self):
        first=self.complete(self.receive('om_1'))
        self.config['prompt_template']='新的规则：{{message}}'
        follow=self.receive('om_2','新对话功能怎么用？')
        self.assertEqual(first['thread_id'],follow['thread_id'])
        self.assertTrue(follow['messages'][0]['content'].startswith('新的规则：'))

    def test_near_limit_request_is_kept_complete_when_context_is_added(self):
        self.complete(self.receive('om_1','前文'))
        # Valid original intake, with little room for continuity metadata.
        text='a'*11840
        follow=self.receive('om_large',text)
        self.assertIsNotNone(follow)
        files=list((self.root/'conversations').glob('*.request.json'))
        self.assertEqual(len(files),1)
        self.assertIn(text,json.loads(files[0].read_text())['current_request'])
        self.assertIn(str(files[0]),follow['messages'][0]['content'])

    def test_group_roots_remain_separate_and_never_read_private_history(self):
        private=self.complete(self.receive('om_dm','我的私聊资料'))
        mentions=[{'id':{'open_id':'ou_bot'},'key':'@_user_1'}]
        a=self.complete(self.receive('om_g1','@_user_1 一件事',chat_type='group',chat='oc_team',mentions=mentions))
        b=self.receive('om_g2','@_user_1 另一件事',chat_type='group',chat='oc_team',mentions=mentions)
        self.assertIsNone(b['thread_id'])
        self.assertNotEqual(a['thread_id'],private['thread_id'])
        self.assertNotIn('连续私聊',b['messages'][0]['content'])

    def test_server_rejects_cross_connection_and_parallel_thread_continuation(self):
        first=self.receive('om_1')
        with self.assertRaises(BridgeError):
            self.store.submit(self.conn,{'message':'parallel','continue_from':first['id']},'invalid-parallel')
        first=self.complete(first)
        other=self.store.create_connection('other','p')
        with self.assertRaises(BridgeError):
            self.store.submit(other,{'message':'wrong user','continue_from':first['id']},'invalid-cross-scope')
        self.receive('om_2')
        with self.assertRaises(BridgeError):
            self.store.submit(self.conn,{'message':'same thread busy','continue_from':first['id']},'invalid-thread-busy')

    def test_archived_tasks_are_rejected_even_for_private_continuation(self):
        first=self.complete(self.receive('om_archived'))
        with patch.object(self.store.history,'tasks',return_value={first['id']:{'archived':True}}):
            with self.assertRaises(BridgeError) as raised:
                self.store.submit(self.conn,{'message':'不得恢复旧任务','continue_from':first['id']},'invalid-archive')
        self.assertEqual(raised.exception.code,'historical_task')
        self.assertEqual(self.store.task(first['id']),first)

    def test_failed_turn_without_a_thread_still_keeps_original_request_as_context(self):
        first=self.receive('om_failed','尚未送达的完整要求')
        self.store.set_task(first['id'],'失败','未能启动 Codex','codex_unavailable')
        with self.store.db() as db:
            db.execute("UPDATE messages SET delivery='failed' WHERE task_id=?",(first['id'],))
        follow=self.receive('om_retry','重试刚才的要求')
        self.assertIsNotNone(follow)
        files=list((self.root/'conversations').glob('*.json'))
        self.assertEqual(json.loads(files[0].read_text())['messages'][0]['user'],'尚未送达的完整要求')


if __name__=='__main__':unittest.main()
