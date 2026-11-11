#!/usr/bin/env python3
"""Explicit live Codex acceptance; synthetic Feishu intake, no real bot or delivery."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json,hashlib,threading,time,uuid
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from service import Store,Engine,LocalCodexTransport,make_handler
from assistant_onboarding import AssistantOnboarding
from feishu_groups import GroupSettings,atomic_write
from feishu_worker import Inbox

root=Path(__file__).resolve().parents[1]/'artifacts'/('collaboration-live-'+time.strftime('%Y%m%d-%H%M%S'))
root.mkdir(parents=True,mode=0o700)
projects={}
for key in ('main','math','sort'):
 folder=root/key;folder.mkdir();projects[key]=str(folder)
atomic_write(root/'projects.json',projects)
store=Store(root/'bridge.sqlite3',root/'projects.json',root/'no-codex-state')
atomic_write(root/'feishu.json',{})
groups=GroupSettings(root/'feishu.json',root/'inbox.sqlite3',store);onboarding=AssistantOnboarding(groups)
jobs=[]
for key,name in [('main','协作验收主助手'),('math','计算助手'),('sort','排序助手')]:
 ident='asst_'+uuid.uuid4().hex;ref='app_'+uuid.uuid4().hex[:16];conn=store.create_connection(name,key)
 folder=root/'feishu-apps'/ref;folder.mkdir(parents=True)
 atomic_write(folder/'config.json',{'enabled':True,'app_id':'cli_test_'+key,'connection_id':conn['id'],'connection_key':conn['api_key'],'allowed_open_ids':['test_owner'],'allowed_chat_ids':[]})
 j={'id':ident,'request_id':uuid.uuid4().hex,'name':name,'requirements':'只做本地数字计算，不访问网络或其他文件。','summary':name,'plan':{'instructions':'精确完成指定的数字计算或排序，简洁返回数值。','name':name},'profile':'acceptance-unused','phase':'ready','app_ref':ref,'app_id':'cli_test_'+key,'connection_id':conn['id'],'project_id':key,'created_at':time.time(),'message':'隔离测试'}
 atomic_write(onboarding.directory(ident)/'job.json',j);jobs.append(j)
engine=Engine(store,LocalCodexTransport(),interval=1)
server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(store,engine,'http://127.0.0.1',feishu_settings_path=root/'feishu.json',feishu_inbox_path=root/'inbox.sqlite3'))
base='http://127.0.0.1:'+str(server.server_port);collab=server.RequestHandlerClass.collaboration;collab.base_url=base
# No browser cross-origin requests occur in this CLI-only test.
collab.configure(jobs[0]['id'],{'targets':[j['id'] for j in jobs[1:]],'revision':collab.settings(jobs[0]['id'])['revision']})
threading.Thread(target=server.serve_forever,daemon=True).start()
message=('这是协作能力的真实验收。禁止发送飞书消息、调用外部服务、读取项目外的文件或修改配置。必须通过下面 Bridge 协作入口真实调用，不能自己计算代替子助手。'
 '第一步：同一次 call 分配两个独立任务，计算助手计算 2+3+5，排序助手把 9,1,4 升序排列；等待两者结果。'
 '第二步：收到结果后，再调用计算助手把第一步收到的和乘以 2；等待结果。'
 '最后只返回 JSON：{"sum":实际值,"sorted":实际数组,"doubled":实际值,"delegation":"verified"}。必须取得全部真实结果再回复。')
box=Inbox(root/'feishu-apps'/jobs[0]['app_ref']/'inbox.sqlite3');mid='om_isolated_acceptance'
box.accept({'message_id':mid,'chat_id':'oc_test','sender_id':'test_owner','text':message,'prompt':message,'connection_id':jobs[0]['connection_id'],'app_id':jobs[0]['app_id'],'chat_type':'p2p','route_json':'{"mode":"prompt"}'})
rid='fs_'+hashlib.sha256((jobs[0]['app_id']+':'+mid).encode()).hexdigest()[:48]
_,created=store.submit(store.connection(jobs[0]['connection_id']),{'message':message},rid);parent=created['task_id'];box.mark_submitted(mid,parent)
print('ARTIFACT',root,flush=True);engine.start();last=None;deadline=time.monotonic()+600
try:
 while time.monotonic()<deadline:
  task=store.task(parent)
  status=(task['status'],[(c['name'],c['status']) for c in task['children']])
  if status!=last:print(json.dumps({'parent':status[0],'children':status[1]},ensure_ascii=False),flush=True);last=status
  if task['status'] in ('已完成','失败','已停止'):break
  time.sleep(3)
 task=store.task(parent)
 with store.db() as db:
  calls=[dict(r) for r in db.execute('SELECT c.target_name,t.status,t.result,c.created_at FROM assistant_calls c JOIN tasks t ON t.id=c.child_task WHERE c.parent_task=? ORDER BY c.created_at',(parent,))]
 result={'parent_status':task['status'],'parent_result':task['result'],'calls':calls,'synthetic_feishu_intake':True,'real_codex_execution':True,'feishu_delivery_tested':False}
 atomic_write(root/'verification.json',result)
 print(json.dumps(result,ensure_ascii=False),flush=True)
 if task['status']!='已完成' or len(calls)!=3 or any(c['status']!='已完成' for c in calls):raise SystemExit(1)
 decoded=json.loads(task['result'].strip().removeprefix('```json').removesuffix('```').strip())
 if decoded!={'sum':10,'sorted':[1,4,9],'doubled':20,'delegation':'verified'}:raise SystemExit(2)
finally:
 engine.close();server.shutdown();server.server_close()
