#!/usr/bin/env python3
"""Real Codex and HTTP intake, synthetic Feishu receipts, isolated local state."""
import json
import secrets
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from service import Store,Engine,LocalCodexTransport,make_handler
from feishu_worker import FeishuAdapter,BridgeClient,Inbox
from test_feishu import FakeSender,event


def main():
    evidence={'real_codex':True,'real_http_intake':True,'real_feishu_message':False,
              'live_config_changed':False,'stage':'starting'}
    target=ROOT/'artifacts/private-conversation-real-codex.json'
    def record(stage):
        evidence['stage']=stage;target.write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
        print(stage,flush=True)
    with tempfile.TemporaryDirectory(prefix='bridge-conversation-check-') as temp:
        root=Path(temp);project=root/'project';project.mkdir()
        mapping=root/'projects.json';mapping.write_text(json.dumps({'test':str(project)}))
        store=Store(root/'bridge.sqlite3',mapping,root/'no-discovery.sqlite3')
        connection=store.create_connection('isolated conversation check','test')
        transport=LocalCodexTransport();engine=Engine(store,transport)
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(store,None,'http://127.0.0.1:1'))
        base='http://127.0.0.1:'+str(server.server_port)
        server.RequestHandlerClass=make_handler(store,None,base)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        inbox=Inbox(root/'inbox.sqlite3');sender=FakeSender()
        config={'app_id':'cli_fixture','connection_id':connection['id'],'allowed_open_ids':['ou_owner'],
            'allowed_chat_ids':[],'prompt_template':'这是独立的连续对话测试。仅回答当前用户消息，简短回复，不执行飞书或其他外部操作。只使用本会话记忆；若 Bridge 附带历史资料文件，应实际读取该文件。没有附带文件时不要搜索文件来寻找测试答案。不知道就只回复不知道。\n{{message}}'}
        adapter=FeishuAdapter(config,inbox,BridgeClient(base,connection['api_key']),sender)
        def receive(mid,text,legacy=False,fresh=False):
            assert adapter.handle_event(event(mid,text=text))
            if legacy or fresh:
                with inbox.db() as db:
                    r=json.loads(db.execute('SELECT route_json FROM inbound WHERE message_id=?',(mid,)).fetchone()[0])
                    if fresh:r['new_conversation']=True
                    if legacy:r.pop('private_conversation_v1',None)
                    db.execute('UPDATE inbound SET route_json=? WHERE message_id=?',(json.dumps(r),mid))
            adapter.pump_once()
            tid=inbox.linked_source(mid)['task_id'];assert tid
            deadline=time.monotonic()+240
            while time.monotonic()<deadline:
                engine.tick();task=store.task(tid)
                if task['status'] in ('已完成','失败','已停止'):
                    assert task['status']=='已完成',task
                    adapter.pump_once()
                    return store.task(tid)
                time.sleep(1)
            raise RuntimeError('real_codex_turn_timeout')
        try:
            alpha='白鹭-'+secrets.token_hex(5);beta='青竹-'+secrets.token_hex(5)
            first=receive('om_old_first','第一轮的校验词是 '+alpha+'，记住它。只回复记住了。',legacy=True)
            record('first_fragment_completed')
            second=receive('om_old_second','第二轮的校验词是 '+beta+'，记住它。只回复记住了。',legacy=True,fresh=True)
            with inbox.db() as db:
                r=json.loads(db.execute('SELECT route_json FROM inbound WHERE message_id=?',('om_old_second',)).fetchone()[0]);r.pop('new_conversation',None)
                db.execute('UPDATE inbound SET route_json=? WHERE message_id=?',(json.dumps(r),'om_old_second'))
            assert first['thread_id']!=second['thread_id']
            record('second_independent_fragment_completed')
            repaired=receive('om_repair','第一轮我告诉你的校验词是什么？只回复校验词。')
            evidence['fragmented_history_recovered']=alpha in (repaired['result'] or '')
            evidence['repair_uses_previous_native_thread']=repaired['thread_id']==second['thread_id']
            evidence['repair_result']=repaired['result']
            assert evidence['fragmented_history_recovered']
            record('old_fragmented_history_recovered')
            follow=receive('om_follow','第二轮的校验词是什么？只回复校验词。')
            evidence['unquoted_followup_remembers']=beta in (follow['result'] or '')
            evidence['unquoted_followup_same_thread']=follow['thread_id']==repaired['thread_id']
            assert evidence['unquoted_followup_remembers'] and evidence['unquoted_followup_same_thread']
            record('native_unquoted_continuation_verified')
            reset=receive('om_reset','新对话')
            fresh=receive('om_after_reset','之前第一轮的校验词是什么？不知道就只回复不知道。')
            evidence['reset_has_new_thread']=reset['thread_id']!=follow['thread_id']
            evidence['reset_does_not_import_old_history']=alpha not in (fresh['result'] or '') and '不知道' in (fresh['result'] or '')
            evidence['completed_tasks_unchanged']=store.task(first['id'])==first and store.task(second['id'])==second
            evidence['synthetic_receipts']=6
            assert all(evidence[k] for k in ('reset_has_new_thread','reset_does_not_import_old_history','completed_tasks_unchanged'))
            record('passed')
        except Exception as exc:
            evidence['error']=str(exc)[:1000];record('failed');raise
        finally:
            transport.close();server.shutdown();server.server_close()


if __name__=='__main__':main()
