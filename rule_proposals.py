"""Rule consolidation proposals: ask the people whose rules would change, apply only on yes.

Lifecycle: an assistant (during a verified rule-save task) or the local legacy-split
script creates a proposal in the app's own inbox database; the receiver sends it
to the original group with @ of the affected authors, or privately to the creator;
the asked people answer by quoting it; Bridge applies the operations only when
every asked person agreed and the affected rules have not changed meanwhile.
No answer keeps everything as it is.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing

from assistant_rules import apply as apply_rules, items, parse_ops
from codex_http_bridge import BridgeError

PROPOSAL_TTL_SECONDS = 24 * 60 * 60
COOLDOWN_SECONDS = 24 * 60 * 60
MAX_IGNORED = 2
# Only a suggestion trigger for the assistant; nothing is ever refused or cut.
CONSOLIDATE_CHARS = 1200
CONSOLIDATE_ITEMS = 12

YES = ('同意', '可以', '好', '好的', '行', '确认', '要', '没问题', '赞成', 'ok', 'OK', 'yes')
NO = ('不同意', '不要', '不行', '不用', '算了', '拒绝', '保持原样', '不', 'no')


def install(db):
    db.execute('''CREATE TABLE IF NOT EXISTS rule_proposals (
        id TEXT PRIMARY KEY, number INTEGER NOT NULL, app_id TEXT NOT NULL, kind TEXT NOT NULL,
        chat_type TEXT NOT NULL, chat_id TEXT NOT NULL, ask_json TEXT NOT NULL, ops_json TEXT NOT NULL,
        snapshot_json TEXT NOT NULL, reason TEXT NOT NULL, fingerprint TEXT NOT NULL,
        proposer TEXT, source_id TEXT, created_at REAL NOT NULL, sent_at REAL, expires_at REAL,
        state TEXT NOT NULL, outbound_id TEXT, answers_json TEXT NOT NULL DEFAULT '{}',
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0)''')
    db.execute('''CREATE TABLE IF NOT EXISTS rule_proposal_answers (
        message_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, state TEXT NOT NULL,
        reply TEXT, reply_id TEXT, attempts INTEGER NOT NULL DEFAULT 0)''')


def _db(inbox_path):
    db = sqlite3.connect(str(inbox_path), timeout=10)
    db.row_factory = sqlite3.Row
    install(db)
    return db


def needs_consolidation(config):
    rows = items(config)
    return len(rows) >= CONSOLIDATE_ITEMS or sum(len(r['text']) for r in rows) >= CONSOLIDATE_CHARS


def _fingerprint(app_id, ops):
    return hashlib.sha256(json.dumps([app_id, ops], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _destination(config, ops, proposer, owner):
    """Who must agree, and where to ask them."""
    touched = [r for r in items(config) if r['id'] in {o['id'] for o in ops if o['op'] in ('replace', 'remove')}]
    if any(o['op'] == 'clear_legacy' for o in ops):
        return 'p2p', owner, [owner]
    authors = sorted({r.get('author') for r in touched} - {proposer} - {None})
    if not authors:
        raise BridgeError(409, 'proposal_not_needed',
                          '这些改动只涉及发言者本人的规则或只是新增，直接用 --add/--replace/--remove 保存即可')
    chats = {(r.get('chat_type'), r.get('chat_id')) for r in touched if r.get('author') in authors}
    if len(chats) == 1:
        chat_type, chat_id = next(iter(chats))
        if chat_type == 'group' and chat_id in config.get('allowed_chat_ids', []):
            return 'group', chat_id, authors
    # Private-chat rules, rules from several chats, or a group no longer connected:
    # the creator decides privately.
    return 'p2p', owner, [owner]


def create(config, inbox_path, *, ops, reason, kind, proposer, source_id, owner, now=None):
    now = now or time.time()
    if not owner:
        raise BridgeError(409, 'owner_unknown', '找不到这位助手的创建者，无法发起确认')
    if not isinstance(reason, str) or not 2 <= len(reason.strip()) <= 500:
        raise BridgeError(400, 'invalid_request', '请用一两句话说明整理原因（2 到 500 字）')
    if kind == 'legacy_split':
        adds = parse_ops([o for o in ops if o.get('op') != 'clear_legacy'])
        if any(o['op'] != 'add' for o in adds) or not str(config.get('self_update_rule') or '').strip():
            raise BridgeError(400, 'invalid_request', '旧规则拆分只能清空旧段落并新增条目')
        ops = [{'op': 'clear_legacy', 'id': None, 'text': ''}] + adds
    else:
        ops = parse_ops(ops)
        known = {r['id'] for r in items(config)}
        missing = [o['id'] for o in ops if o['op'] != 'add' and o['id'] not in known]
        if missing:
            raise BridgeError(404, 'rule_not_found', f'没有第 {missing[0]} 条规则')
    chat_type, chat_id, ask = _destination(config, ops, proposer, owner)
    fingerprint = _fingerprint(config['app_id'], ops)
    with closing(_db(inbox_path)) as db, db:
        history = db.execute('SELECT state FROM rule_proposals WHERE app_id=? AND fingerprint=?',
                             (config['app_id'], fingerprint)).fetchall()
        states = [h['state'] for h in history]
        if 'rejected' in states:
            raise BridgeError(409, 'proposal_rejected_before', '同样的整理提议之前被拒绝过，不再重复询问')
        if states.count('expired') >= MAX_IGNORED:
            raise BridgeError(409, 'proposal_ignored_before', '同样的整理提议已两次无人回复，不再重复询问')
        if any(s in ('pending', 'sent') for s in states):
            raise BridgeError(409, 'proposal_pending', '同样的整理提议正在等待回复')
        recent = db.execute('SELECT 1 FROM rule_proposals WHERE app_id=? AND chat_type=? AND chat_id=? '
                            'AND created_at>? LIMIT 1',
                            (config['app_id'], chat_type, chat_id, now - COOLDOWN_SECONDS)).fetchone()
        if recent and kind != 'legacy_split':
            raise BridgeError(429, 'proposal_cooldown', '这里 24 小时内已经发过整理提议，先不再打扰')
        number = (db.execute('SELECT COALESCE(MAX(number),0)+1 FROM rule_proposals WHERE app_id=?',
                             (config['app_id'],)).fetchone()[0])
        snapshot = {'items': {str(r['id']): r['text'] for r in items(config)},
                    'legacy': str(config.get('self_update_rule') or '')}
        ident = 'rp_' + uuid.uuid4().hex[:20]
        db.execute('INSERT INTO rule_proposals (id,number,app_id,kind,chat_type,chat_id,ask_json,ops_json,'
                   'snapshot_json,reason,fingerprint,proposer,source_id,created_at,state) '
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending')",
                   (ident, number, config['app_id'], kind, chat_type, chat_id, json.dumps(ask),
                    json.dumps(ops, ensure_ascii=False), json.dumps(snapshot, ensure_ascii=False),
                    reason.strip(), fingerprint, proposer, source_id, now))
    return {'proposal': number, 'ask': len(ask), 'where': '原群' if chat_type == 'group' else '创建者私聊'}


def message(config, row):
    """The proposal text; group proposals @ every asked author."""
    ops = json.loads(row['ops_json'])
    current = {r['id']: r['text'] for r in items(config)}
    lines = []
    for op in ops:
        if op['op'] == 'clear_legacy':
            lines.append('把原来的整段规则拆成下面这些条目（原段落内容不丢，只是换成逐条保存）：')
        elif op['op'] == 'add':
            lines.append('新增：' + op['text'])
        elif op['op'] == 'replace':
            lines.append(f"第 {op['id']} 条「{current.get(op['id'], '')}」改为：{op['text']}")
        else:
            lines.append(f"删除第 {op['id']} 条「{current.get(op['id'], '')}」")
    ask = json.loads(row['ask_json'])
    head = ''
    if row['chat_type'] == 'group':
        head = ' '.join(f'<at user_id="{html.escape(a, quote=True)}"></at>' for a in ask) + '\n'
    return (head + f"规则整理提议 #P{row['number']}\n原因：{row['reason']}\n\n" + '\n'.join(lines)
            + '\n\n请引用这条消息回复「同意」或「不同意」。'
            + ('需要被 @ 的每个人都同意才会生效；' if row['chat_type'] == 'group' and len(ask) > 1 else '')
            + '24 小时内没有回复就保持原样。')


def due(inbox_path, app_id, now=None):
    """Proposals to send now; expires ones nobody answered."""
    now = now or time.time()
    with closing(_db(inbox_path)) as db, db:
        db.execute("UPDATE rule_proposals SET state='expired' WHERE app_id=? AND state='sent' AND expires_at<?",
                   (app_id, now))
        return [dict(r) for r in db.execute(
            "SELECT * FROM rule_proposals WHERE app_id=? AND state='pending' AND next_attempt<=? ORDER BY created_at",
            (app_id, now))]


def mark_sent(inbox_path, ident, outbound_id, now=None):
    now = now or time.time()
    with closing(_db(inbox_path)) as db, db:
        db.execute("UPDATE rule_proposals SET state='sent',outbound_id=?,sent_at=?,expires_at=? "
                   "WHERE id=? AND state='pending'", (outbound_id, now, now + PROPOSAL_TTL_SECONDS, ident))


def mark_send_failed(inbox_path, ident):
    with closing(_db(inbox_path)) as db, db:
        row = db.execute('SELECT attempts FROM rule_proposals WHERE id=?', (ident,)).fetchone()
        attempts = (row['attempts'] if row else 0) + 1
        db.execute("UPDATE rule_proposals SET attempts=?,next_attempt=?,state=? WHERE id=? AND state='pending'",
                   (attempts, time.time() + min(600, 15 * 2 ** attempts),
                    'failed' if attempts >= 6 else 'pending', ident))


def _decision(text):
    text = re.sub(r'#P\d+|提议|整理|规则|这条|这个|[\s，,。.！!～~、]', '', text or '')
    if text in NO or text.startswith('不同意'):
        return 'no'
    if text in YES or text.startswith('同意'):
        return 'yes'
    return 'unclear'


def match_answer(inbox_path, app_id, *, chat_type, chat_id, sender_id, text, parent_id, root_id):
    """Return (proposal_id, decision) when this message answers a proposal asking this sender."""
    with closing(_db(inbox_path)) as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM rule_proposals WHERE app_id=? AND state='sent' AND chat_type=?", (app_id, chat_type))]
    rows = [r for r in rows if sender_id in json.loads(r['ask_json'])
            and (r['chat_id'] == chat_id if chat_type == 'group' else r['chat_id'] == sender_id)]
    quoted = next((r for r in rows if r['outbound_id'] and r['outbound_id'] in (parent_id, root_id)), None)
    if quoted:
        return quoted['id'], _decision(text)
    # Unquoted answers must name the proposal, so an ordinary "同意" to some
    # other question is never taken as consent.
    numbers = re.findall(r'#P(\d+)', text or '')
    named = [r for r in rows if str(r['number']) in numbers]
    if len(named) == 1:
        return named[0]['id'], _decision(text)
    if '提议' in (text or '') and len(rows) == 1 and _decision(text) != 'unclear':
        return rows[0]['id'], _decision(text)
    return None


def record_answer(inbox_path, message_id, proposal_id):
    with closing(_db(inbox_path)) as db, db:
        db.execute("INSERT OR IGNORE INTO rule_proposal_answers (message_id,proposal_id,state) VALUES (?,?,'pending')",
                   (message_id, proposal_id))


def pending_answers(inbox_path):
    with closing(_db(inbox_path)) as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM rule_proposal_answers WHERE state IN ('pending','reply') AND attempts<8")]


def set_answer(inbox_path, message_id, **fields):
    with closing(_db(inbox_path)) as db, db:
        sets = ','.join(f'{k}=?' for k in fields)
        db.execute(f'UPDATE rule_proposal_answers SET {sets} WHERE message_id=?', (*fields.values(), message_id))


def answer(config, inbox_path, item, proposal_id, decision):
    """Service side: record one asked person's answer, apply when everyone agreed.

    The caller holds CONFIG_LOCK and writes ``config`` back when ``changed``.
    Returns (reply text, changed).
    """
    with closing(_db(inbox_path)) as db, db:
        row = db.execute('SELECT * FROM rule_proposals WHERE id=? AND app_id=?',
                         (proposal_id, config['app_id'])).fetchone()
        if not row:
            raise BridgeError(404, 'proposal_not_found', '找不到这条整理提议')
        row = dict(row)
        ask = json.loads(row['ask_json'])
        place_ok = (item.get('chat_type') == row['chat_type']
                    and (item['chat_id'] == row['chat_id'] if row['chat_type'] == 'group'
                         else item['sender_id'] == row['chat_id']))
        if item['sender_id'] not in ask or not place_ok:
            raise BridgeError(403, 'proposal_not_yours', '这条整理提议不需要你确认')
        label = f"#P{row['number']}"
        if row['state'] == 'expired' or (row['state'] == 'sent' and row['expires_at'] < time.time()):
            db.execute("UPDATE rule_proposals SET state='expired' WHERE id=?", (proposal_id,))
            return f'整理提议 {label} 已过期，规则保持原样。', False
        if row['state'] != 'sent':
            done = {'accepted': '已经生效', 'rejected': '已经被拒绝', 'stale': '已经作废'}.get(row['state'], '已结束')
            return f'整理提议 {label} {done}，不需要再回复。', False
        if decision == 'unclear':
            return f'没看懂对整理提议 {label} 的回复。请引用提议回复「同意」或「不同意」。', False
        answers = json.loads(row['answers_json'])
        answers[item['sender_id']] = decision
        if decision == 'no':
            db.execute("UPDATE rule_proposals SET state='rejected',answers_json=? WHERE id=?",
                       (json.dumps(answers), proposal_id))
            return f'好的，整理提议 {label} 不执行，规则保持原样，之后也不会再提同样的整理。', False
        waiting = [a for a in ask if answers.get(a) != 'yes']
        if waiting:
            db.execute('UPDATE rule_proposals SET answers_json=? WHERE id=?', (json.dumps(answers), proposal_id))
            return f'已记下你同意整理提议 {label}，还在等其他 {len(waiting)} 人确认。', False
        snapshot = json.loads(row['snapshot_json'])
        ops = json.loads(row['ops_json'])
        current = {str(r['id']): r['text'] for r in items(config)}
        touched = [str(o['id']) for o in ops if o['op'] in ('replace', 'remove')]
        legacy_changed = (any(o['op'] == 'clear_legacy' for o in ops)
                          and str(config.get('self_update_rule') or '') != snapshot['legacy'])
        if legacy_changed or any(current.get(t) != snapshot['items'].get(t) for t in touched):
            db.execute("UPDATE rule_proposals SET state='stale',answers_json=? WHERE id=?",
                       (json.dumps(answers), proposal_id))
            return f'整理提议 {label} 涉及的规则在这期间有变化，这条提议作废，规则保持现状。', False
        authors = set(ask) | {row['proposer']}
        apply_ops = [o for o in ops if o['op'] != 'clear_legacy']
        if any(o['op'] == 'clear_legacy' for o in ops):
            history = config.get('legacy_rule_history') or []
            config['legacy_rule_history'] = history + [{'text': config.get('self_update_rule', ''),
                                                        'cleared_at': time.time(), 'proposal': proposal_id}]
            config['self_update_rule'] = ''
        results = apply_rules(config, apply_ops, sender_id=row['proposer'] or ask[0], chat_type=row['chat_type'],
                              chat_id=row['chat_id'] if row['chat_type'] == 'group' else None,
                              source_id=row['source_id'] or proposal_id, authors=authors) if apply_ops else []
        db.execute("UPDATE rule_proposals SET state='accepted',answers_json=? WHERE id=?",
                   (json.dumps(answers), proposal_id))
        added = [r['id'] for r in results if r.get('id') and r['op'] == 'add']
        return (f'整理提议 {label} 已生效' + (f'，新条目编号：{"、".join(map(str, added))}' if added else '')
                + '。下一条新消息开始使用。'), True
