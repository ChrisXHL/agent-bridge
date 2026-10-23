#!/usr/bin/env python3
"""Scoped client for the collaboration context injected by Bridge. No Feishu credentials."""
import argparse
import json
import sys
import time
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError


def main():
    p=argparse.ArgumentParser();p.add_argument('--context',required=True)
    sub=p.add_subparsers(dest='action',required=True);sub.add_parser('list')
    call=sub.add_parser('call');call.add_argument('--request-id',required=True);call.add_argument('--file',required=True)
    wait=sub.add_parser('wait');wait.add_argument('--batch',required=True);wait.add_argument('--seconds',type=int,default=60,choices=range(0,61),metavar='0..60')
    for action in ('say','ask'):
        command=sub.add_parser(action);command.add_argument('--request-id',required=True);command.add_argument('--text',required=True)
    answer=sub.add_parser('answer');answer.add_argument('--id',required=True);answer.add_argument('--text',required=True)
    question=sub.add_parser('question');question.add_argument('--id',required=True);question.add_argument('--seconds',type=int,default=60,choices=range(0,61),metavar='0..60')
    sub.add_parser('questions')
    a=p.parse_args();context=json.loads(Path(a.context).read_text())
    def request(path,data=None):
        req=Request(context['base_url']+path,data=None if data is None else json.dumps(data).encode(),headers={'Authorization':'Bearer '+context['token'],'Content-Type':'application/json'})
        with urlopen(req,timeout=15) as r:return json.load(r)
    try:
        if a.action=='list':out=request('/v1/assistant-peers')
        elif a.action=='call':out=request('/v1/assistant-calls',{'request_id':a.request_id,'requests':json.loads(Path(a.file).read_text())})
        elif a.action=='say':out=request('/v1/assistant-discussions',{'request_id':a.request_id,'text':a.text})
        elif a.action=='ask':out=request('/v1/assistant-questions',{'request_id':a.request_id,'question':a.text})
        elif a.action=='answer':out=request('/v1/assistant-answers',{'question_id':a.id,'answer':a.text})
        elif a.action=='questions':out=request('/v1/assistant-questions')
        else:
            deadline=time.monotonic()+a.seconds
            while True:
                out=request('/v1/assistant-calls/'+a.batch if a.action=='wait' else '/v1/assistant-questions/'+a.id)
                if (a.action=='wait' and (not out['pending'] or out.get('questions'))) or (a.action=='question' and out['answer'] is not None) or time.monotonic()>=deadline:break
                time.sleep(min(2,max(0,deadline-time.monotonic())))
        print(json.dumps(out,ensure_ascii=False))
    except HTTPError as e:
        print(e.read().decode(),file=sys.stderr);return 1
    except (OSError,ValueError,URLError):
        print('无法完成本次协作请求。请检查 Bridge 是否运行；重试调用时保留原请求编号。',file=sys.stderr);return 1
    return 0

if __name__=='__main__':sys.exit(main())
