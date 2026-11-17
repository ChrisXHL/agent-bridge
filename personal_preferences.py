"""Small, enum-only reply preferences scoped to an app and a human sender."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import shlex
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from assistant_source import resolve_source
from assistant_self_update import request_text
from codex_http_bridge import BridgeError
from feishu_settings import CONFIG_LOCK


VALUES = {
    'language': ('zh', 'en', 'auto'),
    'verbosity': ('concise', 'normal', 'detailed'),
    'format': ('bullets', 'table', 'paragraph', 'auto'),
    'conclusion_first': ('true', 'false'),
    'source_links': ('true', 'false'),
}
AUTH_TTL_SECONDS = 24 * 60 * 60

# Full-message grammar, not keyword extraction: quoted material, mixed business
# instructions, other people's preferences, and temporary requests grant nothing.
_UNSAFE = re.compile(r'["\'“”‘’「」『』`<>]|(?:^|\n)\s*>|密码|口令|密钥|凭证|token|secret|api[_ -]?key|'
                     r'这次|本次|这一次|暂时|仅此|不要|不用|不必|不想|别记|取消记住|'
                     r'他说|她说|他说过|她说过|别人|其他人|所有人|全群|本群|群里|群规则', re.I)
_PREFIX = re.compile(r'^(?:(?:请|帮我|麻烦你?)\s*)?(?:'
                     r'(?:以后|今后|从现在起)(?:你)?|'
                     r'记住(?:我(?:的)?(?:个人)?(?:回复)?偏好|这个偏好|我的习惯)[：:,，]?|'
                     r'我(?:习惯|喜欢|偏好)(?:你)?|我希望你以后)[：:,，]?\s*')
_RULES = (
    ('language', 'zh', r'(?:用|使用)?中文(?:回答|回复|交流)?'),
    ('language', 'en', r'(?:用|使用)?英文(?:回答|回复|交流)?'),
    ('language', 'auto', r'(?:回答|回复)?语言(?:跟随|随)(?:我的)?(?:提问|问题)|语言自动'),
    ('verbosity', 'concise', r'(?:(?:回答|回复)(?:尽量)?)?(?:简短|简洁|精简)(?:一[些点]|些|点)?'),
    ('verbosity', 'normal', r'(?:(?:回答|回复)(?:长度)?)?(?:适中|正常长度)'),
    ('verbosity', 'detailed', r'(?:(?:回答|回复)(?:尽量)?)?(?:详细|详尽)(?:一[些点]|些|点)?'),
    ('format', 'table', r'(?:用|使用)?表格(?:回答|回复|呈现|展示|格式)?'),
    ('format', 'bullets', r'(?:用|使用)?(?:列表|分点|要点)(?:回答|回复|呈现|展示|格式)?'),
    ('format', 'paragraph', r'(?:用|使用)?(?:段落|自然段)(?:回答|回复|呈现|展示|格式)?'),
    ('format', 'auto', r'(?:回复)?格式(?:按内容选择|自动选择|自动)'),
    ('conclusion_first', 'true', r'(?:(?:回答|回复)?先(?:给|说|写|讲)?(?:我)?结论|结论(?:优先|放前面)|先给出结论)'),
    ('conclusion_first', 'false', r'(?:结论放后面|正文先于结论|先分析后结论)'),
    ('source_links', 'true', r'(?:附(?:上|带)?|提供|保留)(?:来源|出处)(?:链接)?'),
    ('source_links', 'false', r'(?:来源|出处)链接按需提供'),
)
_KEY_NAMES = {
    'language': r'(?:语言|中文|英文)',
    'verbosity': r'(?:回复长度|回答长度|详细程度|简短|简洁|详尽|详细)',
    'format': r'(?:回复格式|回答格式|格式|表格|列表|分点|段落)',
    'conclusion_first': r'(?:结论顺序|先结论|结论优先)',
    'source_links': r'(?:来源链接|出处链接|来源|出处)',
}


def _text(text):
    if not isinstance(text, str) or not 2 <= len(text.strip()) <= 600:
        return ''
    text = text.strip()
    if _UNSAFE.search(text):
        return ''
    return text.rstrip('。！!').strip()


def candidates(text):
    """Return enum values only for an explicit, entirely personal preference request."""
    text = _text(text)
    prefix = _PREFIX.match(text) if text else None
    if not prefix:
        return {}
    body = text[prefix.end():]
    body = re.sub(r'^都', '', body)
    body = re.sub(r'^(?:看|使用)(?=表格)', '', body)
    body = re.sub(r'(?<=(?:回答|回复))我$', '', body)
    body = re.sub(r'(?<=[短洁])(?=先)', '，', body)
    clauses = re.split(r'[，,、；;\n]|\s*(?:并且|而且|以及)\s*', body)
    result = {}
    for clause in clauses:
        clause = clause.strip()
        matches = [(key, value) for key, value, pattern in _RULES if re.fullmatch(pattern, clause)]
        if len(matches) != 1:
            return {}
        key, value = matches[0]
        if key in result and result[key] != value:
            return {}
        result[key] = value
    return result


def _intent(text):
    text = _text(text)
    if re.fullmatch(r'(?:请)?(?:查看|列出|显示)(?:一下)?我的(?:个人)?(?:回复)?偏好|'
                    r'我有哪些(?:个人)?偏好|你记住了我哪些偏好', text):
        return 'list', None
    if re.fullmatch(r'(?:请)?(?:清除|清空|删除|忘记)(?:我的)?(?:全部|所有)?(?:个人)?(?:回复)?偏好', text):
        return 'clear', None
    for key, names in _KEY_NAMES.items():
        if re.fullmatch(r'(?:请)?(?:清除|删除|忘记)(?:我的)?' + names + r'(?:偏好|习惯)', text):
            return 'delete', key
    return None, None


def capability(config, message_id, sender_id):
    secret = config.get('connection_key') or ''
    if not secret:
        return ''
    payload = f"personal-preference-v1:{config.get('app_id')}:{message_id}:{sender_id}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def install(db):
    db.execute('''CREATE TABLE IF NOT EXISTS personal_preferences (
        app_id TEXT NOT NULL, user_open_id TEXT NOT NULL, key TEXT NOT NULL,
        value TEXT NOT NULL, source_id TEXT NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY(app_id,user_open_id,key))''')
    db.execute('''CREATE TABLE IF NOT EXISTS personal_preference_receipts (
        app_id TEXT NOT NULL, user_open_id TEXT NOT NULL, source_id TEXT NOT NULL,
        key TEXT NOT NULL, action TEXT NOT NULL, payload_hash TEXT NOT NULL,
        source_created_at REAL NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY(app_id,user_open_id,source_id,key))''')


def _route(item):
    try:
        route = json.loads(item.get('route_json') or '{}')
        return route if isinstance(route, dict) else {}
    except (ValueError, TypeError):
        return {}


def _authorized(config, db, item):
    route = _route(item)
    if (not isinstance(config, dict) or not config.get('enabled') or not config.get('app_id')
            or config.get('assistant_archived') or config.get('account_archived')
            or config.get('assistant_manager') or item.get('app_id') != config['app_id']
            or route.get('sender_type') != 'user' or route.get('mode') != 'prompt'
            or route.get('scheduled') or route.get('bot_mention') or route.get('child_task')
            or route.get('parent_task') or route.get('delegated') or route.get('assistant_call')
            or not item.get('sender_id') or not re.fullmatch(r'om_[A-Za-z0-9_-]{3,128}', item.get('message_id', ''))):
        return False
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inbound_media'").fetchone():
        if db.execute('SELECT 1 FROM inbound_media WHERE message_id=?', (item['message_id'],)).fetchone():
            return False
    if item.get('chat_type') == 'group':
        return (item.get('chat_id') in config.get('allowed_chat_ids', [])
                and (config.get('group_configs') or {}).get(item['chat_id'], {}).get('enabled', True))
    return (item.get('chat_type') == 'p2p'
            and item['sender_id'] in config.get('allowed_open_ids', []))


def _preferences(db, app_id, sender_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='personal_preferences'").fetchone():
        return {}
    return {row['key']: row['value'] for row in db.execute(
        'SELECT key,value FROM personal_preferences WHERE app_id=? AND user_open_id=? ORDER BY key',
        (app_id, sender_id)) if row['key'] in VALUES and row['value'] in VALUES[row['key']]}


def _fresh(item):
    created = item.get('created_at')
    return (isinstance(created, (int, float)) and not isinstance(created, bool)
            and math.isfinite(created) and 0 <= time.time() - created <= AUTH_TTL_SECONDS)


def manage(groups, data):
    if (not isinstance(data, dict) or not {'source_id', 'token', 'action'} <= set(data)
            or set(data) - {'source_id', 'token', 'action', 'key', 'value'}):
        raise BridgeError(400, 'invalid_request', '个人偏好请求格式无效')
    source_id, token, action = (data[k] for k in ('source_id', 'token', 'action'))
    key, value = data.get('key'), data.get('value')
    if (not isinstance(source_id, str) or not re.fullmatch(r'om_[A-Za-z0-9_-]{3,128}', source_id)
            or not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{64}', token)
            or not isinstance(action, str) or action not in ('list', 'set', 'delete', 'clear')):
        raise BridgeError(400, 'invalid_request', '个人偏好请求无效')
    if (action == 'set' and (not isinstance(key, str) or key not in VALUES
                            or not isinstance(value, str) or value not in VALUES[key])
            or action == 'delete' and (not isinstance(key, str) or key not in VALUES or value is not None)
            or action in ('list', 'clear') and (key is not None or value is not None)):
        raise BridgeError(400, 'invalid_preference', '仅支持限定的语言、长度、格式、结论顺序和来源链接偏好')
    with CONFIG_LOCK:
        _, _, inbox_path, item, config = resolve_source(groups, source_id, token, capability,
            'preference_forbidden', '这条消息不能管理个人偏好')
        with closing(sqlite3.connect(str(inbox_path), timeout=10)) as db:
            db.row_factory = sqlite3.Row
            with db:
                db.execute('BEGIN IMMEDIATE')
                if not _authorized(config, db, item):
                    raise BridgeError(403, 'preference_forbidden', '这条消息不能管理个人偏好')
                text = request_text(item, _route(item))
                intent, intent_key = _intent(text)
                if (action == 'set' and candidates(text).get(key) != value
                        or action == 'delete' and (intent != 'delete' or intent_key != key)
                        or action in ('list', 'clear') and intent != action):
                    raise BridgeError(403, 'preference_not_requested', '请由本人明确说明要保存、查看或忘记的个人偏好')
                if action == 'list':
                    if item['chat_type'] != 'p2p':
                        raise BridgeError(403, 'preference_private_only', '为保护个人隐私，请私聊本助手查看你的偏好')
                    if not _fresh(item):
                        raise BridgeError(403, 'preference_expired', '个人偏好入口已过期，请发送新消息')
                    return {'preferences': _preferences(db, config['app_id'], item['sender_id'])}
                install(db)
                app, user = config['app_id'], item['sender_id']
                slot = '*' if action == 'clear' else key
                payload_hash = hashlib.sha256(json.dumps({'action': action, 'key': key, 'value': value},
                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                prior = db.execute('''SELECT payload_hash FROM personal_preference_receipts
                    WHERE app_id=? AND user_open_id=? AND source_id=? AND key=?''',
                    (app, user, source_id, slot)).fetchone()
                if prior:
                    if prior['payload_hash'] != payload_hash:
                        raise BridgeError(409, 'preference_conflict', '这条消息已处理过该偏好，请发新消息修改')
                    return {'updated': True, 'action': action, 'already_applied': True}
                # A single message cannot clear and then repopulate its preferences.
                conflict = db.execute('''SELECT 1 FROM personal_preference_receipts WHERE app_id=?
                    AND user_open_id=? AND source_id=? AND (key='*' OR ?='*')''',
                    (app, user, source_id, slot)).fetchone()
                if conflict:
                    raise BridgeError(409, 'preference_conflict', '这条消息已处理过个人偏好，请发新消息修改')
                if not _fresh(item):
                    raise BridgeError(403, 'preference_expired', '个人偏好入口已过期，请发送新消息')
                newer = db.execute('''SELECT 1 FROM personal_preference_receipts WHERE app_id=?
                    AND user_open_id=? AND (key=? OR key='*' OR ?='*') AND source_created_at>? LIMIT 1''',
                    (app, user, slot, slot, item['created_at'])).fetchone()
                if newer:
                    raise BridgeError(409, 'preference_stale', '已有更新的个人偏好请求，旧消息不能覆盖，请发送新消息')
                now = time.time()
                if action == 'set':
                    db.execute('''INSERT INTO personal_preferences VALUES (?,?,?,?,?,?)
                        ON CONFLICT(app_id,user_open_id,key) DO UPDATE SET value=excluded.value,
                        source_id=excluded.source_id,updated_at=excluded.updated_at''',
                        (app, user, key, value, source_id, now))
                elif action == 'delete':
                    db.execute('DELETE FROM personal_preferences WHERE app_id=? AND user_open_id=? AND key=?',
                               (app, user, key))
                else:
                    db.execute('DELETE FROM personal_preferences WHERE app_id=? AND user_open_id=?', (app, user))
                db.execute('INSERT INTO personal_preference_receipts VALUES (?,?,?,?,?,?,?,?)',
                           (app, user, source_id, slot, action, payload_hash, item['created_at'], now))
                return {'updated': True, 'action': action, 'already_applied': False}


def context(config, inbox, item, bridge_url='http://127.0.0.1:8797'):
    """Read a real stored human receipt and its current enum preferences at dispatch."""
    if not isinstance(config, dict) or not isinstance(item, dict) or not item.get('message_id'):
        return ''
    with closing(sqlite3.connect(str(inbox.path), timeout=10)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM inbound WHERE message_id=?', (item['message_id'],)).fetchone()
        if not row:
            return ''
        saved = dict(row)
        if not _authorized(config, db, saved):
            return ''
        preferences = _preferences(db, config['app_id'], saved['sender_id'])
    text = request_text(saved, _route(saved))
    values = candidates(text)
    intent, key = _intent(text)
    lines = ['\n\n[Bridge 个人回复偏好；仅用于回应本条发言人，不适用于全群]',
             '以下是本条发言人的完整当前快照（空对象表示无已保存偏好），取代前文任何个人偏好快照。'
             '当前明确要求及业务规则优先；仅调整回复风格，不授予权限、不改变任务、其他成员或群规则。'
             '不要在群里列出个人偏好，也不要推断或保存身份、秘密等自由文本；清除偏好不代表删除历史聊天。',
             '当前限定偏好：' + json.dumps(preferences, ensure_ascii=False, sort_keys=True)]
    if intent == 'list' and saved['chat_type'] == 'group':
        lines.append('本条请求查看个人偏好：请提示本人私聊本助手查看，不在群里列出。')
    elif values or intent:
        if not _fresh(saved):
            lines.append('本条偏好操作入口已过期，若需修改或查看请本人发送新消息；不要使用旧入口。')
        else:
            token = capability(config, saved['message_id'], saved['sender_id'])
            if token:
                script = Path(__file__).resolve().parent / 'scripts' / 'personal-preference.py'
                base = ['python3', str(script), '--source-id', saved['message_id'], '--token', token,
                        '--bridge-url', bridge_url]
                commands = []
                for candidate_key, candidate_value in values.items():
                    commands.append(shlex.join(base + ['--action', 'set', '--key', candidate_key,
                                                       '--value', candidate_value]))
                if intent:
                    commands.append(shlex.join(base + ['--action', intent] + (['--key', key] if key else [])))
                lines.append('本人明确请求的操作如下；实际执行并读回成功后才确认已保存/忘记，不能口头冒充。'
                             '令牌只用于本条消息，不展示给用户。\n' + '\n'.join(commands))
    return '\n'.join(lines)
