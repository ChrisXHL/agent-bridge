#!/usr/bin/env python3
"""Manage schedules through the verified source Feishu message."""
import argparse
import json
import os
import sys
from pathlib import Path
from urllib import error, request


def local_timezone():
    name = os.environ.get('TZ', '')
    if name:
        return name
    location = str(Path('/etc/localtime').resolve())
    return location.split('zoneinfo/', 1)[1] if 'zoneinfo/' in location else ''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-id', required=True)
    parser.add_argument('--token', required=True)
    parser.add_argument('--action', choices=('create', 'list', 'cancel'), required=True)
    parser.add_argument('--at', default='')
    parser.add_argument('--timezone', default=local_timezone())
    parser.add_argument('--repeat', choices=('once', 'daily', 'weekly', 'interval'), default='once')
    parser.add_argument('--every-minutes', type=int, default=0)
    parser.add_argument('--task', default='')
    parser.add_argument('--id', default='')
    args = parser.parse_args()
    payload = json.dumps({'source_id': args.source_id, 'token': args.token, 'action': args.action,
                          'at': args.at, 'timezone': args.timezone, 'repeat': args.repeat,
                          'task': args.task, 'id': args.id,
                          'interval_minutes': args.every_minutes}, ensure_ascii=False).encode()
    req = request.Request('http://127.0.0.1:8797/api/feishu/assistant-schedules', data=payload,
                          headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with request.urlopen(req, timeout=15) as response:
            result = json.load(response)
    except error.HTTPError as exc:
        try:
            message = json.load(exc).get('error', {}).get('message') or '定时操作失败'
        except (ValueError, AttributeError):
            message = '定时操作失败'
        print(message, file=sys.stderr)
        return 1
    except OSError:
        print('无法连接本机 Bridge，定时操作未完成。', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
