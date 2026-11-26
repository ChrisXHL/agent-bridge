#!/usr/bin/env python3
"""Save a long-term rule (记住…) for a platform bot after Bridge verifies the source message."""
import argparse
import json
import re
import sys
from urllib import error, request
from urllib.parse import urlsplit


def loopback_url(value):
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'
                 and parsed.port is not None and not parsed.username and not parsed.password
                 and parsed.path in ('', '/') and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError('Bridge 地址必须是带端口的本机 http://127.0.0.1 地址')
    return value.rstrip('/')


def platform_id(value):
    if not re.fullmatch(r'[a-z][a-z0-9_]{1,23}', value):
        raise argparse.ArgumentTypeError('平台 ID 无效')
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bridge-url', type=loopback_url, default='http://127.0.0.1:8797')
    parser.add_argument('--platform', type=platform_id, required=True)
    parser.add_argument('--source-id', required=True)
    parser.add_argument('--token', required=True)
    parser.add_argument('--rule', required=True, help='更新后的完整业务 Prompt')
    args = parser.parse_args()
    payload = json.dumps({'source_id': args.source_id, 'token': args.token, 'rule': args.rule},
                         ensure_ascii=False).encode()
    req = request.Request('%s/api/platforms/%s/self-update' % (args.bridge_url, args.platform), data=payload,
                          headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with request.urlopen(req, timeout=15) as response:
            result = json.load(response)
    except error.HTTPError as exc:
        try:
            failure = json.load(exc).get('error', {})
        except (ValueError, AttributeError):
            failure = {}
        print(json.dumps({'error': {'code': failure.get('code') or 'update_failed',
                                    'message': failure.get('message') or '更新失败'}}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    except OSError:
        print('无法连接本机 Bridge，规则未更新。', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
