#!/usr/bin/env python3
"""Update the current Feishu assistant after Bridge verifies the source message."""
import argparse
import json
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bridge-url', type=loopback_url, default='http://127.0.0.1:8797')
    parser.add_argument('--source-id', required=True)
    parser.add_argument('--token', required=True)
    rule = parser.add_mutually_exclusive_group(required=True)
    rule.add_argument('--rule')
    rule.add_argument('--clear', action='store_true')
    args = parser.parse_args()
    payload = json.dumps({'source_id': args.source_id, 'token': args.token,
                          'rule': args.rule or '', 'clear': args.clear}, ensure_ascii=False).encode()
    req = request.Request(args.bridge_url + '/api/feishu/assistant-self-update', data=payload,
                          headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with request.urlopen(req, timeout=15) as response:
            result = json.load(response)
    except error.HTTPError as exc:
        code = 'update_failed'
        try:
            failure = json.load(exc).get('error', {})
            message = failure.get('message') or '更新失败'
            code = failure.get('code') or code
        except (ValueError, AttributeError):
            message = '更新失败'
        print(json.dumps({'error': {'code': code, 'message': message}}, ensure_ascii=False), file=sys.stderr)
        return 1
    except OSError:
        print('无法连接本机 Bridge，规则未更新。', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
