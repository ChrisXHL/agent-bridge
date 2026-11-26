"""Durable native group mentions; receivers run independently with their own rules."""
from __future__ import annotations
import hashlib
import html
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from codex_http_bridge import BridgeError
from feishu_groups import group_routes


def install(db):
    db.execute('''CREATE TABLE IF NOT EXISTS assistant_group_mentions (
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL, root_task TEXT NOT NULL, request_id TEXT NOT NULL,
        actor_id TEXT NOT NULL, target_id TEXT NOT NULL, chat_id TEXT NOT NULL,
        text TEXT NOT NULL, ancestry TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
        outbound_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL,
        UNIQUE(task_id,request_id))''')
    if 'targets_json' not in {r[1] for r in db.execute('PRAGMA table_info(assistant_group_mentions)')}:
        db.execute('ALTER TABLE assistant_group_mentions ADD COLUMN targets_json TEXT')


MAX_HOPS = 12  # real safeguard: bot-to-bot @ chains cannot ping-pong forever
TARGET_KEYS = {'assistant_id', 'assistant_name', 'names', 'open_ids', 'all'}
AT_TAG = re.compile(r'<at\s+(?:user_id|id)\s*=\s*"?([^"\s>]+)"?\s*>', re.I)


class GroupMentions:
    """Assistants may @ anyone in the group: Bridge assistants, other bots, members, or everyone."""
    def __init__(self, collaboration):
        self.c = collaboration
        self.lock = threading.Lock()

    def binding(self, ident, chat):
        job, cfg, _ = self.c.ready(ident)
        routes = [r for r in group_routes(cfg) if r['chat_id'] == chat and r.get('enabled', True)]
        if not routes or not cfg.get('bot_open_id'):
            raise BridgeError(409, 'mention_not_in_group', '发送方助手必须启用并接入当前群')
        return job, cfg

    def assistants(self):
        """Every ready Bridge assistant with a bot identity, not only authorized peers."""
        found = []
        for path in self.c.onboarding.root.glob('asst_*/job.json'):
            try:
                job, cfg, _ = self.c.ready(json.loads(path.read_text())['id'])
            except (BridgeError, OSError, ValueError, KeyError):
                continue
            if cfg.get('bot_open_id'):
                found.append({'assistant_id': job['id'], 'name': job['name'], 'open_id': cfg['bot_open_id']})
        return found

    def members(self, cfg, chat):
        sender = self.c.topics.sender_factory(cfg['app_id'], cfg['app_secret'])
        if not hasattr(sender, 'list_chat_members'):
            return []
        try:
            return sender.list_chat_members(chat)
        except Exception:
            return []

    def resolve(self, session, data, actor_cfg, chat):
        targets = []
        def add(open_id, name, assistant_id=None):
            if open_id and all(t['open_id'] != open_id for t in targets):
                targets.append({'open_id': open_id, 'name': name or open_id, 'assistant_id': assistant_id})
        if data.get('all'):
            add('all', '所有人')
        local = self.assistants()
        if 'assistant_id' in data:
            hit = [a for a in local if a['assistant_id'] == data['assistant_id']]
            if not hit:
                raise BridgeError(409, 'mention_target_unknown', '没有这个助手ID；可改用 --name 或 --open-id')
            add(hit[0]['open_id'], hit[0]['name'], hit[0]['assistant_id'])
        names = list(data.get('names') or []) + ([data['assistant_name']] if 'assistant_name' in data else [])
        people = None
        for name in names:
            name = name.strip().lstrip('@')
            hit = [a for a in local if a['name'] == name]
            if len(hit) == 1:
                add(hit[0]['open_id'], name, hit[0]['assistant_id']);continue
            if people is None:
                people = self.members(actor_cfg, chat)
            exact = [m for m in people if m['name'] == name] or hit
            if len(exact) != 1:
                exact = exact or [m for m in people if name and name in m['name']]
            if len(exact) != 1:
                choices = '、'.join(sorted({a['name'] for a in local} | {m['name'] for m in people}))[:1500]
                raise BridgeError(409, 'mention_target_ambiguous' if exact else 'mention_target_unknown',
                                  f'无法唯一确定“{name}”。可用 --open-id 直接指定；已知名称：{choices or "无（机器人可能缺少获取群成员权限 im:chat.members:read）"}')
            m = exact[0]
            add(m.get('open_id'), m['name'], m.get('assistant_id'))
        for open_id in data.get('open_ids') or []:
            hit = next((a for a in local if a['open_id'] == open_id), None)
            add(open_id, hit['name'] if hit else None, hit['assistant_id'] if hit else None)
        return targets

    def chain(self, session, cfg):
        """This task's chain of assistant hops, authenticated only by our sent-message ledger.

        The latest message that fed this task decides: an @ we sent continues that
        branch (sender chain + this assistant); a person's message starts afresh.
        """
        me = session['assistant_id']
        job = self.c.onboarding.read(me)
        _, _, inbox = self.c.groups.resolve(job['app_ref'])
        if inbox.exists():
            with closing(sqlite3.connect(inbox.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                latest = db.execute('SELECT message_id FROM inbound WHERE task_id=? AND app_id=? '
                                    'ORDER BY created_at DESC, rowid DESC LIMIT 1',
                                    (session['task_id'], cfg['app_id'])).fetchone()
            if latest:
                with self.c.store.db() as db:
                    previous = db.execute('SELECT ancestry,root_task,targets_json FROM assistant_group_mentions '
                                          'WHERE outbound_id=?', (latest[0],)).fetchone()
                if previous:
                    ancestry = json.loads(previous['ancestry'])
                    if previous['targets_json'] is None:  # legacy rows already end with the receiver
                        return ancestry, previous['root_task']
                    return ancestry + [me], previous['root_task']
        return [me], session['task_id']

    def receives(self, ident, chat):
        """Whether a Bridge assistant's receiver actually picks up @s in this chat."""
        try:
            _, cfg, _ = self.c.ready(ident)
        except BridgeError:
            return False
        if cfg.get('assistant_manager') or cfg.get('account_archived') or not cfg.get('bot_open_id'):
            return False
        return any(r['chat_id'] == chat and r.get('enabled', True) for r in group_routes(cfg))

    def send(self, session, data):
        if (not isinstance(data, dict) or set(data) - TARGET_KEYS - {'request_id', 'text'}
                or not {'request_id', 'text'} <= set(data)
                or not isinstance(data['request_id'], str) or not 8 <= len(data['request_id']) <= 100
                or not isinstance(data['text'], str) or not 1 <= len(data['text'].strip()) <= 4000
                or any(not isinstance(data[k], str) for k in ('assistant_id', 'assistant_name') if k in data)
                or any(not isinstance(data.get(k, []), list) or len(data.get(k, [])) > 50
                       or any(not isinstance(v, str) or not 1 <= len(v) <= 180 for v in data.get(k, []))
                       for k in ('names', 'open_ids'))):
            raise BridgeError(400, 'invalid_mention', '请提供唯一请求编号、1到4000字的消息，以及要@的对象（名称、open_id 或所有人）')
        with self.c.store.db() as db:
            child = db.execute('SELECT 1 FROM assistant_calls WHERE child_task=?', (session['task_id'],)).fetchone()
        if child:
            raise BridgeError(403, 'mention_child_forbidden', '内部子任务只返回委托结果，不能另行向群派发消息')
        chat = self.c.group_chat(session['task_id'])
        if not chat:
            raise BridgeError(409, 'mention_group_required', '只能在当前任务所在的群里@')
        _, actor_cfg = self.binding(session['assistant_id'], chat)
        text = data['text'].strip()
        targets = self.resolve(session, data, actor_cfg, chat)
        targets = [t for t in targets if t['open_id'] != actor_cfg['bot_open_id']]
        if not targets and not AT_TAG.search(text):
            raise BridgeError(400, 'invalid_mention', '请至少指定一个要@的对象（不能只@自己）')
        ancestry, mention_root = self.chain(session, actor_cfg)
        if len(ancestry) >= MAX_HOPS:
            raise BridgeError(409, 'mention_chain_too_long', f'助手之间连续互相@已达{MAX_HOPS}轮，请停下直接回复群里的人')
        primary = next((t['assistant_id'] for t in targets if t['assistant_id']), None) or (targets[0]['open_id'] if targets else 'inline')
        encoded = json.dumps(targets, ensure_ascii=False, sort_keys=True)
        with self.c.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM assistant_group_mentions WHERE task_id=? AND request_id=?',
                             (session['task_id'], data['request_id'])).fetchone()
            if old:
                if (old['text'], old['chat_id'], old['targets_json'] or encoded) != (text, chat, encoded):
                    raise BridgeError(409, 'request_changed', '同一编号不能改换@对象或消息内容')
                ident = old['id']
            else:
                ident = 'mention_' + uuid.uuid4().hex
                db.execute('''INSERT INTO assistant_group_mentions
                    (id,task_id,root_task,request_id,actor_id,target_id,chat_id,text,ancestry,created_at,targets_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                    (ident, session['task_id'], mention_root, data['request_id'], session['assistant_id'], primary, chat,
                     text, json.dumps(ancestry),
                     time.time(), encoded))
        self.deliver(ident)
        with self.c.store.db() as db:
            row = db.execute('SELECT id,state,outbound_id FROM assistant_group_mentions WHERE id=?', (ident,)).fetchone()
        recipients = []
        for t in targets:
            if t['open_id'] == 'all':
                kind, note = 'everyone', '所有人会收到提醒；其他助手不会因@所有人自动接单'
            elif t['assistant_id'] and self.receives(t['assistant_id'], chat):
                kind, note = 'assistant', '会自动接单，按自己的规则处理并回群'
            elif t['assistant_id']:
                kind, note = 'assistant_not_listening', '未接入本群或已停用，看得到@但不会自动接单；需要它干活请改用内部委托call、换一位助手，或告诉用户先把它接入本群'
            else:
                kind, note = 'member_or_external', '群成员或非Bridge机器人，是否回应由对方决定'
            recipients.append({'name': t['name'], 'kind': kind, 'note': note})
        return {**dict(row), 'receiver_started': False, 'mentioned': [t['name'] for t in targets],
                'recipients': recipients,
                'notice': 'sent仅表示真实@消息已发群，不代表对方已处理；逐个查看 recipients 判断对方是否会自动接单'}

    def deliver(self, ident=None):
        with self.lock:
            self._deliver(ident)

    def _deliver(self, ident=None):
        with self.c.store.db() as db:
            rows = [dict(r) for r in db.execute('''SELECT * FROM assistant_group_mentions
                WHERE state='pending' AND next_attempt<=? ORDER BY created_at LIMIT 12''', (time.time(),))]
        for row in rows:
            if ident and row['id'] != ident:continue
            try:
                if time.time() - row['created_at'] > 3600:
                    with self.c.store.db() as db:
                        db.execute("UPDATE assistant_group_mentions SET state='uncertain' WHERE id=?", (row['id'],))
                    continue
                if self.c.store.task(row['task_id'])['status'] == '已停止':
                    raise BridgeError(409, 'mention_stopped', '原任务已停止')
                _, actor = self.binding(row['actor_id'], row['chat_id'])
                targets = json.loads(row.get('targets_json') or 'null')
                if targets is None:  # rows queued before open mentions: a single Bridge assistant
                    target_job, target = self.c.ready(row['target_id'])[:2]
                    targets = [{'open_id': target['bot_open_id'], 'name': target_job['name']}]
                sender = self.c.topics.sender_factory(actor['app_id'], actor['app_secret'])
                tags = ''.join('<at user_id="' + html.escape(t['open_id'], quote=True) + '">'
                               + html.escape(t['name']) + '</at>' for t in targets)
                body = tags + '\n' + row['text'] if tags else row['text']
                outgoing = sender.send_group(row['chat_id'], row['id'], body)
                if not outgoing:raise RuntimeError('missing_message_id')
                with self.c.store.db() as db:
                    db.execute("UPDATE assistant_group_mentions SET state='sent',outbound_id=? WHERE id=?",
                               (outgoing, row['id']))
            except BridgeError:
                with self.c.store.db() as db:
                    db.execute("UPDATE assistant_group_mentions SET state='cancelled' WHERE id=?", (row['id'],))
            except Exception:
                attempts = row['attempts'] + 1
                with self.c.store.db() as db:
                    db.execute('UPDATE assistant_group_mentions SET state=?,attempts=?,next_attempt=? WHERE id=?',
                               ('uncertain' if attempts >= 5 else 'pending', attempts,
                                time.time() + min(300, 5 * 2 ** attempts), row['id']))
