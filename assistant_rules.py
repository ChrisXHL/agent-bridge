"""Itemized shared assistant rules: one entry per rule, with author and history.

The legacy ``self_update_rule`` paragraph stays as it was. New "记住" requests
are stored as numbered items next to it, so a save never rewrites unrelated
rules. Original authors and subsequent editors are recorded for audit.
"""
from __future__ import annotations

import copy
import re
import time

from codex_http_bridge import BridgeError

MAX_OPS_PER_CALL = 10
MAX_ITEM_LENGTH = 2000


def items(config):
    return [i for i in (config.get('rule_items') or []) if isinstance(i, dict) and i.get('text')]


def _key(text):
    """Exact-duplicate key; semantic overlap is judged by the model before saving."""
    return re.sub(r'[\s。．.！!；;，,、]+', '', text)


def render_items(config):
    return '\n'.join(f"{i['id']}. {i['text']}" for i in items(config))


def rules_text(config):
    """Legacy paragraph plus itemized rules, for display, export and migration."""
    legacy = str(config.get('self_update_rule') or '').strip()
    listed = render_items(config)
    return '\n\n'.join(p for p in (legacy, ('逐条保存的规则：\n' + listed) if listed else '') if p)


def listing(config, viewer):
    return [{'id': i['id'], 'text': i['text'], 'mine': i.get('author') == viewer,
             'source': '群聊' if i.get('chat_type') == 'group' else '私聊',
             'updated_at': i.get('updated_at')} for i in items(config)]


def parse_ops(ops):
    if not isinstance(ops, list) or not 1 <= len(ops) <= MAX_OPS_PER_CALL:
        raise BridgeError(400, 'invalid_request', f'一次请提供 1 到 {MAX_OPS_PER_CALL} 项规则操作')
    parsed = []
    for op in ops:
        kind = op.get('op') if isinstance(op, dict) else None
        text = op.get('text', '') if isinstance(op, dict) else None
        rule_id = op.get('id') if isinstance(op, dict) else None
        if kind not in ('add', 'replace', 'remove') or not isinstance(text, str):
            raise BridgeError(400, 'invalid_request', '规则操作只能是新增、替换或删除')
        text = text.strip().replace('{{message}}', '用户消息')
        if kind in ('add', 'replace') and not 5 <= len(text) <= MAX_ITEM_LENGTH:
            raise BridgeError(400, 'invalid_request', f'每条规则需为 5 到 {MAX_ITEM_LENGTH} 字')
        if kind in ('replace', 'remove') and (type(rule_id) is not int or rule_id < 1):
            raise BridgeError(400, 'invalid_request', '替换或删除需要提供规则编号')
        if kind == 'remove' and text:
            raise BridgeError(400, 'invalid_request', '删除规则不需要提供内容')
        parsed.append({'op': kind, 'id': rule_id if kind != 'add' else None, 'text': text})
    return parsed


def apply(config, ops, *, sender_id, chat_id, chat_type, source_id, authors=(), direct=False):
    """Apply an authorized direct edit or a proposal whose authors agreed."""
    rows = copy.deepcopy(items(config))
    removed = copy.deepcopy(config.get('rule_items_removed') or [])
    next_id = max([int(config.get('rule_next_id') or 1)] + [r['id'] + 1 for r in rows])
    now = time.time()
    results = []
    for op in ops:
        if op['op'] == 'add':
            same = next((r for r in rows if _key(r['text']) == _key(op['text'])), None)
            if same:
                results.append({'op': 'add', 'duplicate_of': same['id']})
                continue
            rows.append({'id': next_id, 'text': op['text'], 'author': sender_id,
                         'chat_type': chat_type, 'chat_id': chat_id, 'source_id': source_id,
                         'created_at': now, 'updated_at': now, 'history': []})
            results.append({'op': 'add', 'id': next_id})
            next_id += 1
            continue
        row = next((r for r in rows if r['id'] == op['id']), None)
        if not row:
            raise BridgeError(404, 'rule_not_found', f"没有第 {op['id']} 条规则")
        if not direct and row.get('author') != sender_id and row.get('author') not in authors:
            raise BridgeError(403, 'rule_owned_by_other',
                              f"第 {op['id']} 条是其他人提出的规则，不能直接修改或删除。"
                              '请照常新增当前要求，再用 --propose 发起整理提议请提出人确认（冲突的是第 %d 条）。' % op['id'])
        if op['op'] == 'replace':
            if row['text'] == op['text']:
                results.append({'op': 'replace', 'id': row['id'], 'unchanged': True})
                continue
            row['history'] = (row.get('history') or []) + [
                {'text': row['text'], 'source_id': row.get('source_id'), 'at': row.get('updated_at'),
                 'updated_by': row.get('updated_by') or row.get('author')}]
            row.update(text=op['text'], source_id=source_id, updated_at=now, updated_by=sender_id)
            results.append({'op': 'replace', 'id': row['id']})
        else:
            rows.remove(row)
            removed.append({**row, 'removed_at': now, 'removed_by': sender_id, 'removed_source_id': source_id})
            results.append({'op': 'remove', 'id': row['id']})
    config.update(rule_items=rows, rule_items_removed=removed, rule_next_id=next_id)
    return results


def archive_all(config, reason):
    """Move every item to the removed archive, e.g. when a template replaces all rules."""
    now = time.time()
    config['rule_items_removed'] = (config.get('rule_items_removed') or []) + [
        {**r, 'removed_at': now, 'removed_by': reason} for r in items(config)]
    config['rule_items'] = []
    return config
