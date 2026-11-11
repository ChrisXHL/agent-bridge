#!/usr/bin/env python3
"""Isolated browser fixture: real bootstrap/configuration, simulated Feishu registration.

No real bots or messages. Stdin: finish, remove, fail, check, quit.
"""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json,threading,time
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from types import SimpleNamespace
from service import Store,make_handler
from feishu_groups import atomic_write

root=Path(__file__).resolve().parents[1]/'artifacts'/('manager-shortcut-'+time.strftime('%Y%m%d-%H%M%S'))
root.mkdir(parents=True,mode=0o700)
atomic_write(root/'projects.json',{});atomic_write(root/'feishu.json',{})
store=Store(root/'bridge.sqlite3',root/'projects.json',root/'no-codex-state')
server=ThreadingHTTPServer(('127.0.0.1',0),None)
server.RequestHandlerClass=make_handler(store,None,'http://127.0.0.1:'+str(server.server_port),feishu_settings_path=root/'feishu.json',feishu_inbox_path=root/'inbox.sqlite3')
onboard=server.RequestHandlerClass.onboarding
launches=[]
def simulate_registration(ident,recover=False):
    launches.append(ident)
    onboard.workers[ident]=SimpleNamespace(is_alive=lambda:True)
    atomic_write(onboard.directory(ident)/'registration.json',{'status':'waiting_scan','url':'https://open.feishu.cn/page/launcher?user_code=ISOLATED_TEST','expires_at':time.time()+3600})
    onboard.update(ident,phase='waiting_scan',message='隔离验收：模拟等待飞书扫码，未创建真实机器人。')
onboard.launch=simulate_registration
threading.Thread(target=server.serve_forever,daemon=True).start()
print(json.dumps({'url':'http://127.0.0.1:'+str(server.server_port)+'/panel','artifact':str(root)},ensure_ascii=False),flush=True)
try:
 for command in sys.stdin:
    command=command.strip()
    jobs=[json.loads(p.read_text()) for p in onboard.root.glob('asst_*/job.json')]
    if command=='quit':break
    if not jobs:print('No jobs yet',flush=True);continue
    ident=jobs[0]['id']
    if command=='finish':
        atomic_write(onboard.directory(ident)/'credentials.json',{'app_id':'cli_isolated_manager','app_secret':'ISOLATED_FAKE_SECRET','owner_open_id':'ou_isolated_owner','brand':'feishu'})
        onboard.update(ident,profile_saved=True)
        with patch('feishu_groups.probe',return_value={'bot_ready':True,'bot_open_id':'ou_isolated_bot','bot_name':'助手管理员'}):onboard.finish(ident)
    elif command=='remove':onboard.lifecycle(ident,'remove')
    elif command=='fail':onboard.update(ident,phase='failed',message='隔离验收：模拟配置中断，点击可继续原进度。')
    elif command=='check':
        j=onboard.read(ident);cfg=json.loads(onboard.groups.resolve(j['app_ref'])[1].read_text()) if j.get('app_ref') else {}
        result={'job_count':len(jobs),'launch_count':len(launches),'phase':j['phase'],'kind':j.get('kind'),'connection_count':len(store.connections()),'manager_owner_bound':cfg.get('assistant_manager',{}).get('admin_open_id')=='ou_isolated_owner','only_creator_allowed':cfg.get('allowed_open_ids')==['ou_isolated_owner'],'real_feishu_registration':False}
        atomic_write(root/'verification.json',result);print(json.dumps(result,ensure_ascii=False),flush=True);continue
    print(json.dumps({'phase':onboard.read(ident)['phase']},ensure_ascii=False),flush=True)
finally:server.shutdown();server.server_close()
