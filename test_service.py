import http.client
import json
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from pathlib import Path
from service import Store, Engine, make_handler, open_claude_task_conversation

class FakeTransport:
    def __init__(self): self.created=0; self.messages={}; self.turns={}; self.resumed=[]; self.released=0
    def start_thread(self, cwd):
        self.created+=1; tid=f'thread-{self.created}'; self.messages[tid]=[]; return tid
    def send(self, thread_id, message):
        self.messages[thread_id].append(message)
        self.turns[thread_id]=[{'status':'inProgress','items':[{'type':'userMessage','content':[{'type':'text','text':message}]}]}]
        return f'turn-{len(self.messages[thread_id])}'
    def read(self, thread_id): return {'turns':self.turns.get(thread_id,[])}
    def resume_thread(self, thread_id): self.resumed.append(thread_id)
    def release_idle(self): self.released+=1
    def complete(self, thread_id, result):
        self.turns[thread_id][-1]['status']='completed'
        self.turns[thread_id][-1]['items'].append({'type':'agentMessage','text':result})
    def close(self): pass

class BridgeMVPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); root=Path(self.tmp.name); project=root/'project'; project.mkdir()
        mapping=root/'projects.json'; mapping.write_text(json.dumps({'project-1':str(project)}))
        self.codex_state=root/'state_5.sqlite'
        self.store=Store(root/'test.sqlite3',mapping,self.codex_state); self.fake=FakeTransport(); self.engine=Engine(self.store,self.fake)
        self.connection=self.store.create_connection('守望系统','project-1')
    def tearDown(self): self.tmp.cleanup()
    def test_register_existing_folder_for_new_tasks(self):
        folder=Path(self.tmp.name)/'another-workspace';folder.mkdir()
        original=json.loads(self.store.projects_path.read_text())
        project=self.store.register_project_path(str(folder))
        self.assertEqual(project['path'],str(folder.resolve()))
        self.assertEqual(self.store.register_project_path(str(folder))['id'],project['id'])
        self.assertEqual(self.store.register_project_path(str(Path(self.tmp.name)/'project'))['id'],'project-1')
        self.assertEqual(json.loads(self.store.projects_path.read_text()),{**original,project['id']:str(folder.resolve())})
        connection=self.store.create_connection('另一个机器人',project['id'])
        _,task=self.store.submit(self.store.authenticate('Bearer '+connection['api_key']),{'message':'处理新文件夹'},'folder-task-001')
        with patch.object(self.fake,'start_thread',wraps=self.fake.start_thread) as start:
            self.engine.tick()
            start.assert_called_once_with(str(folder.resolve()))
        self.assertEqual(self.store.task(task['task_id'])['status'],'执行中')
        self.assertEqual(self.store.project(project['id'])['path'],str(folder.resolve()))
        self.assertEqual(self.store.connection(self.connection['id'])['project_id'],'project-1')
        with self.assertRaisesRegex(Exception,'文件夹'):
            self.store.register_project_path(str(folder/'missing'))

    def test_panel_registers_folder_through_local_api(self):
        folder=Path(self.tmp.name)/'panel-folder';folder.mkdir()
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.store,self.engine,'http://127.0.0.1:0'))
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
            conn.request('POST','/api/projects',json.dumps({'path':str(folder)}),
                         {'Content-Type':'application/json'})
            response=conn.getresponse();body=json.loads(response.read());conn.close()
            self.assertEqual(response.status,200)
            self.assertEqual(body['path'],str(folder.resolve()))
            self.assertEqual(self.store.project(body['id'])['path'],str(folder.resolve()))
        finally:
            server.shutdown();server.server_close();worker.join()

    def test_panel_native_folder_picker_selects_and_can_cancel(self):
        folder=Path(self.tmp.name)/'picked-folder';folder.mkdir()
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.store,self.engine,'http://127.0.0.1:0'))
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        def request(origin=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
            headers={'Content-Type':'application/json'}
            if origin:headers['Origin']=origin
            conn.request('POST','/api/projects/pick',json.dumps({'project_id':'project-1'}),headers)
            response=conn.getresponse();body=json.loads(response.read());conn.close()
            return response.status,body
        try:
            with patch('service.sys.platform','darwin'), patch('service.subprocess.run',
                return_value=subprocess.CompletedProcess([],0,str(folder)+'\n','')) as picker:
                status,result=request('http://127.0.0.1:0')
            self.assertEqual(status,200)
            self.assertEqual(result['project']['path'],str(folder.resolve()))
            self.assertIn(result['project'],result['projects'])
            self.assertIn('choose folder',' '.join(picker.call_args.args[0]))
            with patch('service.sys.platform','darwin'), patch('service.subprocess.run',
                return_value=subprocess.CompletedProcess([],1,'','User canceled. (-128)')):
                status,result=request('http://127.0.0.1:0')
            self.assertEqual((status,result),(200,{'cancelled':True}))
            status,result=request('http://other-site.example')
            self.assertEqual(status,403)
            self.assertEqual(result['error']['code'],'origin_forbidden')
        finally:
            server.shutdown();server.server_close();worker.join()

    def test_open_claude_task_resumes_exact_session_in_terminal(self):
        self.store.set_execution_backend([self.connection['id']],'claude')
        connection=self.store.authenticate('Bearer '+self.connection['api_key'])
        _,created=self.store.submit(connection,{'message':'原任务'},'claude-open-001')
        thread_id=str(uuid.uuid4())
        with self.store.db() as db:
            db.execute("UPDATE tasks SET thread_id=?,status='已完成' WHERE id=?",(thread_id,created['task_id']))
        with sqlite3.connect(Path(self.tmp.name)/'claude-threads.sqlite3') as db:
            db.execute('CREATE TABLE threads (id TEXT PRIMARY KEY,cwd TEXT,session_started INTEGER)')
            db.execute('CREATE TABLE turns (thread_id TEXT,status TEXT)')
            db.execute('INSERT INTO threads VALUES (?,?,1)',(thread_id,str(Path(self.tmp.name)/'project')))
        with patch('service.sys.platform','darwin'), patch('claude_runtime.resolve_claude_binary',return_value='/usr/local/bin/claude'), patch('service.subprocess.run',return_value=subprocess.CompletedProcess([],0,'','')) as launch:
            result=open_claude_task_conversation(self.store,created['task_id'])
        self.assertEqual(result,{'opened':True})
        command=' '.join(launch.call_args.args[0])
        self.assertIn('tell application "Terminal"',command)
        self.assertIn('--resume '+thread_id,command)
        self.assertIn(str(Path(self.tmp.name)/'project'),command)
        with self.store.db() as db:
            db.execute("UPDATE tasks SET status='执行中' WHERE id=?",(created['task_id'],))
        with patch('service.subprocess.run') as launch:
            with self.assertRaisesRegex(Exception,'正在执行'):
                open_claude_task_conversation(self.store,created['task_id'])
            launch.assert_not_called()

    def test_open_conversation_requires_local_panel_origin(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.store,self.engine,'http://127.0.0.1:0'))
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
            conn.request('POST','/api/tasks/not-a-task/open-conversation','{}',
                         {'Content-Type':'application/json'})
            response=conn.getresponse();body=json.loads(response.read());conn.close()
            self.assertEqual(response.status,403)
            self.assertEqual(body['error']['code'],'local_only')
        finally:
            server.shutdown();server.server_close();worker.join()

    def test_remove_connection_preserves_history_and_can_restore(self):
        connection=self.store.authenticate('Bearer '+self.connection['api_key'])
        _,created=self.store.submit(connection,{'message':'保留这条任务'},'remove-history-001')
        with self.assertRaisesRegex(Exception,'未结束的任务'):
            self.store.remove_connection(connection['id'])
        self.store.set_task(created['task_id'],'已完成','任务结果')
        before=self.store.task(created['task_id'])
        removed=self.store.remove_connection(connection['id'])
        self.assertIsNotNone(removed['deleted_at'])
        self.assertEqual(removed['status'],'已停用')
        self.assertNotIn(connection['id'],[item['id'] for item in self.store.connections()])
        self.assertIn(connection['id'],[item['id'] for item in self.store.connections(deleted=True)])
        with self.assertRaisesRegex(Exception,'已停用'):
            self.store.authenticate('Bearer '+self.connection['api_key'])
        with self.assertRaisesRegex(Exception,'已停用'):
            self.store.submit(connection,{'message':'删除后不能再接收'},'remove-block-002')
        with self.assertRaisesRegex(Exception,'先恢复'):
            self.store.update_connection(connection['id'],{'status':'可用'})
        self.assertEqual(self.store.task(created['task_id']),before)
        restored=self.store.restore_connection(connection['id'])
        self.assertIsNone(restored['deleted_at'])
        self.assertEqual(restored['status'],connection['status'])
        self.assertEqual(self.store.authenticate('Bearer '+self.connection['api_key'])['id'],connection['id'])

    def test_remove_connection_http_rejects_feishu_and_requires_local_origin(self):
        root=Path(self.tmp.name)
        settings=root/'feishu.json'
        settings.write_text(json.dumps({'app_id':'cli_test','connection_id':self.connection['id']}))
        other=self.store.create_connection('普通连接','project-1')
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(
            self.store,self.engine,'http://127.0.0.1:0',
            feishu_settings_path=settings,feishu_inbox_path=root/'inbox.sqlite3'))
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        def request(cid,action,origin=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
            headers={'Content-Type':'application/json'}
            if origin:headers['Origin']=origin
            conn.request('POST',f'/api/connections/{cid}/{action}','{}',headers)
            response=conn.getresponse();body=json.loads(response.read());conn.close()
            return response.status,body
        try:
            status,body=request(other['id'],'remove')
            self.assertEqual((status,body['error']['code']),(403,'local_only'))
            status,body=request(self.connection['id'],'remove','http://127.0.0.1:0')
            self.assertEqual((status,body['error']['code']),(409,'feishu_connection'))
            self.assertIsNone(self.store.connection(self.connection['id'])['deleted_at'])
            status,body=request(other['id'],'remove','http://127.0.0.1:0')
            self.assertEqual(status,200);self.assertIsNotNone(body['deleted_at'])
            status,body=request(other['id'],'restore','http://127.0.0.1:0')
            self.assertEqual(status,200);self.assertIsNone(body['deleted_at'])
        finally:
            server.shutdown();server.server_close();worker.join()
    def test_new_and_followup_reuse_thread_and_verify_connection(self):
        conn=self.store.authenticate('Bearer '+self.connection['api_key'])
        code,first=self.store.submit(conn,{'message':'检查 README'},'request-001'); self.assertEqual(code,202)
        self.engine.tick(); task=self.store.task(first['task_id'])
        self.assertEqual(task['status'],'执行中'); self.assertEqual(self.store.connection(conn['id'])['status'],'可用')
        self.assertEqual(self.fake.created,1)
        _,duplicate=self.store.submit(conn,{'message':'检查 README'},'request-001')
        self.assertTrue(duplicate['duplicate']); self.assertEqual(self.fake.created,1)
        self.fake.complete(task['thread_id'],'初步完成'); self.engine.tick()
        self.assertEqual(self.store.task(task['id'])['status'],'已完成')
        self.assertGreater(self.fake.released,0)
        _,followup=self.store.submit(conn,{'task_id':task['id'],'message':'再检查一遍'},'request-002')
        self.engine.tick(); updated=self.store.task(followup['task_id'])
        self.assertEqual(updated['thread_id'],task['thread_id'])
        self.assertEqual(self.fake.resumed,[task['thread_id']])
        self.assertEqual(self.fake.messages[task['thread_id']],['检查 README','再检查一遍'])
        self.assertEqual(self.fake.created,1); self.assertEqual(len(updated['messages']),2)

    def test_backend_switch_pins_old_task_and_starts_new_claude_thread(self):
        claude=FakeTransport()
        self.engine.claude_transport=claude
        old_connection=self.store.authenticate('Bearer '+self.connection['api_key'])
        _,first=self.store.submit(old_connection,{'message':'old job'},'backend-old-001')
        self.engine.tick()
        old=self.store.task(first['task_id'])
        self.assertEqual(old['execution_backend'],'codex')
        self.fake.complete(old['thread_id'],'done')
        self.engine.tick()
        self.store.set_execution_backend([old_connection['id']],'claude')
        new_connection=self.store.authenticate('Bearer '+self.connection['api_key'])
        _,second=self.store.submit(new_connection,{'message':'new job','continue_from':old['id']},'backend-new-002')
        self.engine.tick()
        new=self.store.task(second['task_id'])
        self.assertEqual(new['execution_backend'],'claude')
        self.assertEqual(claude.created,1)
        self.assertEqual(self.fake.created,1)
        self.assertEqual(claude.messages[new['thread_id']],['new job'])
        self.assertEqual(self.store.task(old['id'])['execution_backend'],'codex')
    def test_scope_and_idempotency(self):
        conn=self.store.authenticate('Bearer '+self.connection['api_key'])
        _,created=self.store.submit(conn,{'message':'one'},'request-001')
        other=self.store.create_connection('自动化脚本','project-1')
        other_conn=self.store.authenticate('Bearer '+other['api_key'])
        with self.assertRaises(Exception): self.store.submit(other_conn,{'task_id':created['task_id'],'message':'two'},'request-002')
        with self.assertRaises(Exception): self.store.submit(conn,{'message':'different'},'request-001')
        self.assertEqual(self.fake.created,0)
    def test_queued_while_desktop_closed_then_delivered(self):
        conn=self.store.authenticate('Bearer '+self.connection['api_key'])
        _,created=self.store.submit(conn,{'message':'queued'},'request-closed-001')
        with patch.object(Engine,'_codex_desktop_open',return_value=False):
            self.engine.tick()
        self.assertEqual(self.store.task(created['task_id'])['status'],'等待 Codex')
        self.assertEqual(self.fake.created,0)
        self.engine.tick()
        self.assertEqual(self.fake.created,1)
    def test_active_limit_queues_new_tasks_until_a_turn_finishes(self):
        conn=self.store.authenticate('Bearer '+self.connection['api_key'])
        ids=[]
        for index in range(6):
            _,created=self.store.submit(conn,{'message':f'job {index}'},f'request-limit-{index}')
            ids.append(created['task_id'])
        for _ in range(6):
            self.engine.tick()
        self.assertEqual(self.fake.created,5)
        queued=self.store.task(ids[5])
        self.assertEqual(queued['status'],'等待 Codex')
        self.assertEqual(queued['messages'][0]['delivery'],'queued')
        first=self.store.task(ids[0])
        self.fake.complete(first['thread_id'],'done')
        self.engine.tick()
        self.assertEqual(self.store.task(ids[0])['status'],'已完成')
        self.assertEqual(self.store.task(ids[5])['status'],'执行中')
        self.assertEqual(self.fake.created,6)
    def test_unresolved_receipt_does_not_consume_execution_slot_or_change_history(self):
        _,old=self.store.submit(self.connection,{'message':'old uncertain receipt'},'request-old-uncertain')
        with self.store.db() as db:
            db.execute("UPDATE messages SET delivery='uncertain' WHERE task_id=?",(old['task_id'],))
            db.execute("UPDATE tasks SET status='需要你处理',error_code='delivery_uncertain' WHERE id=?",(old['task_id'],))
        before=self.store.task(old['task_id'])
        self.engine.max_active=1
        _,new=self.store.submit(self.connection,{'message':'new ordinary group reply'},'request-new-after-uncertain')
        self.engine.tick()
        self.assertEqual(self.store.task(new['task_id'])['status'],'执行中')
        self.assertEqual(self.store.task(old['task_id']),before)

    def test_real_pending_approval_still_reserves_slot(self):
        _,old=self.store.submit(self.connection,{'message':'old awaiting approval'},'request-old-approval-slot')
        with self.store.db() as db:
            db.execute("UPDATE messages SET delivery='uncertain' WHERE task_id=?",(old['task_id'],))
            db.execute("UPDATE tasks SET status='需要你处理',error_code='delivery_uncertain' WHERE id=?",(old['task_id'],))
            db.execute("INSERT INTO approvals VALUES ('approval-slot',?,'command','confirm','pending',0,0)",(old['task_id'],))
        self.engine.max_active=1
        _,new=self.store.submit(self.connection,{'message':'new job'},'request-after-real-approval')
        self.engine.tick()
        self.assertEqual(self.store.task(new['task_id'])['status'],'等待 Codex')
        self.assertEqual(self.fake.created,0)

    def test_unavailable_project_waits_and_delivers_after_path_returns(self):
        conn=self.store.authenticate('Bearer '+self.connection['api_key'])
        _,created=self.store.submit(conn,{'message':'check'},'request-moved-001')
        project=Path(self.store.projects()[0]['path']); project.rmdir()
        self.engine.tick()
        task=self.store.task(created['task_id'])
        self.assertEqual(task['status'],'等待 Codex')
        self.assertEqual(task['error_code'],'project_unavailable')
        self.assertEqual(task['messages'][0]['delivery'],'queued')
        self.assertEqual(self.fake.created,0)
        self.assertIn('project-1',[p['id'] for p in self.store.projects()])
        project.mkdir()
        self.engine.tick()
        task=self.store.task(created['task_id'])
        self.assertEqual(task['status'],'执行中')
        self.assertIsNone(task['error_code'])
        self.assertIsNone(task['result'])
        self.assertEqual(task['messages'][0]['delivery'],'delivered')
        self.assertEqual(self.fake.created,1)
    def test_unavailable_project_does_not_block_another_connection(self):
        first=self.store.authenticate('Bearer '+self.connection['api_key'])
        other=Path(self.tmp.name)/'other'; other.mkdir()
        mapping=json.loads(self.store.projects_path.read_text())
        mapping['project-2']=str(other)
        self.store.projects_path.write_text(json.dumps(mapping))
        second_connection=self.store.create_connection('另一个来源','project-2')
        second=self.store.authenticate('Bearer '+second_connection['api_key'])
        _,blocked=self.store.submit(first,{'message':'first'},'request-blocked-001')
        _,ready=self.store.submit(second,{'message':'second'},'request-ready-001')
        Path(mapping['project-1']).rmdir()
        self.engine.tick()
        self.assertEqual(self.store.task(blocked['task_id'])['status'],'等待 Codex')
        self.assertEqual(self.store.task(ready['task_id'])['status'],'执行中')
        self.assertEqual(self.fake.created,1)
    def test_codex_project_discovery_outage_waits_and_recovers(self):
        with sqlite3.connect(self.codex_state) as db:
            db.execute('CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, position INTEGER, created_at_ms INTEGER)')
            db.execute('CREATE TABLE project_roots (project_id TEXT, position INTEGER, path TEXT)')
            project=Path(self.tmp.name)/'codex-project'; project.mkdir()
            db.execute('INSERT INTO projects VALUES (?,?,?,?)',('codex-new','Codex project',0,1))
            db.execute('INSERT INTO project_roots VALUES (?,?,?)',('codex-new',0,str(project)))
        connection=self.store.create_connection('Codex 项目','codex-new')
        conn=self.store.authenticate('Bearer '+connection['api_key'])
        _,created=self.store.submit(conn,{'message':'check'},'request-discovery-001')
        with patch.object(self.store,'_codex_projects',return_value=[]):
            self.engine.tick()
        self.assertEqual(self.store.task(created['task_id'])['status'],'等待 Codex')
        self.assertEqual(self.fake.created,0)
        self.engine.tick()
        self.assertEqual(self.store.task(created['task_id'])['status'],'执行中')
        self.assertEqual(self.fake.created,1)
    def test_configured_project_dispatch_does_not_depend_on_codex_state_db(self):
        conn=self.store.authenticate('Bearer '+self.connection['api_key'])
        _,created=self.store.submit(conn,{'message':'check'},'request-configured-001')
        with patch.object(self.store,'_codex_projects',side_effect=AssertionError('project database should not be read')):
            self.engine.tick()
        self.assertEqual(self.store.task(created['task_id'])['status'],'执行中')
        self.assertEqual(self.fake.created,1)
    def test_new_codex_project_is_discovered_and_can_receive_connection(self):
        with sqlite3.connect(self.codex_state) as db:
            db.execute('CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, position INTEGER, created_at_ms INTEGER)')
            db.execute('CREATE TABLE project_roots (project_id TEXT, position INTEGER, path TEXT)')
            new_path=Path(self.tmp.name)/'new-project'; new_path.mkdir()
            db.execute('INSERT INTO projects VALUES (?,?,?,?)',('codex-new','New project',0,1))
            db.execute('INSERT INTO project_roots VALUES (?,?,?)',('codex-new',0,str(new_path)))
        projects=self.store.projects()
        self.assertEqual(len(projects),2)
        self.assertEqual(projects[1],{'id':'codex-new','name':'New project','path':str(new_path)})
        created=self.store.create_connection('新来源','codex-new')
        self.assertEqual(created['project_name'],'New project')
        # A second project is visible without restarting the service.
        another=Path(self.tmp.name)/'another'; another.mkdir()
        with sqlite3.connect(self.codex_state) as db:
            db.execute('INSERT INTO projects VALUES (?,?,?,?)',('codex-later','Later project',1,2))
            db.execute('INSERT INTO project_roots VALUES (?,?,?)',('codex-later',0,str(another)))
        self.assertIn('codex-later',[p['id'] for p in self.store.projects()])
    def test_codex_discovery_preserves_existing_project_id_and_rejects_missing_root(self):
        configured_path=self.store.projects()[0]['path']
        with sqlite3.connect(self.codex_state) as db:
            db.execute('CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, position INTEGER, created_at_ms INTEGER)')
            db.execute('CREATE TABLE project_roots (project_id TEXT, position INTEGER, path TEXT)')
            db.execute('INSERT INTO projects VALUES (?,?,?,?)',('codex-same','Same project',0,1))
            db.execute('INSERT INTO project_roots VALUES (?,?,?)',('codex-same',0,configured_path))
            db.execute('INSERT INTO projects VALUES (?,?,?,?)',('codex-missing','Missing',1,2))
            db.execute('INSERT INTO project_roots VALUES (?,?,?)',('codex-missing',0,str(Path(self.tmp.name)/'missing')))
        self.assertEqual([p['id'] for p in self.store.projects()],['project-1'])
        with self.assertRaises(Exception): self.store.create_connection('新来源','codex-missing')
    def test_codex_approval_surfaces_and_user_decision_returns_to_same_process(self):
        conn=self.store.authenticate('Bearer '+self.connection['api_key'])
        _,created=self.store.submit(conn,{'message':'check'},'request-approval-001')
        self.engine.tick()
        task=self.store.task(created['task_id'])
        class Client:
            def __init__(self): self.responses=[]
            def _write(self, message): self.responses.append(message)
        self.fake.client=Client()
        request={'id':77,'method':'item/commandExecution/requestApproval',
                 'params':{'threadId':task['thread_id'],'command':'test command'}}
        self.assertTrue(self.engine._on_server_request(request))
        pending=self.store.task(task['id'])
        self.assertEqual(pending['status'],'需要你处理')
        self.assertEqual(pending['approval']['detail'],'test command')
        self.engine.resolve_approval(pending['approval']['id'],'decline')
        self.assertEqual(self.fake.client.responses,[{'id':77,'result':{'decision':'decline'}}])
        self.assertIsNone(self.store.task(task['id'])['approval'])
    def test_approval_arriving_during_delivery_stays_visible(self):
        conn=self.store.authenticate('Bearer '+self.connection['api_key'])
        _,created=self.store.submit(conn,{'message':'check'},'request-approval-002')
        send=self.fake.send
        def send_with_approval(thread_id,message):
            turn_id=send(thread_id,message)
            self.engine._on_server_request({'id':78,'method':'item/fileChange/requestApproval',
                                            'params':{'threadId':thread_id,'reason':'确认文件修改'}})
            return turn_id
        self.fake.send=send_with_approval
        self.engine.tick()
        task=self.store.task(created['task_id'])
        self.assertEqual(task['status'],'需要你处理')
        self.assertEqual(task['approval']['detail'],'确认文件修改')
        self.assertEqual(task['messages'][0]['delivery'],'delivered')
    def test_http_panel_external_task_and_status(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.store,self.engine,'http://127.0.0.1:0'))
        worker=threading.Thread(target=server.serve_forever,daemon=True); worker.start()
        try:
            def request(method,path,payload=None,token=None):
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
                headers={'Content-Type':'application/json'}
                if token: headers['Authorization']='Bearer '+token
                conn.request(method,path,json.dumps(payload) if payload is not None else None,headers)
                res=conn.getresponse(); body=res.read(); conn.close()
                return res.status,json.loads(body) if res.getheader('Content-Type','').startswith('application/json') else body
            self.assertEqual(request('GET','/')[0],200)
            self.assertEqual(request('POST','/v1/tasks',{'message':'hello','request_id':'request-001'})[0],401)
            status,result=request('POST','/v1/tasks',{'message':'hello','request_id':'request-001'},self.connection['api_key'])
            self.assertEqual(status,202); self.engine.tick()
            _,tasks=request('GET','/api/tasks'); self.assertEqual(tasks[0]['id'],result['task_id'])
            self.assertEqual(tasks[0]['status'],'执行中')
            self.assertEqual(request('GET','/v1/tasks/'+result['task_id'],token=self.connection['api_key'])[1]['status'],'执行中')
        finally: server.shutdown(); server.server_close()
    def test_lan_http_panel_opens_and_checks_host_origin(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),
            make_handler(self.store,self.engine,'http://127.0.0.1:0','127.0.0.1:0'))
        worker=threading.Thread(target=server.serve_forever,daemon=True); worker.start()
        try:
            def request(method,path,authorization=None,origin=None,host='127.0.0.1:0'):
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port)
                headers={'Host':host}
                if authorization: headers['Authorization']=authorization
                if origin: headers['Origin']=origin
                conn.request(method,path,'{}' if method=='POST' else None,headers)
                res=conn.getresponse(); body=res.read(); status=res.status
                conn.close()
                return status,json.loads(body) if body[:1] in (b'{',b'[') else body
            self.assertEqual(request('GET','/panel')[0],200)
            self.assertEqual(request('GET','/api/connections')[1][0]['project_name'],'project')
            self.assertEqual(request('POST','/api/connections',origin='http://evil.test')[0],403)
            self.assertEqual(request('GET','/api/connections',host='evil.test')[0],403)
            self.assertEqual(request('GET','/healthz')[0],200)
            self.assertEqual(request('GET','/v1/tasks/missing')[0],401)
            self.assertEqual(request('GET','/v1/tasks/missing','Bearer '+self.connection['api_key'])[0],404)
        finally: server.shutdown(); server.server_close()
if __name__=='__main__': unittest.main()
