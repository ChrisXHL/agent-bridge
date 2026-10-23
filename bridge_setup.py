"""Agent-facing setup. Standard library only; works before pip/npm dependencies exist."""
from __future__ import annotations
import argparse
import contextlib
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import json
import os
import platform
import plistlib
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
VERSION = '2.6.2'
CLI_VERSION = '1.0.85'
DEFAULT_STATE = Path.home()/'Library/Application Support/CodexBridgeV2'


def local_env(root=ROOT):
    env = os.environ.copy()
    env['PATH'] = os.pathsep.join([str(root/'.tools/node_modules/.bin'), str(root/'.venv/bin'), env.get('PATH','')])
    return env


def get(port, path, data=None):
    req = Request(f'http://127.0.0.1:{port}'+path,
                  data=json.dumps(data).encode() if data is not None else None,
                  headers={'Content-Type':'application/json','Origin':f'http://127.0.0.1:{port}'})
    with urlopen(req, timeout=4) as response:return json.load(response)


def executable(name, root=ROOT):
    return shutil.which(name, path=local_env(root)['PATH'])


def probe(argv, root=ROOT, timeout=5):
    try:
        p = subprocess.run(argv, env=local_env(root), capture_output=True, timeout=timeout)
        return p.returncode == 0
    except (OSError, subprocess.TimeoutExpired):return False


def runtime(port, root=ROOT, state=DEFAULT_STATE):
    try:
        value=get(port,'/api/bridge-instance')
        if value.get('product')!='codex-bridge':return {'state':'port_conflict'}
        same = value.get('source_root')==str(root.resolve()) and value.get('state_dir')==str(state.resolve())
        return {'state':'running' if same else 'other_installation','version':value.get('version'),
                'panel_url':f'http://127.0.0.1:{port}/panel'}
    except (OSError, ValueError, HTTPError):
        with socket.socket() as s:
            s.settimeout(.3)
            occupied=s.connect_ex(('127.0.0.1',port))==0
        return {'state':'port_conflict' if occupied else 'stopped'}


def doctor(root=ROOT, state=DEFAULT_STATE, port=8797):
    env=local_env(root); python=root/'.venv/bin/python'
    node=executable('node',root); npm=executable('npm',root); cli=executable('lark-cli',root)
    from codex_runtime import resolve_codex_binary
    codex=resolve_codex_binary(os.getenv('CODEX_BIN'))
    checks={'platform':platform.system()=='Darwin', 'python':(3,11)<=sys.version_info[:2]<(3,14),
            'python_dependencies':python.is_file() and probe([str(python),'-c','import lark_oapi'],root,timeout=30),
            'node':bool(node) and probe([node,'--version'],root),
            'npm':bool(npm) and probe([npm,'--version'],root),
            'node_dependencies':False,
            'lark_cli':bool(cli) and probe([cli,'--version'],root),
            'codex':probe([codex,'--version'],root), 'codex_login':probe([codex,'login','status'],root)}
    # Resolve modules from the copied package, never from this caller's cwd.
    checks['node_dependencies']=bool(node) and probe([node,'-e',f"require({json.dumps(str(root/'node_modules/@larksuiteoapi/node-sdk'))});require({json.dumps(str(root/'node_modules/qrcode'))})"],root)
    server=runtime(port,root,state)
    missing=[k for k,v in checks.items() if not v]
    stage='needs_agent';action='prepare_then_install'
    if not checks['platform']:stage='unsupported_platform';action='use_supported_macos_host'
    elif not checks['python']:action='agent_install_supported_python'
    elif not checks['node'] or not checks['npm']:action='agent_install_node_from_official_source'
    elif not checks['codex']:action='agent_install_codex_from_official_source'
    elif any(not checks[k] for k in ('python_dependencies','node_dependencies','lark_cli')):action='prepare'
    elif not checks['codex_login']:stage='needs_login';action='user_complete_codex_login'
    elif server['state']=='running':stage='ready';action='create_or_resume_manager'
    else:action='install'
    if server['state'] in ('other_installation','port_conflict'):stage='port_conflict';action='inspect_existing_service'
    return {'schema_version':1,'stage':stage,'checks':checks,'missing':missing,'runtime':server,
            'next_action':action,
            'user_action_required':stage=='needs_login','execution_backend':'Codex',
            'acceptance':{'feishu_receipt':False,'codex_execution':False,'feishu_reply':False}}


def save(path,value):
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(value,ensure_ascii=False));temp.chmod(0o600);temp.replace(path)


@contextlib.contextmanager
def lock(state):
    state.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (state/'setup.lock').open('a') as f:
        os.chmod(f.name,0o600)
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield


def run_logged(argv,state,root=ROOT,timeout=600):
    with (state/'setup.log').open('ab') as log:
        os.chmod(log.name,0o600)
        p=subprocess.run(argv,cwd=root,env=local_env(root),stdout=log,stderr=log,timeout=timeout)
    if p.returncode:raise RuntimeError('command_failed')


def prepare(root=ROOT,state=DEFAULT_STATE,port=8797):
    if platform.system()!='Darwin':return doctor(root,state,port)
    with lock(state):
        check=doctor(root,state,port)
        if not check['checks']['platform'] or not check['checks']['python']:
            return {**check,'next_action':'agent_install_supported_python_on_macos'}
        if not check['checks']['node'] or not check['checks']['npm']:
            return {**check,'next_action':'agent_install_node_from_official_source'}
        marker=state/'setup.json'
        try:
            if not check['checks']['python_dependencies']:
                save(marker,{'stage':'python_dependencies'})
                if not (root/'.venv/bin/python').exists():run_logged([sys.executable,'-m','venv',str(root/'.venv')],state,root)
                run_logged([str(root/'.venv/bin/python'),'-m','pip','install','-r',str(root/'requirements.txt')],state,root)
            if not check['checks']['node_dependencies']:
                save(marker,{'stage':'node_dependencies'})
                run_logged([executable('npm',root),'ci','--ignore-scripts','--no-audit','--no-fund'],state,root)
            if not check['checks']['lark_cli']:
                save(marker,{'stage':'lark_cli'})
                run_logged([executable('npm',root),'install','--prefix',str(root/'.tools'),'--no-audit','--no-fund','--save-exact','@larksuite/cli@'+CLI_VERSION],state,root)
        except (OSError,RuntimeError,subprocess.TimeoutExpired):
            value={'stage':'needs_agent','next_action':'inspect_private_setup_log_then_retry_same_command',
                   'log_path':str(state/'setup.log'),'user_action_required':False}
            save(marker,value);return value
        result=doctor(root,state,port);save(marker,{'stage':'prepared','source_root':str(root)})
        return result


def install(root=ROOT,state=DEFAULT_STATE,port=8797):
    if platform.system()!='Darwin':return doctor(root,state,port)
    with lock(state):
        check=doctor(root,state,port)
        if check['runtime']['state']=='running':return check
        if check['runtime']['state']!='stopped':return check
        if check['missing']:return check
        # Never replace an existing launch agent belonging to another copy.
        label='dev.codex.bridge.v2' if state.resolve()==DEFAULT_STATE.resolve() and port==8797 else 'dev.codex.bridge.'+hashlib.sha256((str(state.resolve())+str(port)).encode()).hexdigest()[:10]
        plist=Path.home()/'Library/LaunchAgents'/(label+'.plist')
        if plist.exists():
            old=plistlib.loads(plist.read_bytes())
            if old.get('WorkingDirectory')!=str(root.resolve()):
                return {'stage':'existing_installation','next_action':'inspect_existing_launch_agent_do_not_overwrite','user_action_required':False}
        projects=root/'projects.json' if (root/'projects.json').exists() else state/'projects.json'
        if not projects.exists():save(projects,{})
        # A fresh user gets open collaboration; existing settings are never changed.
        policy=state/'assistant-collaboration/policy.json'
        existing_state=(state/'bridge.sqlite3').exists() or (state/'feishu.json').exists()
        if not policy.exists() and not existing_state:save(policy,{'mode':'open'})
        env=local_env(root)
        from codex_runtime import resolve_codex_binary
        codex=resolve_codex_binary(os.getenv('CODEX_BIN'))
        config={'Label':label,'WorkingDirectory':str(root.resolve()),'RunAtLoad':True,'KeepAlive':True,
                'ProgramArguments':[str(root/'.venv/bin/python'),str(root/'service.py'),'--db',str(state/'bridge.sqlite3'),'--projects',str(projects),'--port',str(port)],
                'StandardOutPath':str(state/'service.log'),'StandardErrorPath':str(state/'service-error.log'),
                'EnvironmentVariables':{'PATH':env['PATH'],'CODEX_BIN':codex,'FEISHU_CONFIG':str(state/'feishu.json'),'BRIDGE_REQUIRE_APP':'0','PYTHONUNBUFFERED':'1'}}
        if os.getenv('BRIDGE_PROJECT_DISCOVERY')=='0':config['EnvironmentVariables']['BRIDGE_PROJECT_DISCOVERY']='0'
        for name in ('BRIDGE_BACKEND','BRIDGE_WORKSPACE_ROOT','CLAUDE_BIN','CLAUDE_MODEL','CLAUDE_PERMISSION_MODE'):
            if os.getenv(name):config['EnvironmentVariables'][name]=os.environ[name]
        plist.parent.mkdir(parents=True,exist_ok=True);plist.write_bytes(plistlib.dumps(config));plist.chmod(0o600)
        run_logged(['launchctl','bootout',f'gui/{os.getuid()}/{label}'],state,root,timeout=10) if probe(['launchctl','print',f'gui/{os.getuid()}/{label}'],root) else None
        run_logged(['launchctl','bootstrap',f'gui/{os.getuid()}',str(plist)],state,root,timeout=15)
        for _ in range(100):
            if runtime(port,root,state)['state']=='running':return doctor(root,state,port)
            time.sleep(.3)
        return {'stage':'needs_agent','next_action':'inspect_private_service_log','log_path':str(state/'service-error.log'),'user_action_required':False}


def manager(port=8797,state=DEFAULT_STATE):
    instance=get(port,'/api/bridge-instance')
    if (instance.get('product')!='codex-bridge' or instance.get('source_root')!=str(ROOT.resolve())
        or instance.get('state_dir')!=str(state.resolve())):
        return {'stage':'other_installation','next_action':'inspect_existing_service','user_action_required':False}
    result=get(port,'/api/feishu/manager',{})
    action={'waiting_scan':'user_scan_official_qr','ready':'open_existing_manager_in_feishu',
            'removed':'restore_existing_manager','failed':'inspect_saved_job_then_resume',
            'needs_check':'verify_existing_feishu_application'}.get(result.get('phase'),'poll_existing_job')
    return {'stage':result.get('phase'),'assistant':result,'next_action':action,
            'user_action_required':result.get('phase')=='waiting_scan'}


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['doctor','prepare','install','manager']);p.add_argument('--state-dir',type=Path,default=DEFAULT_STATE);p.add_argument('--port',type=int,default=int(os.getenv('BRIDGE_PORT','8797')))
    args=p.parse_args()
    try:
        if args.action=='manager':result=manager(args.port,args.state_dir.resolve())
        else:result={'doctor':doctor,'prepare':prepare,'install':install}[args.action](ROOT,args.state_dir.resolve(),args.port)
    except BlockingIOError:result={'stage':'busy','next_action':'wait_for_existing_setup_then_doctor','user_action_required':False}
    except Exception as e:result={'stage':'needs_agent','error_type':type(e).__name__,'next_action':'inspect_local_state_do_not_recreate_bot','user_action_required':False}
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
