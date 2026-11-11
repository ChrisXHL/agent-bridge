#!/usr/bin/env python3
"""Exercise a clean exported package on an isolated port/state; never create Feishu apps."""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from codex_runtime import resolve_codex_binary


def main():
    archive=ROOT/'artifacts/codex-bridge-2.6.2-agent.zip'
    evidence={'stage':'started','new_feishu_account_tested':False,'fresh_os_tested':False,'codex_login_reused':True,'private_state_copied':False}
    target=ROOT/'artifacts/agent-package-clean-install.json'
    def record():target.write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    with tempfile.TemporaryDirectory(prefix='bridge-clean-install-') as temporary:
        test=Path(temporary);tools=test/'tools';tools.mkdir();state=test/'state'
        with zipfile.ZipFile(archive) as z:
            z.extractall(test)
            for info in z.infolist():
                if not info.is_dir():(test/info.filename).chmod((info.external_attr>>16)&0o777 or 0o644)
        package=next(test.glob('codex-bridge-*'))
        for name in ('node','npm'):
            binary=shutil.which(name)
            if not binary:raise RuntimeError(name+'_missing')
            (tools/name).symlink_to(binary)
        env=os.environ.copy();env['PATH']=str(tools)+':/usr/bin:/bin:/usr/sbin:/sbin'
        env['CODEX_BIN']=resolve_codex_binary();env['BRIDGE_PROJECT_DISCOVERY']='0'
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        label='dev.codex.bridge.'+hashlib.sha256((str(state.resolve())+str(port)).encode()).hexdigest()[:10]
        plist=Path.home()/'Library/LaunchAgents'/(label+'.plist')
        def command(action,timeout=900):
            out=subprocess.run([sys.executable,str(package/'bridge_setup.py'),action,'--state-dir',str(state),'--port',str(port)],env=env,cwd=package,capture_output=True,text=True,timeout=timeout)
            if out.returncode:raise RuntimeError('setup_process_failed')
            return json.loads(out.stdout)
        try:
            initial=command('doctor');assert not initial['checks']['python_dependencies'] and not initial['checks']['node_dependencies'] and not initial['checks']['lark_cli']
            mcp_env={**env,'BRIDGE_PORT':str(port)}
            request=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'bridge_environment_status','arguments':{}}})+'\n'
            mcp=subprocess.run([sys.executable,str(package/'mcp_server.py')],input=request,env=mcp_env,cwd=package,capture_output=True,text=True,timeout=60)
            response=json.loads(mcp.stdout)['result']['structuredContent'];assert response['next_action']=='prepare'
            evidence['mcp_diagnoses_before_dependencies']=True
            evidence.update(stage='preparing',initial_missing=initial['missing']);record()
            prepared=command('prepare');evidence['prepared_missing']=prepared.get('missing');record()
            if 'python_dependencies' in (prepared.get('missing') or []):
                diagnostic=subprocess.run([str(package/'.venv/bin/python'),'-c','import lark_oapi'],env=env,cwd=package,capture_output=True,text=True,timeout=90)
                (ROOT/'artifacts/agent-package-private-import.log').write_text(diagnostic.stderr)
                evidence['python_import_exit']=diagnostic.returncode;record()
            assert not prepared.get('missing'),prepared
            evidence['stage']='installing';record()
            installed=command('install');assert installed['stage']=='ready',installed
            again=command('install');assert again['stage']=='ready'
            def read(path):return json.load(urlopen(f'http://127.0.0.1:{port}'+path,timeout=5))
            assert read('/api/bridge-instance')['source_root']==str(package.resolve())
            assert read('/api/feishu/collaboration-policy')=={'mode':'open'}
            assert read('/api/tasks')==[]
            assert read('/api/projects')==[]
            assert read('/api/feishu/onboarding')['assistants']==[]
            # A package pointed at another installation must refuse mutation.
            external=subprocess.run([sys.executable,str(ROOT/'bridge_setup.py'),'install','--state-dir',str(state),'--port',str(port)],capture_output=True,text=True,timeout=60)
            conflict=json.loads(external.stdout);assert conflict['runtime']['state']=='other_installation'
            evidence.update(stage='passed',python_dependencies_installed=True,node_dependencies_installed=True,local_feishu_cli_installed=(package/'.tools/node_modules/.bin/lark-cli').exists(),
                            service_identity_verified=True,repeated_install_reused_service=True,default_free_collaboration=True,other_installation_not_overwritten=True,
                            imported_tasks=0,imported_assistants=0,imported_projects=0,first_feishu_message_verified=False)
            record()
        except Exception as e:
            evidence.update(stage='failed',error_type=type(e).__name__)
            if (state/'setup.log').exists():
                # Private local evidence; never include these logs in a share ZIP.
                shutil.copyfile(state/'setup.log',ROOT/'artifacts/agent-package-private-setup.log')
            record();raise
        finally:
            if plist.exists():
                subprocess.run(['launchctl','bootout',f'gui/{os.getuid()}/{label}'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                plist.unlink()
    evidence['test_service_cleaned_up']=True;record();print(json.dumps(evidence,ensure_ascii=False))


if __name__=='__main__':main()
