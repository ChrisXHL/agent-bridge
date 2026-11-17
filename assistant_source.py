"""Resolve an exact Feishu receipt without treating message IDs as app identities."""
from __future__ import annotations

import hmac
import sqlite3
from contextlib import closing
from pathlib import Path

from codex_http_bridge import BridgeError
from feishu_settings import load


def resolve_source(groups, source_id, token, capability, forbidden_code, forbidden_message):
    """Select the uniquely signed app receipt; never initialize or migrate inboxes."""
    from feishu_groups import application_paths

    found = False
    matches = []
    seen = set()
    for ref, path, inbox_path in application_paths(groups.config_path, groups.inbox_path):
        inbox_path = Path(inbox_path)
        if not inbox_path.is_file():
            continue
        identity = (Path(path).resolve(), inbox_path.resolve())
        if identity in seen:
            continue
        seen.add(identity)
        try:
            with closing(sqlite3.connect(inbox_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)) as db:
                db.row_factory = sqlite3.Row
                if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inbound'").fetchone():
                    continue
                row = db.execute('SELECT * FROM inbound WHERE message_id=?', (source_id,)).fetchone()
        except sqlite3.Error as exc:
            raise BridgeError(503, 'source_unavailable', '暂时无法核对助手来源消息，请稍后重试') from exc
        if row is None:
            continue
        found = True
        item = dict(row)
        config = load(path)
        expected = capability(config, source_id, item['sender_id'])
        if (item.get('app_id') == config.get('app_id') and expected
                and hmac.compare_digest(token, expected)):
            matches.append((ref, Path(path), inbox_path, item, config))
    if not found:
        raise BridgeError(404, 'source_not_found', '找不到这条助手消息，请使用当前消息的入口')
    if not matches:
        raise BridgeError(403, forbidden_code, forbidden_message)
    if len(matches) != 1:
        raise BridgeError(409, 'source_ambiguous', '来源消息对应多个相同身份的助手配置，请检查重复接入')
    return matches[0]
