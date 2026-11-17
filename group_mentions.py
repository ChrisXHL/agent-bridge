"""Durable native group mentions; receivers run independently with their own rules."""
from __future__ import annotations
import hashlib
import html
import json
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


class GroupMentions:
    def __init__(self, collaboration):
        self.c = collaboration
        self.lock = threading.Lock()

    def binding(self, ident, chat):
        job, cfg, _ = self.c.ready(ident)
        routes = [r for r in group_routes(cfg) if r['chat_id'] == chat and r.get('enabled', True)]
        if not routes or not cfg.get('bot_open_id'):
            raise BridgeError(409, 'mention_not_in_group', '双方助手都必须启用并接入当前群')
        return job, cfg

    def chain(self, session, cfg):
        """Only our sent-message ledger authenticates a local mention chain."""
        job = self.c.onboarding.read(session['assistant_id'])
        _, _, inbox = self.c.groups.resolve(job['app_ref'])
        if inbox.exists():
            with closing(sqlite3.connect(inbox.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                mids = [r[0] for r in db.execute('SELECT message_id FROM inbound WHERE task_id=? AND app_id=?',
                                                (session['task_id'], cfg['app_id']))]
            with self.c.store.db() as db:
                for mid in mids:
                    previous = db.execute('SELECT ancestry,root_task FROM assistant_group_mentions WHERE outbound_id=? AND target_id=?',
                                          (mid, session['assistant_id'])).fetchone()
                    if previous:
                        return json.loads(previous['ancestry']), previous['root_task']
        return [session['assistant_id']], session['task_id']

    def send(self, session, data):
        if (not isinstance(data, dict) or set(data) - {'request_id', 'assistant_id', 'assistant_name', 'text'}
                or not {'request_id', 'text'} <= set(data)
                or ('assistant_id' in data) == ('assistant_name' in data)
                or not isinstance(data['request_id'], str) or not 8 <= len(data['request_id']) <= 100
                or not isinstance(data['text'], str) or not 1 <= len(data['text'].strip()) <= 4000):
            raise BridgeError(400, 'invalid_mention', '请提供接收助手、唯一请求编号和1到4000字的消息')
        with self.c.store.db() as db:
            child = db.execute('SELECT 1 FROM assistant_calls WHERE child_task=?', (session['task_id'],)).fetchone()
        if child:
            raise BridgeError(403, 'mention_child_forbidden', '内部子任务只返回委托结果，不能另行向群派发消息')
        chat = self.c.group_chat(session['task_id'])
        if not chat:
            raise BridgeError(409, 'mention_group_required', '只能在当前任务所在的群里@助手')
        peers = self.c.peers_for(session['assistant_id'], session['task_id'])
        field = 'assistant_id' if 'assistant_id' in data else 'name'
        value = data.get('assistant_id', data.get('assistant_name'))
        matches = [p for p in peers if p[field] == value]
        if len(matches) != 1:
            raise BridgeError(409, 'mention_target_ambiguous', '请指定本群唯一且已接入的助手')
        target = matches[0]['assistant_id']
        _, actor_cfg = self.binding(session['assistant_id'], chat)
        _, target_cfg = self.binding(target, chat)
        root, _, _ = self.c.lineage(session['task_id'])
        if target not in self.c.allowed(session['assistant_id'], root):
            raise BridgeError(403, 'mention_authorization_required', '当前协作策略尚未允许向这位助手派发消息，请先取得协作授权')
        ancestry, mention_root = self.chain(session, actor_cfg)
        if target in ancestry or target == session['assistant_id']:
            raise BridgeError(409, 'mention_cycle', '不能用群@循环派发给自己或此前发起助手')
        text = data['text'].strip()
        # User text cannot add hidden native @ tags or @all to the controlled payload.
        if '<at' in text.lower():
            raise BridgeError(400, 'invalid_mention', '消息正文不能包含额外的原生@标签')
        with self.c.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM assistant_group_mentions WHERE task_id=? AND request_id=?',
                             (session['task_id'], data['request_id'])).fetchone()
            if old:
                if (old['target_id'], old['text'], old['chat_id']) != (target, text, chat):
                    raise BridgeError(409, 'request_changed', '同一编号不能改换接收助手或消息内容')
                ident = old['id']
            else:
                ident = 'mention_' + uuid.uuid4().hex
                db.execute('''INSERT INTO assistant_group_mentions
                    (id,task_id,root_task,request_id,actor_id,target_id,chat_id,text,ancestry,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)''',
                    (ident, session['task_id'], mention_root, data['request_id'], session['assistant_id'], target, chat,
                     text, json.dumps(ancestry + [target]), time.time()))
        self.deliver(ident)
        with self.c.store.db() as db:
            row = db.execute('SELECT id,state,outbound_id FROM assistant_group_mentions WHERE id=?', (ident,)).fetchone()
        return {**dict(row), 'receiver_started': False,
                'notice': 'sent仅表示真实@消息已发群，不代表对方已处理；接收助手独立按自身规则处理并回群'}

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
                target_job, target = self.binding(row['target_id'], row['chat_id'])
                root, _, _ = self.c.lineage(row['task_id'])
                if row['target_id'] not in self.c.allowed(row['actor_id'], root):
                    raise BridgeError(403, 'mention_authorization_required', '协作授权已变化')
                sender = self.c.topics.sender_factory(actor['app_id'], actor['app_secret'])
                body = ('<at user_id="' + html.escape(target['bot_open_id'], quote=True) + '">'
                        + html.escape(target_job['name']) + '</at>\n' + row['text'])
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
