#!/usr/bin/env python3
"""Bridge 助手统一管理工具（任务管家 / 维护助手使用）。

只走本机 Bridge 的正式接口（127.0.0.1:8797），写操作前自动备份到
~/Library/Application Support/CodexBridgeV2/admin-backups/，不输出任何密钥。

用法：
  bridge-admin.py list                         列出全部助手（名称、引擎、启用、运行状态、群数）
  bridge-admin.py groups                       列出已接入的群及其助手
  bridge-admin.py show 助手名或ref              查看某助手的完整规则与设置
  bridge-admin.py set-prompt 助手名 --file F    用文件内容替换该助手的规则（必须恰好含一次 {{message}}）
  bridge-admin.py set-backend 助手名 claude|codex
  bridge-admin.py enable 助手名 / disable 助手名
  bridge-admin.py create --requirements "用途" [--group]   新建助手，打印二维码图片路径
  bridge-admin.py job 助手ID                   查看新建任务进度
  bridge-admin.py health                       服务、守护进程、各机器人连接、近期失败任务
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE = os.environ.get('BRIDGE_URL', 'http://127.0.0.1:8797')
STATE = Path.home() / 'Library/Application Support/CodexBridgeV2'
BACKUPS = STATE / 'admin-backups'
SECRET_KEYS = ('secret', 'token', 'key', 'password')


def api(path, data=None, method=None):
    body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
    req = urllib.request.Request(BASE + path, data=body, method=method or ('POST' if body is not None else 'GET'),
                                 headers={'Content-Type': 'application/json', 'Host': '127.0.0.1:8797'})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode() or '{}')
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors='replace')
        sys.exit(f'接口返回 {e.code}: {detail[:500]}')


def redact(o):
    if isinstance(o, dict):
        return {k: ('***' if any(s in k.lower() for s in SECRET_KEYS) and k != 'has_secret' else redact(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [redact(x) for x in o]
    return o


def catalog():
    return api('/api/feishu/catalog')


def find(apps, key):
    exact = [a for a in apps if key in (a.get('ref'), a.get('name'), a.get('bot_name'), a.get('app_id'))]
    if len(exact) == 1:
        return exact[0]
    if not exact:
        fuzzy = [a for a in apps if key in (a.get('name') or '') or key in (a.get('bot_name') or '')]
        if len(fuzzy) == 1:
            return fuzzy[0]
        exact = fuzzy
    names = '、'.join(f"{a.get('name')}({a.get('ref')})" for a in exact) or '无'
    sys.exit(f'找不到唯一的助手“{key}”，候选：{names}。请改用 ref。')


def backup(app):
    BACKUPS.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUPS, 0o700)
    src = STATE / 'feishu-apps' / app['ref'] / 'config.json'
    dst = BACKUPS / f"{time.strftime('%Y%m%d-%H%M%S')}-{app['ref']}-config.json"
    if src.exists():
        dst.write_bytes(src.read_bytes())
        os.chmod(dst, 0o600)
    meta = BACKUPS / (dst.stem + '.meta.json')
    meta.write_text(json.dumps({'name': app.get('name'), 'execution_backend': app.get('execution_backend'),
                                'enabled': app.get('enabled')}, ensure_ascii=False))
    print('已备份:', dst)
    return dst


def cmd_list(_):
    for a in catalog()['apps']:
        groups = len(a.get('allowed_chat_ids') or [])
        print(f"{a.get('name')}\tref={a.get('ref')}\t引擎={a.get('execution_backend')}\t启用={a.get('enabled')}"
              f"\t运行={a.get('runtime')}\t群={groups}\t规则版本={a.get('prompt_policy_version')}")


def cmd_groups(_):
    c = catalog()
    names = {a['ref']: a.get('name') for a in c['apps']}
    for g in c.get('groups') or []:
        g = redact(g)
        keep = {k: g.get(k) for k in ('chat_id', 'name', 'title', 'app_ref', 'enabled') if k in g}
        print(json.dumps(keep, ensure_ascii=False), names.get(g.get('app_ref'), ''))


def cmd_show(a):
    print(json.dumps(redact(find(catalog()['apps'], a.name)), ensure_ascii=False, indent=1))


def cmd_set_prompt(a):
    app = find(catalog()['apps'], a.name)
    text = Path(a.file).read_text()
    if text.count('{{message}}') != 1:
        sys.exit('规则必须恰好包含一次 {{message}}')
    backup(app)
    api('/api/feishu/apps', {'ref': app['ref'], 'revision': app['revision'], 'prompt_template': text})
    after = find(catalog()['apps'], app['ref'])
    print('规则已更新并读回一致' if after.get('prompt_template') == text else '警告：读回内容与提交不一致，请检查')


def cmd_set_backend(a):
    app = find(catalog()['apps'], a.name)
    backup(app)
    api('/api/feishu/apps/backend', {'app_ref': app['ref'], 'revision': app['revision'], 'execution_backend': a.backend})
    print('当前引擎:', find(catalog()['apps'], app['ref']).get('execution_backend'))


def cmd_enabled(a, value):
    app = find(catalog()['apps'], a.name)
    backup(app)
    api('/api/feishu/apps/enabled', {'app_ref': app['ref'], 'revision': app['revision'], 'enabled': value})
    print('当前启用:', find(catalog()['apps'], app['ref']).get('enabled'))


def cmd_create(a):
    job = api('/api/feishu/onboarding', {'request_id': str(uuid.uuid4()), 'requirements': a.requirements,
                                          'audience': 'group' if a.group else 'private'})
    ident = job['id']
    print('新建任务:', ident)
    j = job
    for _ in range(120):
        j = api('/api/feishu/onboarding/' + ident)
        if j['phase'] in ('waiting_scan', 'ready', 'failed', 'needs_check', 'waiting_group'):
            break
        time.sleep(3)
    print('阶段:', j['phase'], j.get('message'))
    qr = STATE / 'assistant-onboarding' / ident / 'qr.png'
    if qr.exists():
        print('二维码图片:', qr, '（复制到当前目录后用 im +messages-reply --image 发给用户扫码）')
    if j.get('verification_url'):
        print('扫码链接:', j['verification_url'])


def cmd_job(a):
    print(json.dumps(redact(api('/api/feishu/onboarding/' + a.ident)), ensure_ascii=False, indent=1))


def cmd_health(_):
    print('服务:', api('/healthz'))
    out = subprocess.run(['launchctl', 'list'], capture_output=True, text=True).stdout
    print('守护进程:', [l for l in out.splitlines() if 'codex.bridge' in l] or '未找到 dev.codex.bridge.v2')
    bad = [a for a in catalog()['apps'] if a.get('enabled') and a.get('runtime') != '已连接飞书']
    print('机器人连接全部正常' if not bad else '连接异常: ' + '、'.join(f"{a.get('name')}({a.get('runtime')})" for a in bad))
    db = sqlite3.connect(f"file:{STATE/'bridge.sqlite3'}?mode=ro", uri=True)
    rows = db.execute("""SELECT c.source, t.status, COALESCE(t.error_code,''), datetime(t.updated_at,'unixepoch','localtime')
                         FROM tasks t JOIN connections c ON c.id=t.connection_id
                         WHERE t.updated_at > strftime('%s','now') - 86400
                           AND (t.error_code IS NOT NULL OR t.status NOT IN ('已完成'))
                         ORDER BY t.updated_at DESC LIMIT 20""").fetchall()
    print('近 24 小时未完成/出错任务:' if rows else '近 24 小时没有出错任务')
    for r in rows:
        print('  ', *r)


def main():
    p = argparse.ArgumentParser(description='Bridge 助手统一管理')
    s = p.add_subparsers(dest='cmd', required=True)
    s.add_parser('list'); s.add_parser('groups'); s.add_parser('health')
    x = s.add_parser('show'); x.add_argument('name')
    x = s.add_parser('set-prompt'); x.add_argument('name'); x.add_argument('--file', required=True)
    x = s.add_parser('set-backend'); x.add_argument('name'); x.add_argument('backend', choices=['codex', 'claude'])
    x = s.add_parser('enable'); x.add_argument('name')
    x = s.add_parser('disable'); x.add_argument('name')
    x = s.add_parser('create'); x.add_argument('--requirements', required=True); x.add_argument('--group', action='store_true')
    x = s.add_parser('job'); x.add_argument('ident')
    a = p.parse_args()
    {'list': cmd_list, 'groups': cmd_groups, 'show': cmd_show, 'set-prompt': cmd_set_prompt,
     'set-backend': cmd_set_backend, 'enable': lambda a: cmd_enabled(a, True),
     'disable': lambda a: cmd_enabled(a, False), 'create': cmd_create, 'job': cmd_job,
     'health': cmd_health}[a.cmd](a)


if __name__ == '__main__':
    main()
