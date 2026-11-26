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


def _number(parser, value):
    try:
        number = int(value)
    except ValueError:
        parser.error('--replace 的第一个参数必须是规则编号')
    return number


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bridge-url', type=loopback_url, default='http://127.0.0.1:8797')
    parser.add_argument('--source-id', required=True)
    parser.add_argument('--token', required=True)
    # Shared assistants: itemized rules. Legacy assistants: --rule / --clear.
    parser.add_argument('--list', action='store_true', help='列出现有规则条目')
    parser.add_argument('--add', action='append', default=[], metavar='规则', help='新增一条规则，可重复')
    parser.add_argument('--replace', action='append', nargs=2, default=[], metavar=('编号', '规则'),
                        help='直接替换第 N 条，可重复')
    parser.add_argument('--remove', action='append', type=int, default=[], metavar='编号',
                        help='直接删除第 N 条，可重复')
    parser.add_argument('--propose', metavar='原因', help='把这些改动作为整理提议，请提出人确认后生效')
    parser.add_argument('--rule')
    parser.add_argument('--clear', action='store_true')
    args = parser.parse_args()
    ops = ([{'op': 'replace', 'id': _number(parser, n), 'text': t} for n, t in args.replace]
           + [{'op': 'remove', 'id': n, 'text': ''} for n in args.remove]
           + [{'op': 'add', 'text': t} for t in args.add])
    modes = [bool(args.list), bool(ops), args.rule is not None or args.clear]
    if sum(modes) != 1 or (args.rule is not None and args.clear) or (args.propose is not None and not ops):
        parser.error('请只选择一种：--list；--add/--replace/--remove；或整段规则 --rule/--clear')
    body = {'source_id': args.source_id, 'token': args.token}
    if args.list:
        body['list'] = True
    elif ops:
        body['ops'] = ops
        if args.propose is not None:
            body['propose'] = args.propose
    else:
        body.update(rule=args.rule or '', clear=args.clear)
    payload = json.dumps(body, ensure_ascii=False).encode()
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
        print('未收到 Bridge 保存结果。请使用同样参数重试核对。', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
