"""Copy one assistant's working rules without its Feishu identity or bindings."""
from __future__ import annotations

from codex_http_bridge import BridgeError
from feishu_groups import atomic_write, group_routes
from feishu_settings import CONFIG_LOCK, load, validate_prompt_template


FORMAT = 'codex-bridge-assistant-template'


def _validate_bundle(bundle):
    if (not isinstance(bundle, dict) or set(bundle) != {'format', 'version', 'assistant'}
            or bundle.get('format') != FORMAT or bundle.get('version') != 1):
        raise BridgeError(400, 'invalid_template', '这不是助手配置文件')
    assistant = bundle['assistant']
    if (not isinstance(assistant, dict) or set(assistant) != {
            'name', 'prompt_template', 'self_update_rule', 'backend', 'workspace_hint'}
            or not isinstance(assistant['name'], str)
            or not 1 <= len(assistant['name'].strip()) <= 60
            or not isinstance(assistant['self_update_rule'], str)
            or len(assistant['self_update_rule']) > 1600
            or assistant['backend'] not in ('codex', 'claude')
            or not isinstance(assistant['workspace_hint'], str)
            or len(assistant['workspace_hint']) > 100):
        raise BridgeError(400, 'invalid_template', '助手配置内容无效')
    validate_prompt_template(assistant['prompt_template'])
    return assistant


def export_template(settings, data):
    if not isinstance(data, dict) or set(data) != {'ref', 'revision'}:
        raise BridgeError(400, 'invalid_request', '请选择一位要复制设置的助手')
    with CONFIG_LOCK:
        _, path, _ = settings.resolve(data['ref'])
        cfg = load(path)
        settings._expected(cfg, data)
        if cfg.get('assistant_manager') or cfg.get('task_dispatcher'):
            raise BridgeError(409, 'special_assistant', '这位助手依赖本机管理记录，暂不能复制设置')
        connection = settings.store.connection(cfg['connection_id'])
        project_name = next((p['name'] for p in settings.store.projects()
                             if p['id'] == connection['project_id']), '')
        assistant = {
            'name': cfg.get('app_name') or cfg.get('bot_name') or '助手',
            'prompt_template': cfg.get('prompt_template'),
            'self_update_rule': cfg.get('self_update_rule', ''),
            'backend': connection['execution_backend'],
            'workspace_hint': project_name[:100],
        }
        return {'bundle': {'format': FORMAT, 'version': 1, 'assistant': _validate_bundle({
            'format': FORMAT, 'version': 1, 'assistant': assistant})}, 'count': 1}


def apply_template(settings, data):
    if not isinstance(data, dict) or set(data) != {'bundle', 'target_ref', 'revision', 'project_id'}:
        raise BridgeError(400, 'invalid_request', '请选择目标机器人和工作文件夹')
    assistant = _validate_bundle(data['bundle'])
    with CONFIG_LOCK:
        _, path, _ = settings.resolve(data['target_ref'])
        cfg = load(path)
        settings._expected(cfg, data)
        if cfg.get('assistant_manager') or cfg.get('task_dispatcher'):
            raise BridgeError(409, 'special_assistant', '不能把普通助手设置覆盖到助手管理员或任务管家')
        settings.store.project(data['project_id'])
        # Freeze legacy group defaults before changing the target's base prompt or folder.
        routes = {route['chat_id']: route for route in group_routes(cfg)}
        connection_id = settings.connection(cfg, data['project_id'],
                                            cfg.get('app_name') or cfg.get('bot_name') or '助手',
                                            old_id=cfg.get('connection_id'))
        connection = settings.store.connection(connection_id, reveal=True)
        updated = {**cfg, 'connection_id': connection_id, 'connection_key': connection['api_key'],
                   'group_configs': routes, 'prompt_template': assistant['prompt_template'],
                   'self_update_rule': assistant['self_update_rule']}
        atomic_write(path, updated)
        return {'applied': True, 'name': cfg.get('app_name') or cfg.get('bot_name') or '助手',
                'execution_backend': connection['execution_backend'],
                'source_backend': assistant['backend']}
