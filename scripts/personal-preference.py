#!/usr/bin/env python3
"""Read or update only the verified speaker's response preferences."""
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
    parser.add_argument('--action', choices=('list', 'set', 'delete', 'clear'), required=True)
    parser.add_argument('--key', choices=('language', 'verbosity', 'format', 'conclusion_first', 'source_links'))
    parser.add_argument('--value')
    args = parser.parse_args()
    if args.action == 'set' and (args.key is None or args.value is None):
        parser.error('set 需要 --key 和 --value')
    if args.action == 'delete' and (args.key is None or args.value is not None):
        parser.error('delete 仅需要 --key')
    if args.action in ('list', 'clear') and (args.key is not None or args.value is not None):
        parser.error('list/clear 不接受 --key 或 --value')
    data = {'source_id': args.source_id, 'token': args.token, 'action': args.action}
    if args.key is not None:data['key'] = args.key
    if args.value is not None:data['value'] = args.value
    req = request.Request(args.bridge_url + '/api/feishu/personal-preferences',
                          data=json.dumps(data, ensure_ascii=False).encode(),
                          headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with request.urlopen(req, timeout=15) as response:
            result = json.load(response)
    except error.HTTPError as exc:
        try:
            failure = json.load(exc).get('error', {})
            code, message = failure.get('code', 'preference_failed'), failure.get('message', '偏好操作失败')
        except (ValueError, AttributeError):
            code, message = 'preference_failed', '偏好操作失败'
        print(json.dumps({'error': {'code': code, 'message': message}}, ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print('无法核实本机偏好操作结果，请勿声称已经记住。', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
