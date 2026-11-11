"""Portable Feishu routing settings. No task history is transferred."""
import base64
import json
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from codex_http_bridge import BridgeError
from feishu_groups import application_paths, atomic_write, group_routes
from feishu_settings import CONFIG_LOCK, load, validate_prompt_template


def _key(password, salt):
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600_000).derive(password.encode())


def _field(value):
    return base64.urlsafe_b64encode(value).decode()


def _decode(value):
    return base64.urlsafe_b64decode(value.encode())


def export_bundle(settings, data):
    if not isinstance(data, dict) or set(data) != {'selected'}:
        raise BridgeError(400, 'invalid_request', '导出请求无效')
    selected = data['selected']
    if not isinstance(selected, list) or not 1 <= len(selected) <= 100:
        raise BridgeError(400, 'invalid_request', '请选择要导出的机器人')
    if any(not isinstance(item, dict) or set(item) != {'ref', 'revision'} for item in selected):
        raise BridgeError(400, 'invalid_request', '机器人选择无效')
    if len({item['ref'] for item in selected}) != len(selected):
        raise BridgeError(400, 'invalid_request', '机器人重复选择')
    records, warnings = [], ['助手之间的长期协作授权需要在目标电脑重新设置']
    with CONFIG_LOCK:
        for item in selected:
            _, path, _ = settings.resolve(item['ref'])
            cfg = load(path)
            settings._expected(cfg, item)
            if cfg.get('assistant_manager') or cfg.get('task_dispatcher'):
                raise BridgeError(409, 'special_bot_migration',
                                  '助手管理员和任务管家依赖本机管理记录，暂不能用配置包迁移；请取消选择后导出')
            if not cfg.get('app_id') or not cfg.get('app_secret'):
                raise BridgeError(409, 'missing_credentials', '所选机器人缺少应用凭证')
            connection = settings.store.connection(cfg['connection_id'])
            project = settings.store.project(connection['project_id'])
            groups = []
            for route in group_routes(cfg):
                group_connection = settings.store.connection(route['connection_id'])
                group_project = settings.store.project(group_connection['project_id'])
                groups.append({k: route[k] for k in ('chat_id', 'name', 'prompt_template', 'enabled', 'topic_collaboration')}
                              | {'project_path': group_project['path'], 'backend': group_connection['execution_backend'],
                                 'self_update_rule': route.get('self_update_rule', '')})
            records.append({
                'app_id': cfg['app_id'], 'app_secret': cfg['app_secret'],
                'kind': 'standard',
                'name': cfg.get('app_name') or cfg.get('bot_name') or cfg['app_id'],
                'prompt_template': cfg.get('prompt_template'),
                'self_update_rule': cfg.get('self_update_rule', ''),
                'self_update_owner_open_id': cfg.get('auto_group_owner_open_id') or '',
                'allowed_open_ids': cfg.get('allowed_open_ids', []),
                'project_path': project['path'], 'backend': connection['execution_backend'],
                'groups': groups,
            })
    return {'bundle': {'format': 'codex-bridge-feishu-migration', 'version': 2, 'apps': records},
            'count': len(records), 'warnings': warnings}


def import_bundle(settings, data):
    if not isinstance(data, dict) or set(data) not in ({'bundle', 'project_id'}, {'bundle', 'password', 'project_id'}):
        raise BridgeError(400, 'invalid_request', '导入请求无效')
    bundle, project_id = data['bundle'], data['project_id']
    if (not isinstance(bundle, dict) or bundle.get('format') != 'codex-bridge-feishu-migration' or
            bundle.get('version') not in (1, 2) or not isinstance(project_id, str)):
        raise BridgeError(400, 'invalid_request', '迁移包格式无效')
    settings.store.project(project_id)
    if bundle['version'] == 1:
        password = data.get('password')
        if not isinstance(password, str) or not password:
            raise BridgeError(400, 'migration_password', '旧加密包需要填写原来的迁移密码')
        try:
            raw = AESGCM(_key(password, _decode(bundle['salt']))).decrypt(
                _decode(bundle['nonce']), _decode(bundle['data']), b'codex-bridge-feishu-migration-v1')
            content = json.loads(raw)
        except (InvalidTag, KeyError, ValueError, TypeError):
            raise BridgeError(400, 'migration_password', '迁移包或密码不正确') from None
        apps = content.get('apps') if isinstance(content, dict) and content.get('version') == 1 else None
    else:
        if 'password' in data or set(bundle) != {'format', 'version', 'apps'}:
            raise BridgeError(400, 'invalid_bundle', '迁移包内容无效')
        apps = bundle.get('apps')
    if not isinstance(apps, list) or not 1 <= len(apps) <= 100:
        raise BridgeError(400, 'invalid_bundle', '迁移包内容无效')
    with CONFIG_LOCK:
        existing = {load(path).get('app_id') for _, path, _ in application_paths(settings.config_path, settings.inbox_path)}
        ids = [app.get('app_id') for app in apps if isinstance(app, dict)]
        if len(set(ids)) != len(ids) or any(app_id in existing for app_id in ids):
            raise BridgeError(409, 'duplicate_app', '迁移包有重复机器人，或目标电脑已经接入其中的机器人')
        # Validate every record before creating a connection or writing a config.
        for app in apps:
            if (not isinstance(app, dict) or not re.fullmatch(r'cli_[a-z0-9]{16}', app.get('app_id') or '') or
                    app.get('kind') != 'standard' or
                    not isinstance(app.get('app_secret'), str) or not app['app_secret'] or
                    not isinstance(app.get('name'), str) or not 1 <= len(app['name'].strip()) <= 60 or
                    not isinstance(app.get('allowed_open_ids'), list) or
                    any(not isinstance(x, str) or not x.startswith('ou_') for x in app['allowed_open_ids']) or
                    not isinstance(app.get('project_path'), str) or
                    not isinstance(app.get('groups'), list) or len(app['groups']) > 100 or
                    app.get('backend') not in ('codex', 'claude')):
                raise BridgeError(400, 'invalid_bundle', '迁移包内容无效')
            validate_prompt_template(app.get('prompt_template'))
            if (not isinstance(app.get('self_update_rule', ''), str) or len(app.get('self_update_rule', '')) > 1600
                    or not isinstance(app.get('self_update_owner_open_id', ''), str)
                    or (app.get('self_update_owner_open_id') and
                        not re.fullmatch(r'ou_[A-Za-z0-9_-]{1,125}', app['self_update_owner_open_id']))):
                raise BridgeError(400, 'invalid_bundle', '助手更新规则无效')
            seen_chats = set()
            for group in app['groups']:
                if (not isinstance(group, dict) or group.get('backend') not in ('codex', 'claude') or
                        not re.fullmatch(r'oc_[A-Za-z0-9_-]{1,125}', group.get('chat_id') or '') or
                        group['chat_id'] in seen_chats or
                        not isinstance(group.get('name'), str) or not 1 <= len(group['name']) <= 60 or
                        not isinstance(group.get('project_path'), str) or
                        not isinstance(group.get('enabled'), bool) or
                        not isinstance(group.get('topic_collaboration'), bool)):
                    raise BridgeError(400, 'invalid_bundle', '迁移包中的群配置无效')
                seen_chats.add(group['chat_id'])
                validate_prompt_template(group.get('prompt_template'))
                if not isinstance(group.get('self_update_rule', ''), str) or len(group.get('self_update_rule', '')) > 1600:
                    raise BridgeError(400, 'invalid_bundle', '群内助手更新规则无效')
        from feishu_groups import probe
        for app in apps:
            probe(app['app_id'], app['app_secret'])
        created = []
        for app in apps:
            ref = settings.save_app({'name': app['name'], 'app_id': app['app_id'],
                'app_secret': app['app_secret'], 'project_id': project_id, 'enabled': False,
                'allowed_open_ids': app['allowed_open_ids'], 'prompt_template': app['prompt_template']})
            _, path, _ = settings.resolve(ref)
            cfg = load(path)
            cfg['self_update_rule'] = app.get('self_update_rule', '')
            if app.get('self_update_owner_open_id'):
                cfg['auto_group_owner_open_id'] = app['self_update_owner_open_id']
            settings.store.set_execution_backend([cfg['connection_id']], app['backend'])
            routes = {}
            for group in app['groups']:
                cid = settings.store.create_connection(group['name'][:60], project_id)['id']
                settings.store.set_execution_backend([cid], group['backend'])
                routes[group['chat_id']] = {k: group[k] for k in
                    ('chat_id', 'name', 'prompt_template', 'enabled', 'topic_collaboration')}
                if group.get('self_update_rule'):
                    routes[group['chat_id']]['self_update_rule'] = group['self_update_rule']
                routes[group['chat_id']]['connection_id'] = cid
            cfg.update(allowed_chat_ids=list(routes), group_configs=routes, enabled=False,
                       migration_project_paths={'app': app['project_path'],
                                                'groups': {g['chat_id']: g['project_path'] for g in app['groups']}})
            atomic_write(path, cfg)
            created.append({'name': app['name'], 'app_id': app['app_id'], 'ref': ref, 'groups': len(routes)})
    return {'created': created, 'enabled': False,
            'message': '已导入并保持停用。核对目标电脑的工作文件夹和执行工具后，再逐个启用；旧电脑随后停用。'}


def stop_selected(settings, data):
    if (not isinstance(data, dict) or set(data) not in ({'selected', 'target_prepared'},
                                                        {'selected', 'target_verified'}) or
            not (data.get('target_prepared') is True or data.get('target_verified') is True) or
            not isinstance(data['selected'], list) or
            not 1 <= len(data['selected']) <= 100):
        raise BridgeError(400, 'invalid_request', '请先在新电脑导入并核对配置')
    if (len({x.get('ref') for x in data['selected'] if isinstance(x, dict)}) != len(data['selected']) or
            any(not isinstance(x, dict) or set(x) != {'ref', 'revision'} for x in data['selected'])):
        raise BridgeError(400, 'invalid_request', '机器人选择无效')
    with CONFIG_LOCK:
        configs = []
        for item in data['selected']:
            _, path, _ = settings.resolve(item['ref'])
            cfg = load(path)
            settings._expected(cfg, item)
            configs.append((path, cfg))
        for path, cfg in configs:
            if cfg.get('enabled'):
                atomic_write(path, {**cfg, 'enabled': False})
    return {'stopped': len(configs)}
