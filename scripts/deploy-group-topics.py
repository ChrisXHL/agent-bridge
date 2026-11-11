#!/usr/bin/env python3
"""One deployment in this task, only after existing work is idle; no task replay."""
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

project=Path(__file__).resolve().parents[1]
runtime=Path.home()/'Library/Application Support/CodexBridgeV2'
evidence=project/'artifacts/group-topics-deployment.json'
historical={'task_e71b705c57ef4b7d9848','task_19441aa06fe04d56983c'}
files=['group_topics.py','feishu_worker.py','feishu_media.py','feishu_groups.py','feishu_probe.py','assistant_collaboration.py','assistant_authorizations.py','assistant_consent.py','assistant_onboarding.py','bridge_setup.py','mcp_server.py','service.py','scripts/assistant-call.py','scripts/bridge-setup','static/feishu-groups.js','static/assistants.js','static/index.html']
def digest(value):return hashlib.sha256(value).hexdigest()
def get(path):
    with urlopen('http://127.0.0.1:8797'+path,timeout=8) as response:return json.load(response)
def save(value):evidence.write_text(json.dumps(value,ensure_ascii=False,indent=2))
expected={f:digest((project/f).read_bytes()) for f in files}
def blockers():
    value=get('/api/tasks');rows=value if isinstance(value,list) else value.get('tasks',[])
    active=[r['id'] for r in rows if r['id'] not in historical and r['status'] in ('执行中','等待 Codex','需要你处理')]
    jobs=[j['id'] for j in get('/api/feishu/onboarding').get('assistants',[]) if j['phase'] not in ('ready','failed','needs_check','removed')]
    requests=[]
    for p in (runtime/'assistant-management').glob('**/*.json'):
        try:
            if json.loads(p.read_text()).get('status')=='pending':requests.append(p.stem)
        except (ValueError,OSError):pass
    return {'active_tasks':active,'active_creation':jobs,'pending_management':requests}
def snapshot():
    configs={str(p.relative_to(runtime)):digest(p.read_bytes()) for p in [runtime/'feishu.json',*sorted((runtime/'feishu-apps').glob('app_*/config.json'))]}
    history={i:digest(json.dumps(get('/api/tasks/'+i),sort_keys=True,ensure_ascii=False).encode()) for i in historical}
    return {'configs':configs,'history':history}

print('Waiting for an idle deployment window.',flush=True)
deadline=time.monotonic()+1800
while True:
    blocked=blockers()
    if not any(blocked.values()):break
    save({'deployed':False,'state':'waiting_for_existing_work','blockers':blocked,'code_hashes':expected})
    if time.monotonic()>=deadline:raise SystemExit('No safe idle window; existing work was not interrupted.')
    time.sleep(5)
assert expected=={f:digest((project/f).read_bytes()) for f in files},'Code changed after verification; deployment cancelled.'
before=snapshot()
backup=runtime/'backups'/('group-topics-'+time.strftime('%Y%m%d-%H%M%S'));backup.mkdir(mode=0o700,parents=True)
with sqlite3.connect('file:'+str(runtime/'bridge.sqlite3')+'?mode=ro',uri=True) as source:
    with sqlite3.connect(backup/'bridge.sqlite3') as target:source.backup(target)
(backup/'bridge.sqlite3').chmod(0o600)
(backup/'baseline.json').write_text(json.dumps(before));(backup/'baseline.json').chmod(0o600)
assert not any(blockers().values()),'New work arrived; deployment cancelled before restart.'
subprocess.run(['launchctl','kickstart','-k','gui/'+str(os.getuid())+'/dev.codex.bridge.v2'],check=True)
for _ in range(30):
    try:
        if get('/healthz').get('status')=='ok':break
    except OSError:pass
    time.sleep(1)
else:raise SystemExit('Service did not become healthy; backup preserved at '+str(backup))
after=snapshot()
with sqlite3.connect('file:'+str(runtime/'bridge.sqlite3')+'?mode=ro',uri=True) as db:
    schema={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
result={'deployed':True,'health':get('/healthz'),'existing_configs_unchanged':before['configs']==after['configs'],
        'historical_records_unchanged':before['history']==after['history'],'topic_tables_ready':{'group_topics','group_topic_aliases','group_discussions','group_questions'}<=schema,
        'backup':str(backup),'code_hashes':expected,'old_group_permissions_expanded':False,'real_feishu_group_end_to_end_verified':False}
save(result);assert result['existing_configs_unchanged'] and result['historical_records_unchanged'] and result['topic_tables_ready']
print(json.dumps({k:v for k,v in result.items() if k not in ('code_hashes','backup')},ensure_ascii=False),flush=True)
