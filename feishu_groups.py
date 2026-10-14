"""Per-application credentials and per-group prompt routes; legacy config stays in place."""
from __future__ import annotations
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path

from codex_http_bridge import BridgeError
from feishu_settings import CONFIG_LOCK, DEFAULT_PROMPT_TEMPLATE, load, public, save, start_pairing, validate_prompt_template
from feishu_probe import probe
from feishu_worker import Inbox


def application_paths(default_path, inbox_path):
    default_path, inbox_path = Path(default_path), Path(inbox_path)
    yield 'default', default_path, inbox_path
    directory = default_path.parent / 'feishu-apps'
    for path in sorted(directory.glob('app_*/config.json')):
        if re.fullmatch(r'app_[a-f0-9]{16}', path.parent.name):
            yield path.parent.name, path, path.parent / 'inbox.sqlite3'


def group_routes(value):
    """Unmigrated groups resolve exactly as before, without rewriting private files."""
    overrides = value.get('group_configs', {})
    return [dict({'chat_id': chat, 'name': '已接入群 · ' + chat[-6:],
                  'connection_id': value.get('connection_id', ''),
                  'prompt_template': value.get('prompt_template', DEFAULT_PROMPT_TEMPLATE),
                  'enabled': True}, **overrides.get(chat, {}))
            for chat in value.get('allowed_chat_ids', [])]


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def atomic_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.feishu-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


class GroupSettings:
    def __init__(self, config_path, inbox_path, store, supervisor=None):
        self.config_path, self.inbox_path = Path(config_path), Path(inbox_path)
        self.store, self.supervisor = store, supervisor

    def resolve(self, ref):
        for item in application_paths(self.config_path, self.inbox_path):
            if item[0] == ref: return item
        raise BridgeError(404, 'app_not_found', '飞书应用不存在，请刷新页面')

    def _expected(self, value, data):
        if data.get('revision') != revision(value):
            raise BridgeError(409, 'settings_changed', '配置已在其他地方更新。请重新打开后修改，未保存内容仍保留在这里。')

    def catalog(self):
        apps, groups = [], []
        for ref, path, inbox in application_paths(self.config_path, self.inbox_path):
            value = load(path)
            if not value.get('app_id'): continue
            item = public(value)
            item.update(ref=ref, name=value.get('app_name') or value.get('bot_name') or value['app_id'],
                        revision=revision(value))
            item['runtime'] = (self.supervisor.status_for(ref) if self.supervisor and hasattr(self.supervisor, 'status_for')
                               else self.supervisor.status() if ref == 'default' and self.supervisor else '未启动')
            item['pairing_candidate'] = (Inbox(inbox).latest_pairing(value.get('pairing_code'))
                                         if inbox.exists() and value.get('pairing_code') else None)
            apps.append(item)
            for route in group_routes(value):
                route = {k: route[k] for k in ('chat_id', 'name', 'connection_id', 'prompt_template', 'enabled')}
                route.update(app_ref=ref, app_name=item['name'], revision=item['revision'])
                try:
                    connection = self.store.connection(route['connection_id'])
                    route.update(project_id=connection['project_id'], project_name=connection['project_name'])
                except BridgeError:
                    route.update(project_id='', project_name='项目不可用')
                groups.append(route)
        return {'apps': apps, 'groups': groups}

    def connection(self, value, project_id, name, old_id=None):
        self.store.project(project_id)
        for cid in (old_id, value.get('connection_id')):
            if not cid: continue
            old = self.store.connection(cid)
            if old['project_id'] == project_id:
                if old['status'] == '已停用':
                    raise BridgeError(400, 'connection_disabled', '对应连接已停用，请在连接页恢复或选择其他项目')
                return cid
        return self.store.create_connection(name[:60], project_id)['id']

    def save_app(self, data):
        if not isinstance(data, dict) or set(data) - {'ref','revision','name','app_id','app_secret','project_id','enabled','allowed_open_ids','prompt_template'}:
            raise BridgeError(400, 'invalid_request', '应用设置包含无效字段')
        with CONFIG_LOCK:
            ref = data.get('ref')
            current = load(self.resolve(ref)[1]) if ref else {}
            if ref: self._expected(current, data)
            name = data.get('name', current.get('app_name', ''))
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
                raise BridgeError(400, 'invalid_name', '请填写 1 到 60 字的应用名称')
            app_id = data.get('app_id', current.get('app_id'))
            if current.get('app_id') and app_id != current['app_id']:
                raise BridgeError(400, 'app_id_immutable', '更换应用请新增一份应用配置，避免影响已绑定的群')
            for other, path, _ in application_paths(self.config_path, self.inbox_path):
                if other != ref and load(path).get('app_id') == app_id:
                    raise BridgeError(409, 'duplicate_app', '这个应用已添加，请选择已有应用，共用连接即可')
            secret = data.get('app_secret') or current.get('app_secret')
            prompt = validate_prompt_template(data.get('prompt_template', current.get('prompt_template', DEFAULT_PROMPT_TEMPLATE)))
            if 'enabled' in data and not isinstance(data['enabled'], bool):
                raise BridgeError(400, 'invalid_request', '应用启用状态无效')
            users = data.get('allowed_open_ids', current.get('allowed_open_ids', []))
            if not isinstance(users, list) or len(users)>100 or any(not isinstance(x,str) or not x.startswith('ou_') or len(x)>128 for x in users):
                raise BridgeError(400, 'invalid_users', '允许私聊的用户格式不正确')
            if data.get('project_id'): self.store.project(data['project_id'])
            elif not current.get('connection_id'):
                raise BridgeError(400, 'missing_project', '请选择项目')
            checked = probe(app_id, secret) if not current or data.get('app_secret') else {
                k: current.get(k) for k in ('bot_ready','bot_open_id','bot_name')}
            enabled = data.get('enabled', current.get('enabled', False))
            if enabled and not checked.get('bot_ready'):
                raise BridgeError(400, 'bot_not_ready', '请先启用、发布机器人，再检查状态')
            # Freeze legacy group defaults before changing application-level private-chat defaults.
            routes = {r['chat_id']: r for r in group_routes(current)}
            cid = (self.connection(current, data['project_id'], name.strip()) if data.get('project_id') else current['connection_id'])
            ref = ref or 'app_' + uuid.uuid4().hex[:16]
            path = (self.resolve(ref)[1] if current else self.config_path.parent/'feishu-apps'/ref/'config.json')
            value = {**current, **checked, 'app_id':app_id, 'app_secret':secret, 'app_name':name.strip(),
                     'connection_id':cid, 'connection_key':self.store.connection(cid,reveal=True)['api_key'],
                     'enabled':enabled, 'allowed_open_ids':list(dict.fromkeys(users)),
                     'allowed_chat_ids':current.get('allowed_chat_ids', []), 'group_configs':routes,
                     'prompt_template':prompt}
            atomic_write(path, value)
            return ref

    def pairing(self, data):
        if not isinstance(data, dict) or set(data)-{'app_ref','action'}:
            raise BridgeError(400, 'invalid_request', '配对请求无效')
        with CONFIG_LOCK:
            _, path, _ = self.resolve(data.get('app_ref'))
            if data.get('action') == 'start': start_pairing(path, self.store)
            elif data.get('action') == 'check':
                value = load(path); checked = probe(value.get('app_id'),value.get('app_secret'))
                save(path, {}, self.store, internal={k:checked.get(k) for k in ('bot_ready','bot_open_id','bot_name')})
            else: raise BridgeError(400, 'invalid_request', '配对操作无效')

    def save_group(self, data):
        if not isinstance(data, dict) or set(data)-{'app_ref','revision','chat_id','name','project_id','prompt_template','enabled','pairing_code'}:
            raise BridgeError(400, 'invalid_request', '群配置包含无效字段')
        with CONFIG_LOCK:
            _, path, inbox = self.resolve(data.get('app_ref'))
            value = load(path); self._expected(value, data)
            chat = data.get('chat_id')
            if not isinstance(chat,str) or not re.fullmatch(r'oc_[A-Za-z0-9_-]{1,125}',chat):
                raise BridgeError(400, 'invalid_chat', '请先在目标群完成配对')
            name = data.get('name')
            if not isinstance(name,str) or not 1<=len(name.strip())<=60:
                raise BridgeError(400, 'invalid_name', '请填写 1 到 60 字的群配置名称')
            prompt = validate_prompt_template(data.get('prompt_template'))
            if not isinstance(data.get('enabled',True),bool):raise BridgeError(400,'invalid_request','群启用状态无效')
            existing = {r['chat_id']:r for r in group_routes(value)}
            if chat in existing and data.get('pairing_code'):
                raise BridgeError(409, 'group_exists', '这个群已有配置，请返回列表编辑，避免覆盖已有规则')
            if chat not in existing:
                code = value.get('pairing_code')
                candidate = Inbox(inbox).latest_pairing(code) if inbox.exists() and code else None
                if (not code or data.get('pairing_code')!=code or value.get('pairing_expires_at',0)<=time.time()
                        or not candidate or candidate.get('chat_type')!='group' or candidate.get('chat_id')!=chat):
                    raise BridgeError(409,'pairing_required','请在目标群 @机器人发送当前配对码，再确认添加')
            old = existing.get(chat,{})
            cid=self.connection(value,data.get('project_id'),name.strip(),old.get('connection_id'))
            existing[chat]={'chat_id':chat,'name':name.strip(),'connection_id':cid,'prompt_template':prompt,'enabled':data.get('enabled',True)}
            value.update(group_configs=existing,allowed_chat_ids=list(existing))
            if chat not in {r['chat_id'] for r in group_routes(load(path))}:
                value.update(pairing_code='',pairing_expires_at=0)
            atomic_write(path,value)

    def dispatch(self, path, method, data=None):
        if path == '/api/feishu/catalog' and method=='GET': return self.catalog()
        if path == '/api/feishu/apps' and method=='POST':
            ref=self.save_app(data); return {**self.catalog(),'saved_app_ref':ref}
        if path == '/api/feishu/groups' and method=='POST':
            self.save_group(data); return self.catalog()
        if path == '/api/feishu/app-pairing' and method=='POST':
            self.pairing(data); return self.catalog()
        raise BridgeError(404,'not_found','页面不存在')
