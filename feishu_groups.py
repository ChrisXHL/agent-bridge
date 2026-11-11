"""Per-application credentials and per-group prompt routes; legacy config stays in place."""
from __future__ import annotations
import hashlib
import base64
import concurrent.futures
import json
import os
import re
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from codex_http_bridge import BridgeError
from feishu_settings import CONFIG_LOCK, DEFAULT_PROMPT_TEMPLATE, load, public, save, start_pairing, validate_prompt_template
from feishu_probe import probe
from feishu_probe import check_topic_permissions, chat_name
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
                  'enabled': True,'topic_collaboration':False}, **overrides.get(chat, {}))
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


class AccountIdentity:
    """Best-effort Feishu tenant grouping; never exposes or persists credentials."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}

    @staticmethod
    def fetch(app_id, secret):
        if not re.fullmatch(r'cli_[a-z0-9]{16}', app_id or '') or not secret:
            return ''
        body = json.dumps({'grant_type': 'client_credentials', 'client_id': app_id,
                           'client_secret': secret}).encode()
        request = urllib.request.Request('https://accounts.feishu.cn/oauth/v3/token',
                                         data=body, headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                token = json.load(response).get('access_token', '')
            payload = token.split('.')[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4)))
            tenant = str(claims.get('tid') or '')
            return tenant if tenant.isdigit() else ''
        except (OSError, ValueError, IndexError, KeyError, TypeError):
            return ''

    def resolve(self, entries):
        now = time.time()
        pending = {}
        with self.lock:
            for _, app_id, secret in entries:
                key = (app_id, hashlib.sha256(secret.encode()).hexdigest())
                if key not in self.cache or self.cache[key][1] <= now:
                    pending[key] = (app_id, secret)
        if pending:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                found = dict(zip(pending, pool.map(lambda pair: self.fetch(*pair), pending.values())))
            with self.lock:
                for key, tenant in found.items():
                    self.cache[key] = (tenant, now + (86400 if tenant else 300))
        with self.lock:
            return {ref: self.cache.get((app_id, hashlib.sha256(secret.encode()).hexdigest()), ('', 0))[0]
                    for ref, app_id, secret in entries}


def cli_profiles():
    """Read only profile names associated with installed lark-cli applications."""
    path = Path.home() / '.lark-cli' / 'config.json'
    try:
        apps = json.loads(path.read_text(encoding='utf-8')).get('apps', [])
        return {app['appId']: app.get('name') or 'default' for app in apps
                if isinstance(app, dict) and isinstance(app.get('appId'), str)}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


class GroupSettings:
    def __init__(self, config_path, inbox_path, store, supervisor=None):
        self.config_path, self.inbox_path = Path(config_path), Path(inbox_path)
        self.store, self.supervisor = store, supervisor
        self.account_identity = AccountIdentity()
        self.account_labels_path = self.config_path.parent / 'feishu-account-labels.json'
        self.account_archives_path = self.config_path.parent / 'feishu-account-archives.json'
        self.recover_account_archives()

    def account_archives(self):
        try:
            value = load(self.account_archives_path)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def managed_connection_ids(self):
        ids = set()
        for _, path, _ in application_paths(self.config_path, self.inbox_path):
            value = load(path)
            ids.update(cid for cid in
                       [value.get('connection_id'), *(route['connection_id'] for route in group_routes(value))]
                       if cid)
        return ids

    def recover_account_archives(self):
        with CONFIG_LOCK:
            archives = self.account_archives()
            for account_id, record in list(archives.items()):
                if record.get('state') in ('removing', 'restoring'):
                    self._apply_account_archive(account_id, record, archives)

    def _apply_account_archive(self, account_id, record, archives):
        restoring = record['state'] == 'restoring'
        for item in record['apps']:
            _, path, _ = self.resolve(item['ref'])
            value = load(path)
            if value.get('app_id') != item['app_id']:
                raise BridgeError(409, 'settings_changed', '机器人配置已变化，请先核对账号')
            if restoring:
                if value.get('account_archived') not in (None, account_id):
                    raise BridgeError(409, 'settings_changed', '机器人已归入另一个账号，请先核对')
            else:
                if value.get('account_archived') not in (None, account_id):
                    raise BridgeError(409, 'settings_changed', '机器人已归入另一个账号，请先核对')
            if not restoring:
                value['enabled'] = False
                value['account_archived'] = account_id
                atomic_write(path, value)
        for cid, status in record['connections'].items():
            self.store.update_connection(cid, {'status': status if restoring else '已停用'})
        if restoring:
            for item in record['apps']:
                _, path, _ = self.resolve(item['ref'])
                value = load(path)
                value.pop('account_archived', None)
                value['enabled'] = item['enabled']
                atomic_write(path, value)
            archives.pop(account_id, None)
        else:
            record['state'] = 'removed'
        atomic_write(self.account_archives_path, archives)

    def remove_account(self, data):
        if not isinstance(data, dict) or set(data) != {'account_id', 'expected'}:
            raise BridgeError(400, 'invalid_request', '移除账号请求无效')
        account_id, expected = data['account_id'], data['expected']
        if not isinstance(account_id, str) or not isinstance(expected, list) or not expected:
            raise BridgeError(400, 'invalid_request', '请刷新账号列表后重试')
        with CONFIG_LOCK:
            archives = self.account_archives()
            if account_id in archives:
                raise BridgeError(409, 'account_removed', '账号已移除，可在下方恢复')
            current = self.catalog()
            apps = [a for a in current['apps'] if a['account_id'] == account_id]
            if not apps or {a['ref']: a['revision'] for a in apps} != {
                    x.get('ref'): x.get('revision') for x in expected if isinstance(x, dict)} or len(expected) != len(apps):
                raise BridgeError(409, 'settings_changed', '账号下的机器人已变化，请刷新后重试')
            refs = {a['ref'] for a in apps}
            configs = {ref: (path, load(path), inbox) for ref, path, inbox in
                       application_paths(self.config_path, self.inbox_path) if ref in refs}
            ids = {cid for _, value, _ in configs.values() for cid in
                   ([value.get('connection_id')] + [r['connection_id'] for r in group_routes(value)]) if cid}
            for ref, path, _ in application_paths(self.config_path, self.inbox_path):
                if ref in refs: continue
                value = load(path)
                other = {value.get('connection_id')} | {r['connection_id'] for r in group_routes(value)}
                if ids & other:
                    raise BridgeError(409, 'shared_connection', '这个账号与其他账号共用了连接，请先分开连接')
            with self.store.db() as db:
                historical = self.store.history.tasks()
                if any(row['id'] not in historical for cid in ids for row in db.execute(
                        "SELECT id FROM tasks WHERE connection_id=? AND status NOT IN ('已完成','失败','已停止')", (cid,))):
                    raise BridgeError(409, 'account_busy', '这个账号还有未结束的任务，请处理完后再移除')
            for ref, (_, value, inbox) in configs.items():
                if inbox.exists() and (any(json.loads(x.get('route_json') or '{}').get('mode','prompt') == 'prompt'
                                           for x in Inbox(inbox).pending()) or Inbox(inbox).submitted()):
                    raise BridgeError(409, 'account_busy', '这个账号还有待处理消息，请处理完后再移除')
                if value.get('assistant_manager'):
                    for path in (self.config_path.parent/'assistant-management').glob('mgr_*/request.json'):
                        request = load(path)
                        if request.get('app_ref') == ref and (
                                request.get('status') in ('pending','watching') or
                                (request.get('status') == 'done' and
                                 request.get('notifications',{}).get('result',{}).get('state') != 'sent')):
                            raise BridgeError(409, 'account_busy', '助手管理员还有未完成的请求，请处理完后再移除')
            for path in (self.config_path.parent/'assistant-onboarding').glob('asst_*/job.json'):
                job = load(path)
                if job.get('app_ref') in refs and job.get('phase') not in ('ready', 'removed'):
                    raise BridgeError(409, 'account_busy', '账号内还有正在创建的助手，请完成后再移除')
            record = {'state': 'removing', 'name': next((a['account_name'] for a in apps if a['account_name']), apps[0]['name']+' 所在账号'),
                      'apps': [{'ref': a['ref'], 'app_id': a['app_id'], 'name': a['name'],
                                'enabled': configs[a['ref']][1].get('enabled', False)} for a in apps],
                      'connections': {cid: self.store.connection(cid)['status'] for cid in ids},
                      'group_count': sum(g['app_ref'] in refs for g in current['groups'])}
            archives[account_id] = record
            atomic_write(self.account_archives_path, archives)
            self._apply_account_archive(account_id, record, archives)
            return self.catalog()

    def restore_account(self, data):
        if not isinstance(data, dict) or set(data) != {'account_id'} or not isinstance(data['account_id'], str):
            raise BridgeError(400, 'invalid_request', '恢复账号请求无效')
        with CONFIG_LOCK:
            archives = self.account_archives()
            account_id = data['account_id']
            record = archives.get(account_id)
            if not record or record.get('state') != 'removed':
                raise BridgeError(404, 'account_not_found', '已移除的账号不存在，请刷新后重试')
            for item in record['apps']:
                _, path, _ = self.resolve(item['ref'])
                value = load(path)
                if value.get('app_id') != item['app_id'] or value.get('account_archived') != account_id:
                    raise BridgeError(409, 'settings_changed', '机器人配置已变化，请先核对账号')
            record['state'] = 'restoring'
            atomic_write(self.account_archives_path, archives)
            self._apply_account_archive(account_id, record, archives)
            return self.catalog()

    def account_labels(self):
        try:
            value = load(self.account_labels_path)
            return {key: name for key, name in value.items()
                    if isinstance(key, str) and isinstance(name, str) and 1 <= len(name) <= 60}
        except (OSError, ValueError):
            return {}

    def save_account_label(self, data):
        if not isinstance(data, dict) or set(data) != {'account_id', 'name'}:
            raise BridgeError(400, 'invalid_request', '账号名称请求无效')
        account_id, name = data['account_id'], data['name']
        if not isinstance(account_id, str) or not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise BridgeError(400, 'invalid_name', '请填写 1 到 60 字的账号名称')
        if account_id not in {app['account_id'] for app in self.catalog()['apps']}:
            raise BridgeError(404, 'account_not_found', '账号已变化，请刷新页面后重试')
        with CONFIG_LOCK:
            labels = self.account_labels()
            labels[account_id] = name.strip()
            atomic_write(self.account_labels_path, labels)

    def resolve(self, ref):
        for item in application_paths(self.config_path, self.inbox_path):
            if item[0] == ref: return item
        raise BridgeError(404, 'app_not_found', '飞书应用不存在，请刷新页面')

    def _expected(self, value, data):
        if value.get('assistant_archived'):
            raise BridgeError(409, 'assistant_removed', '助手已移除，请先从“已移除的助手”中恢复。')
        if value.get('account_archived'):
            raise BridgeError(409, 'account_removed', '账号已移除，请先在账号列表中恢复。')
        if data.get('revision') != revision(value):
            raise BridgeError(409, 'settings_changed', '配置已在其他地方更新。请重新打开后修改，未保存内容仍保留在这里。')

    def catalog(self):
        apps, groups = [], []
        configs = []
        managed_connection_ids = self.managed_connection_ids()
        for ref, path, inbox in application_paths(self.config_path, self.inbox_path):
            value = load(path)
            if not value.get('app_id') or value.get('assistant_archived') or value.get('account_archived'): continue
            configs.append((ref, value.get('app_id', ''), value.get('app_secret', '')))
            item = public(value)
            item.update(ref=ref, name=value.get('app_name') or value.get('bot_name') or value['app_id'],
                        revision=revision(value),
                        self_update_rule=value.get('self_update_rule', ''),
                        migration_supported=not (value.get('assistant_manager') or value.get('task_dispatcher')))
            try:
                connection = self.store.connection(value['connection_id'])
                item['execution_backend'] = connection['execution_backend']
                project = self.store.project(connection['project_id'])
                item['project_id'], item['project_name'], item['project_path'] = project['id'], project['name'], project['path']
            except (BridgeError, KeyError):
                item['execution_backend'] = 'codex'
                item['project_id'], item['project_name'], item['project_path'] = '', '项目不可用', ''
            item['runtime'] = (self.supervisor.status_for(ref) if self.supervisor and hasattr(self.supervisor, 'status_for')
                               else self.supervisor.status() if ref == 'default' and self.supervisor else '未启动')
            item['pairing_candidate'] = (Inbox(inbox).latest_pairing(value.get('pairing_code'))
                                         if inbox.exists() and value.get('pairing_code') else None)
            apps.append(item)
            for route in group_routes(value):
                route = {k: route[k] for k in ('chat_id', 'name', 'connection_id', 'prompt_template', 'enabled','topic_collaboration')}
                route['self_update_rule'] = value.get('group_configs', {}).get(route['chat_id'], {}).get('self_update_rule', '')
                route.update(app_ref=ref, app_name=item['name'], revision=item['revision'])
                try:
                    connection = self.store.connection(route['connection_id'])
                    project = self.store.project(connection['project_id'])
                    route.update(project_id=project['id'], project_name=project['name'], project_path=project['path'])
                except BridgeError:
                    route.update(project_id='', project_name='项目不可用', project_path='')
                groups.append(route)
        accounts = self.account_identity.resolve(configs)
        profiles = cli_profiles()
        labels = self.account_labels()
        for item in apps:
            tenant = accounts.get(item['ref'], '')
            item['account_id'] = tenant or 'unverified:' + item['ref']
            item['account_verified'] = bool(tenant)
            item['account_name'] = labels.get(item['account_id'], '')
            item['cli_profile'] = profiles.get(item['app_id'], '')
        return {'apps': apps, 'groups': groups,
                'managed_connection_ids': sorted(managed_connection_ids),
                'archived_accounts': [{'account_id': account_id, 'name': record['name'],
                                       'app_count': len(record['apps']), 'group_count': record['group_count'],
                                       'state': record['state']}
                                      for account_id, record in self.account_archives().items()],
                'execution_cli': {'name': '按机器人选择', 'options': ['codex', 'claude']},
                'capabilities': {'group_topics': True}}

    def connection(self, value, project_id, name, old_id=None):
        self.store.project(project_id)
        for cid in (old_id, value.get('connection_id')):
            if not cid: continue
            old = self.store.connection(cid)
            if old['project_id'] == project_id:
                if old['status'] == '已停用':
                    raise BridgeError(400, 'connection_disabled', '对应连接已停用，请在连接页恢复或选择其他项目')
                return cid
        created = self.store.create_connection(name[:60], project_id)['id']
        original = value.get('connection_id')
        if original:
            backend = self.store.connection(original)['execution_backend']
            self.store.set_execution_backend([created], backend)
        return created

    def set_app_backend(self, data):
        if not isinstance(data, dict) or set(data) != {'app_ref', 'revision', 'execution_backend'}:
            raise BridgeError(400, 'invalid_request', '切换执行工具的请求无效')
        ref = data['app_ref']
        if data['execution_backend'] == 'claude':
            from claude_runtime import ClaudeIdentityRequired, run_structured
            directory = Path(self.store.db_path).parent
            try:
                checked = run_structured('仅用于检查 Claude Code 是否可执行。返回 ready=true。',
                               {'type':'object','properties':{'ready':{'type':'boolean'}},
                                'required':['ready'],'additionalProperties':False},
                               directory, directory/'claude-backend-check.log', timeout=35)
                if checked.get('ready') is not True:
                    raise RuntimeError('Claude Code readiness check was not affirmative')
            except ClaudeIdentityRequired as exc:
                raise BridgeError(409, 'claude_identity_required', 'Claude Code 账号需要先完成身份验证，验证后再切换。现有机器人保持原工具。') from exc
            except Exception as exc:
                raise BridgeError(503, 'claude_unavailable', 'Claude Code 暂时无法执行，请检查本机登录与 CLI；机器人仍使用原工具。') from exc
        with CONFIG_LOCK:
            _, path, _ = self.resolve(ref)
            value = load(path)
            self._expected(value, data)
            own = {value.get('connection_id')} | {r['connection_id'] for r in group_routes(value)}
            own.discard(None)
            for other_ref, other_path, _ in application_paths(self.config_path, self.inbox_path):
                if other_ref == ref:
                    continue
                other = load(other_path)
                used = {other.get('connection_id')} | {r['connection_id'] for r in group_routes(other)}
                if own & used:
                    raise BridgeError(409, 'shared_connection', '这个机器人与其他机器人共用连接，请先分开连接')
            self.store.set_execution_backend(own, data['execution_backend'])
            return self.catalog()

    def set_app_enabled(self, data):
        if (not isinstance(data, dict) or set(data) != {'app_ref', 'revision', 'enabled'}
                or not isinstance(data['enabled'], bool)):
            raise BridgeError(400, 'invalid_request', '机器人启用状态请求无效')
        with CONFIG_LOCK:
            _, path, _ = self.resolve(data['app_ref'])
            value = load(path)
            self._expected(value, data)
            if data['enabled'] and not value.get('bot_ready'):
                raise BridgeError(409, 'bot_not_ready', '机器人尚未在飞书启用并发布，请先完成飞书设置')
            if value.get('enabled') != data['enabled']:
                atomic_write(path, {**value, 'enabled': data['enabled']})
            return self.catalog()

    def save_app(self, data, manager_admin=None):
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
            if manager_admin:
                value['assistant_manager']={'admin_open_id':manager_admin}
            atomic_write(path, value)
            return ref

    def pairing(self, data):
        if not isinstance(data, dict) or set(data)-{'app_ref','action'}:
            raise BridgeError(400, 'invalid_request', '配对请求无效')
        with CONFIG_LOCK:
            _, path, _ = self.resolve(data.get('app_ref'))
            if load(path).get('assistant_archived'):
                raise BridgeError(409, 'assistant_removed', '助手已移除，请先恢复后再配对。')
            if data.get('action') == 'start': start_pairing(path, self.store)
            elif data.get('action') == 'check':
                value = load(path); checked = probe(value.get('app_id'),value.get('app_secret'))
                save(path, {}, self.store, internal={k:checked.get(k) for k in ('bot_ready','bot_open_id','bot_name')})
            else: raise BridgeError(400, 'invalid_request', '配对操作无效')

    def save_group(self, data):
        if not isinstance(data, dict) or set(data)-{'app_ref','revision','chat_id','name','project_id','prompt_template','enabled','pairing_code','topic_collaboration'}:
            raise BridgeError(400, 'invalid_request', '群配置包含无效字段')
        with CONFIG_LOCK:
            _, path, inbox = self.resolve(data.get('app_ref'))
            value = load(path); self._expected(value, data)
            chat = data.get('chat_id')
            if not isinstance(chat,str) or not re.fullmatch(r'oc_[A-Za-z0-9_-]{1,125}',chat):
                raise BridgeError(400, 'invalid_chat', '请先在目标群完成配对')
            name = data.get('name')
            if not isinstance(name,str) or len(name.strip())>60:
                raise BridgeError(400, 'invalid_name', '群名称不能超过 60 字')
            prompt = validate_prompt_template(data.get('prompt_template'))
            if not isinstance(data.get('enabled',True),bool):raise BridgeError(400,'invalid_request','群启用状态无效')
            if 'topic_collaboration' in data and not isinstance(data['topic_collaboration'],bool):raise BridgeError(400,'invalid_request','话题协作状态无效')
            existing = {r['chat_id']:r for r in group_routes(value)}
            if chat in existing and data.get('pairing_code'):
                raise BridgeError(409, 'group_exists', '这个群已有配置，请返回列表编辑，避免覆盖已有规则')
            if chat not in existing:
                code = value.get('pairing_code')
                candidate = Inbox(inbox).latest_pairing(code) if inbox.exists() and code else None
                if (not code or data.get('pairing_code')!=code or value.get('pairing_expires_at',0)<=time.time()
                        or not candidate or candidate.get('chat_type')!='group' or candidate.get('chat_id')!=chat):
                    raise BridgeError(409,'pairing_required','请在目标群 @机器人发送当前配对码，再确认添加')
                if not name.strip():
                    name = chat_name(value['app_id'], value['app_secret'], chat) or ''
                    if not name:
                        raise BridgeError(409, 'group_name_unavailable',
                                          '暂时无法从飞书读取群名，请手动填写群名称后保存。')
            elif not name.strip():
                raise BridgeError(400, 'invalid_name', '请填写群名称')
            old = existing.get(chat,{})
            if data.get('enabled',True) and data.get('topic_collaboration',old.get('topic_collaboration',False)):
                check_topic_permissions(value['app_id'],value['app_secret'])
            cid=self.connection(value,data.get('project_id'),name.strip(),old.get('connection_id'))
            existing[chat]={'chat_id':chat,'name':name.strip(),'connection_id':cid,'prompt_template':prompt,'enabled':data.get('enabled',True)}
            existing[chat]['topic_collaboration']=data.get('topic_collaboration',old.get('topic_collaboration',False))
            if old.get('self_update_rule'):
                existing[chat]['self_update_rule']=old['self_update_rule']
            value.update(group_configs=existing,allowed_chat_ids=list(existing))
            if chat not in {r['chat_id'] for r in group_routes(load(path))}:
                value.update(pairing_code='',pairing_expires_at=0)
            atomic_write(path,value)

    def group_defaults(self,data):
        if not isinstance(data,dict) or set(data)!={'app_ref'}:raise BridgeError(400,'invalid_request','请选择应用。')
        ref,path,_=self.resolve(data['app_ref']);cfg=load(path)
        if cfg.get('assistant_manager'):raise BridgeError(400,'invalid_source','助手管理员仅限创建者私聊管理。')
        # Reuse the saved business purpose, with group delivery instead of private-owner delivery.
        for p in (self.config_path.parent/'assistant-onboarding').glob('asst_*/job.json'):
            j=load(p)
            if j.get('app_ref')==ref and j.get('plan') and j.get('profile'):
                from assistant_onboarding import build_prompt
                credentials=load(p.parent/'credentials.json')
                if credentials.get('app_id')==cfg.get('app_id'):
                    return {'prompt_template':build_prompt(j,credentials,group=True)}
        template=cfg.get('prompt_template',DEFAULT_PROMPT_TEMPLATE)
        return {'prompt_template':validate_prompt_template('本次来自已绑定的飞书群。以下原规则中的私聊接收者不适用于本次群任务；交付到本次来源群，不向个人另行发送，不分享旧资料或个人账号权限。\n\n'+template)}

    def dispatch(self, path, method, data=None):
        if path == '/api/feishu/assistant-template/export' and method == 'POST':
            from assistant_template import export_template
            return export_template(self, data)
        if path == '/api/feishu/assistant-template/apply' and method == 'POST':
            from assistant_template import apply_template
            return apply_template(self, data)
        if path == '/api/feishu/migration/export' and method == 'POST':
            from feishu_migration import export_bundle
            return export_bundle(self, data)
        if path == '/api/feishu/migration/import' and method == 'POST':
            from feishu_migration import import_bundle
            return import_bundle(self, data)
        if path == '/api/feishu/migration/stop' and method == 'POST':
            from feishu_migration import stop_selected
            return stop_selected(self, data)
        if path=='/api/feishu/group-defaults' and method=='POST':return self.group_defaults(data)
        if path == '/api/feishu/catalog' and method=='GET': return self.catalog()
        if path == '/api/feishu/accounts' and method == 'POST':
            self.save_account_label(data); return self.catalog()
        if path == '/api/feishu/accounts/remove' and method == 'POST':return self.remove_account(data)
        if path == '/api/feishu/accounts/restore' and method == 'POST':return self.restore_account(data)
        if path == '/api/feishu/apps/backend' and method == 'POST':return self.set_app_backend(data)
        if path == '/api/feishu/apps/enabled' and method == 'POST':return self.set_app_enabled(data)
        if path == '/api/feishu/apps' and method=='POST':
            ref=self.save_app(data); return {**self.catalog(),'saved_app_ref':ref}
        if path == '/api/feishu/groups' and method=='POST':
            self.save_group(data); return self.catalog()
        if path == '/api/feishu/app-pairing' and method=='POST':
            self.pairing(data); return self.catalog()
        raise BridgeError(404,'not_found','页面不存在')
