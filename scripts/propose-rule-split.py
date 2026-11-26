#!/usr/bin/env python3
"""Ask an assistant's creator (privately) to replace its old rule paragraph with items.

The agent reads the paragraph (--show), writes the split as a JSON list of rule
strings, and creates the proposal here. Nothing changes until the creator
answers 「同意」 by quoting the proposal; the old paragraph is kept in history.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant_prompt import is_shared  # noqa: E402
from assistant_self_update import owner_for  # noqa: E402
from codex_http_bridge import BridgeError  # noqa: E402
from feishu_settings import load  # noqa: E402
from rule_proposals import create  # noqa: E402

DEFAULT_ROOT = Path.home() / 'Library/Application Support/CodexBridgeV2/feishu-apps'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app-ref', required=True, help='feishu-apps 下的目录名，如 app_xxx')
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--show', action='store_true', help='只显示旧段落和创建者，不创建提议')
    parser.add_argument('--items', type=Path, help='JSON 文件：拆分后的规则字符串列表')
    parser.add_argument('--reason', default='把原来的整段规则拆成逐条保存，以后可以单独修改、合并，不再整段重写。')
    args = parser.parse_args()
    path = args.root / args.app_ref / 'config.json'
    config = load(path)
    owner = owner_for(config, path, verified_only=True)
    legacy = str(config.get('self_update_rule') or '').strip()
    if args.show or not args.items:
        print(json.dumps({'shared': is_shared(config), 'owner_known': bool(owner),
                          'legacy_rule': legacy}, ensure_ascii=False, indent=1))
        return 0
    if not is_shared(config):
        print('这位助手仍使用旧版独立规则，不做拆分。', file=sys.stderr)
        return 1
    rules = json.loads(args.items.read_text())
    if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules):
        print('--items 需要是规则字符串列表', file=sys.stderr)
        return 1
    try:
        result = create(config, path.parent / 'inbox.sqlite3', kind='legacy_split', reason=args.reason,
                        ops=[{'op': 'add', 'text': r} for r in rules], proposer=owner, source_id=None,
                        owner=owner)
    except BridgeError as exc:
        print(json.dumps({'error': {'code': exc.code, 'message': str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
