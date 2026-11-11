"""Conversation-scoped updates to an assistant's working rules."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from pathlib import Path

from codex_http_bridge import BridgeError
from feishu_groups import atomic_write, group_routes
from feishu_settings import CONFIG_LOCK, load


MAX_RULE_LENGTH = 1600


def explicit_request(text):
    """Keep write access out of ordinary tasks and quoted source material."""
    if not isinstance(text, str) or not 5 <= len(text.strip()) <= 2000:
        return False
    head = text.strip()[:60]
    return bool(re.match(r'^(?:请|帮我|麻烦)?\s*(?:以后|今后|从现在起|把你|修改你|修改自己|更新你|更新自己|升级你|调整你|调整自己|给自己加|改一下你|你以后|你今后|清除你|删掉你|重置你)', head)
                or re.match(r'^我(?:希望|想|要)\s*(?:你|让你).*(?:以后|今后|改|调整)', head))


def owner_for(config, config_path=None):
    """Prefer the verified creator; a sole paired private user covers legacy bots."""
    owner = (config.get('auto_group_owner_open_id')
             or (config.get('task_dispatcher') or {}).get('owner_open_id')
             or (config.get('assistant_manager') or {}).get('admin_open_id'))
    if owner:
        return owner
    if config_path:
        path = Path(config_path)
        root = path.parent.parent.parent if path.name == 'config.json' else path.parent
        ref = path.parent.name if path.name == 'config.json' else 'default'
        for job_path in (root / 'assistant-onboarding').glob('asst_*/job.json'):
            job = load(job_path)
            if job.get('app_ref') != ref or job.get('phase') != 'ready':
                continue
            credentials = load(job_path.parent / 'credentials.json')
            if credentials.get('app_id') == config.get('app_id') and credentials.get('owner_open_id'):
                return credentials['owner_open_id']
    allowed = config.get('allowed_open_ids') or []
    return allowed[0] if len(allowed) == 1 else None


def capability(config, message_id, sender_id):
    secret = config.get('connection_key') or ''
    if not secret:
        return ''
    payload = f"assistant-self-update-v1:{config.get('app_id')}:{message_id}:{sender_id}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def instruction(config, config_path, message_id, sender_id, chat_type, chat_id, user_text,
                has_media=False, sender_type='user'):
    """Offer group-only updates to human members, global updates to the owner."""
    if (config.get('assistant_manager') or sender_type != 'user'
            or (chat_type == 'p2p' and sender_id != owner_for(config, config_path))
            or chat_type not in ('p2p', 'group') or has_media or not explicit_request(user_text)):
        return ''
    token = capability(config, message_id, sender_id)
    if not token:
        return ''
    script = Path(__file__).resolve().parent / 'scripts' / 'update-assistant-self.py'
    scope = '当前群' if chat_type == 'group' else '全部新任务'
    return (f"\n\n[Bridge 可信助手更新入口；{'当前群成员' if chat_type == 'group' else '创建者'}可用] "
            "只有发言者明确要求改变你长期的用途、做事规则或回复风格时，"
            f"才运行 python3 {script} --source-id {message_id} --token {token} --rule '合并旧规则后的完整新规则'。"
            f"当前消息更新范围：{scope}。如需清除之前追加的规则，用 --clear 代替 --rule。"
            "先真正执行并读回成功结果，再告诉用户已更新；不要只口头答应。"
            "新规则不能改变飞书权限、工作文件夹、执行工具、其他助手或历史任务；这些须走现有设置。"
            "令牌仅用于本条消息，不要在回复中展示。")


def applied_rule(config, chat_id=None):
    global_rule = str(config.get('self_update_rule') or '').strip()
    group = (config.get('group_configs') or {}).get(chat_id, {}) if chat_id else {}
    group_rule = str(group.get('self_update_rule') or '').strip()
    parts = []
    if global_rule:
        parts.append('创建者为本助手更新的通用业务规则：\n' + global_rule)
    if group_rule:
        parts.append('当前群更新的业务规则：\n' + group_rule)
    return ('\n\n[Bridge 已保存的助手规则；以当前版本为准]\n' + '\n\n'.join(parts)) if parts else ''


def update(groups, data):
    if not isinstance(data, dict) or set(data) != {'source_id', 'token', 'rule', 'clear'}:
        raise BridgeError(400, 'invalid_request', '更新助手的请求无效')
    source_id, token, rule, clear = (data[k] for k in ('source_id', 'token', 'rule', 'clear'))
    if (not isinstance(source_id, str) or not re.fullmatch(r'om_[A-Za-z0-9_-]{3,128}', source_id)
            or not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{64}', token)
            or not isinstance(rule, str) or not isinstance(clear, bool)
            or (clear and rule) or (not clear and not 5 <= len(rule.strip()) <= MAX_RULE_LENGTH)):
        raise BridgeError(400, 'invalid_request', '请提供 5 到 1600 字的新规则，或清除已追加的规则')
    # The message ID is looked up in saved Feishu receipts, never accepted as an identity claim.
    with CONFIG_LOCK:
        matches = []
        from feishu_groups import application_paths
        from feishu_worker import Inbox
        for ref, path, inbox_path in application_paths(groups.config_path, groups.inbox_path):
            if not inbox_path.exists():
                continue
            item = Inbox(inbox_path).linked_source(source_id)
            if item and item['message_id'] == source_id:
                matches.append((ref, path, item))
        if len(matches) != 1:
            raise BridgeError(404, 'source_not_found', '找不到这条助手消息')
        ref, path, item = matches[0]
        config = load(path)
        route = json.loads(item.get('route_json') or '{}')
        if (not config.get('enabled') or config.get('assistant_archived') or config.get('account_archived')
                or config.get('assistant_manager') or item.get('app_id') != config.get('app_id')
                or route.get('mode') != 'prompt'
                or not explicit_request(item.get('text'))
                or not hmac.compare_digest(token, capability(config, source_id, item['sender_id']))):
            raise BridgeError(403, 'self_update_forbidden', '这条消息不能更新助手规则')
        if item.get('chat_type') == 'group':
            chat_id = item['chat_id']
            routes = {r['chat_id']: r for r in group_routes(config)}
            if chat_id not in routes or not routes[chat_id].get('enabled', True):
                raise BridgeError(409, 'group_unavailable', '这个群当前没有接入助手')
            # Old receipts predate sender_type. Preserve their owner-only behavior.
            if route.get('sender_type') != 'user' and not (
                    'sender_type' not in route and item['sender_id'] == owner_for(config, path)):
                raise BridgeError(403, 'self_update_forbidden', '只有当前群的成员能更新本群规则')
            scope = 'group'
        elif (item.get('chat_type') == 'p2p' and item['sender_id'] == owner_for(config, path)
              and item['sender_id'] in config.get('allowed_open_ids', [])):
            scope = 'all'
            chat_id = None
        else:
            raise BridgeError(403, 'self_update_forbidden', '当前会话不能更新助手')
        normalized = rule.strip().replace('{{message}}', '用户消息') if not clear else ''
        receipt = hashlib.sha256(json.dumps({'scope': scope, 'chat_id': chat_id, 'rule': normalized},
                                            ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        prior = (config.get('self_update_receipts') or {}).get(source_id)
        if prior:
            if prior != receipt:
                raise BridgeError(409, 'self_update_conflict', '这条消息已更新过规则，不能再次覆盖；请发送新消息')
            return {'updated': True, 'scope': scope, 'rule': normalized, 'already_applied': True}
        if scope == 'group':
            config.setdefault('group_configs', {}).setdefault(chat_id, {})['self_update_rule'] = normalized
        else:
            config['self_update_rule'] = normalized
        receipts = config.setdefault('self_update_receipts', {})
        receipts[source_id] = receipt
        if len(receipts) > 100:
            for old in list(receipts)[:-100]:
                del receipts[old]
        atomic_write(path, config)
        return {'updated': True, 'scope': scope, 'rule': normalized, 'already_applied': False}
