"""Read-only view of pre-prompt tasks; never migrate or replay their records."""
import json
import sqlite3
from pathlib import Path


class LegacyHistory:
    def __init__(self, path):
        self.path = Path(path).resolve()

    def tasks(self):
        if not self.path.exists():
            return {}
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            result = {}
            if 'inbound' in tables:
                columns = {r[1] for r in db.execute('PRAGMA table_info(inbound)')}
                for r in db.execute('SELECT * FROM inbound WHERE task_id IS NOT NULL'):
                    try:
                        prompt = 'route_json' in columns and json.loads(r['route_json'] or '{}').get('mode') == 'prompt'
                    except (ValueError, AttributeError):
                        prompt = False
                    if not prompt:
                        result[r['task_id']] = {'archived': True}
            if 'workflow_runs' in tables:
                for r in db.execute('SELECT task_id,state,definition FROM workflow_runs WHERE task_id IS NOT NULL'):
                    result[r['task_id']] = {
                        'archived': True,
                        'archive_name': json.loads(r['definition']).get('name', '历史任务'),
                        'display_status': {'completed': '已完成', 'cancelled': '已停止'}.get(r['state'], '历史记录'),
                    }
            return result
        finally:
            db.close()
