"""Read-only task source labels from saved receipts, not connection-name guesses."""
from __future__ import annotations
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def labels(store):
    from feishu_groups import application_paths, group_routes
    from feishu_settings import load

    root = Path(store.db_path).parent
    config_path = getattr(store, 'feishu_label_config', root / 'feishu.json')
    inbox_path = getattr(store, 'feishu_label_inbox', root / 'feishu-inbox.sqlite3')
    result = {}
    for _, path, inbox in application_paths(config_path, inbox_path):
        if not inbox.is_file():
            continue
        try:
            config = load(path)
            assistant = str(config.get('app_name') or config.get('bot_name') or '').strip()
            if not assistant:
                continue
            groups = {r['chat_id']: r.get('name') for r in group_routes(config)}
            with closing(sqlite3.connect(inbox.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                db.row_factory = sqlite3.Row
                columns = {r[1] for r in db.execute('PRAGMA table_info(inbound)')}
                if not {'task_id', 'chat_id', 'chat_type', 'app_id', 'connection_id'} <= columns:
                    continue
                rows = db.execute('''SELECT task_id,chat_id,chat_type,connection_id FROM inbound
                    WHERE task_id IS NOT NULL AND app_id=? ORDER BY created_at,rowid''',
                    (config.get('app_id'),)).fetchall()
            for row in rows:
                if row['task_id'] in result:
                    continue
                if row['chat_type'] == 'group':
                    group = groups.get(row['chat_id']) or '群聊（名称未记录）'
                    label = f'{group}（{assistant}）'
                elif row['chat_type'] == 'p2p':
                    group = None
                    label = assistant
                else:
                    continue
                result[row['task_id']] = {'source_label': label, 'assistant_name': assistant,
                                          'chat_type': row['chat_type'], 'group_name': group,
                                          '_connection_id': row['connection_id']}
        except (OSError, ValueError, sqlite3.Error):
            # A broken historical inbox must not prevent the task panel loading.
            continue
    with store.db() as db:
        connections = {r['id']: r['connection_id'] for r in db.execute('SELECT id,connection_id FROM tasks')}
        calls = [dict(r) for r in db.execute('SELECT child_task,parent_task,target_name FROM assistant_calls ORDER BY created_at')]
    result = {tid: value for tid, value in result.items()
              if value['_connection_id'] == connections.get(tid)}
    # Internal tasks inherit only their verified root's chat, retaining their own assistant name.
    pending = calls
    for _ in range(3):
        remaining = []
        for call in pending:
            origin = result.get(call['parent_task'])
            if not origin:
                remaining.append(call)
                continue
            assistant = call['target_name']
            group = origin['group_name']
            result.setdefault(call['child_task'], {
                'source_label': f'{group}（{assistant}）' if group else assistant,
                'assistant_name': assistant, 'chat_type': origin['chat_type'],
                'group_name': group, '_connection_id': connections.get(call['child_task'])})
        pending = remaining
    return {tid: {k: v for k, v in value.items() if not k.startswith('_')} for tid, value in result.items()}
