#!/usr/bin/env python3
"""Allowlisted, inspected source delivery. Never package the working directory wholesale."""
import argparse
import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
DOCUMENTS=['AGENTS.md','README.md','.env.example','.gitignore','requirements.txt','package.json','package-lock.json','run.sh','.mcp.json','.codex-plugin/plugin.json']
SCRIPTS=['assistant-call.py','bridge-setup','launch-mcp','register-assistant.cjs','install-daemon','uninstall-daemon','build-share.py']


def build(output,root=ROOT):
    files=[root/p for p in DOCUMENTS]+[root/'scripts'/p for p in SCRIPTS]
    files += [p for p in root.glob('*.py') if not p.name.startswith('test_')]
    for folder in ('static','skills'):
        files += [p for p in (root/folder).rglob('*') if p.is_file() and p.suffix in ('.html','.js','.css','.md','.yaml','.yml')]
    files=sorted(set(files))
    for p in files:
        if p.is_symlink() or not p.is_file():raise ValueError('missing_or_symlinked_source: '+str(p.relative_to(root)))
        body=p.read_text()
        if re.search(r'(?:cli|ou|oc)_[0-9a-f]{12,}|/Users/(?!example\b|USERNAME\b)[A-Za-z0-9_. -]+/|\bsk-[A-Za-z0-9_-]{24,}',body):
            raise ValueError('private_identifier_in_source: '+str(p.relative_to(root)))
    version=json.loads((root/'.codex-plugin/plugin.json').read_text())['version']
    prefix='codex-bridge-'+version
    manifest={'version':version,'files':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files},'runtime_state_included':False}
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
        for p in files:
            info=zipfile.ZipInfo(prefix+'/'+str(p.relative_to(root)))
            info.external_attr=((stat.S_IFREG | (0o755 if p.stat().st_mode&0o111 else 0o644))<<16)
            info.compress_type=zipfile.ZIP_DEFLATED;z.writestr(info,p.read_bytes())
        z.writestr(prefix+'/PACKAGE-MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    with zipfile.ZipFile(output) as z:assert z.testzip() is None
    return {'path':str(output.resolve()),'version':version,'file_count':len(files),'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'bytes':output.stat().st_size,'runtime_state_included':False}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=ROOT/'artifacts/codex-bridge-2.6.2-agent.zip');a=p.parse_args()
    print(json.dumps(build(a.output),ensure_ascii=False))
