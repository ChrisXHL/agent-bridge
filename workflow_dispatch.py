"""Read trusted workflow receipts before dispatching a queued stage."""
import hashlib
import sqlite3
from pathlib import Path


class WorkflowDispatch:
    def __init__(self, path):
        self.path = Path(path).resolve()

    def snapshot(self):
        if not self.path.exists():
            return {}, False, {}, {}
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='workflow_runs'").fetchone():
                return {}, False, {}, {}
            stale = {}
            held = {}
            states = {}
            live = False
            for run in db.execute('SELECT * FROM workflow_runs'):
                live |= run['state'] in ('running', 'waiting_review', 'checking_review', 'blocked')
                source = db.execute('SELECT task_id,connection_id FROM inbound WHERE message_id=?', (run['source_id'],)).fetchone()
                if (source and source['task_id'] and source['task_id'] == run['task_id']
                        and source['connection_id'] == run['connection_id']):
                    states[(run['connection_id'], run['task_id'])] = (run['state'], run['error'])
                # Only earlier epochs are superseded. A current blocked stage,
                # a normal message, or an uncertain send is never silently retried.
                for epoch in range(1, run['epoch'] + 1):
                    mid = run['source_id'] if epoch == 1 else f"wf_{run['id']}_{epoch}"
                    item = db.execute('SELECT * FROM inbound WHERE message_id=?', (mid,)).fetchone()
                    if not item or not item['task_id'] or item['task_id'] != run['task_id']:
                        continue
                    request_id = 'fs_' + hashlib.sha256((run['app_id'] + ':' + mid).encode()).hexdigest()[:48]
                    key = (run['connection_id'], item['task_id'], request_id)
                    if epoch < run['epoch'] or run['state'] in ('cancelled', 'completed'):
                        stale[key] = run['id']
                    elif run['state'] != 'running':
                        held[key] = run['error'] or '当前工作流阶段正在等待审核或恢复。'
            return stale, live, held, states
        finally:
            db.close()
