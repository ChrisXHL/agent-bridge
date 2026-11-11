#!/usr/bin/env python3
"""Feishu long-connection receiver for the isolated Bridge 2.0 instance."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib import request
from urllib.parse import urlsplit

from codex_http_bridge import BridgeError
from feishu_settings import DEFAULT_PROMPT_TEMPLATE, load, render_prompt
from feishu_media import MediaError, MediaReceiver, install as install_media, parse as parse_media
from assistant_consent import parse as parse_consent

LOG = logging.getLogger("codex_bridge.feishu")
NOTIFY_STATUSES = {"已完成", "失败", "已停止", "需要你处理"}


def result_message(status, result, error_code=None):
    """Keep internal task identifiers in Bridge, not in chat replies."""
    result = str(result or '').strip()[:3500]
    if status == '已完成':
        return result or '这次没有返回可发送的结果，请在 Bridge 面板查看详情。'
    if status == '需要你处理' and error_code == 'delivery_uncertain':
        return '消息交接暂未核实，系统正在检查原任务。你不需要重复确认，也不会因此重复执行。'
    heading = {'失败': '这次没能完成。', '已停止': '这次已停止。',
               '需要你处理': ('需要你确认具体操作。' if error_code == 'approval_required'
                             else '需要你确认一下。' if not error_code else '任务暂时无法继续。')}[status]
    return heading + ('\n' + result if result else '')


def prompt_source(item):
    try:
        return json.loads(item.get('route_json') or '{}').get('mode') == 'prompt'
    except (ValueError, TypeError, AttributeError):
        return False


class Inbox:
    """Durable receipt and Feishu-message-to-Bridge-task mapping."""

    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS inbound (
                message_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL,
                sender_id TEXT NOT NULL, text TEXT NOT NULL, prompt TEXT,
                parent_id TEXT, root_id TEXT, task_id TEXT,
                notified_status TEXT, outbound_id TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
            if "prompt" not in {row["name"] for row in db.execute("PRAGMA table_info(inbound)")}:
                db.execute("ALTER TABLE inbound ADD COLUMN prompt TEXT")
            db.execute("""CREATE TABLE IF NOT EXISTS pairing (
                message_id TEXT PRIMARY KEY, code TEXT NOT NULL,
                sender_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                chat_type TEXT NOT NULL, created_at REAL NOT NULL)""")
            columns={row['name'] for row in db.execute('PRAGMA table_info(inbound)')}
            for column in ('connection_id','app_id','route_json','chat_type'):
                if column not in columns:
                    db.execute('ALTER TABLE inbound ADD COLUMN '+column+' TEXT')
            for column,kind in (('ack_status','TEXT'),('ack_id','TEXT'),('ack_last_attempt','REAL')):
                if column not in columns:
                    db.execute('ALTER TABLE inbound ADD COLUMN '+column+' '+kind)
            install_media(db)
            db.execute('''CREATE TABLE IF NOT EXISTS private_submissions (
                message_id TEXT PRIMARY KEY, payload TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS bot_message_cursors (
                chat_id TEXT PRIMARY KEY, last_created_ms INTEGER NOT NULL)''')
        Path(path).chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def accept(self, item):
        with self.db() as db:
            result = db.execute(
                """INSERT OR IGNORE INTO inbound
                   (message_id,chat_id,sender_id,text,prompt,parent_id,root_id,created_at,updated_at,connection_id,app_id,route_json,chat_type,ack_status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item["message_id"], item["chat_id"], item["sender_id"], item["text"], item["prompt"],
                 item.get("parent_id"), item.get("root_id"), time.time(), time.time(),
                 item.get('connection_id'),item.get('app_id'),item.get('route_json'),item.get('chat_type'),
                 'pending' if item.get('ack') else None),
            )
            if result.rowcount and item.get('media') is not None:
                media=item['media']
                db.execute('INSERT INTO inbound_media(message_id,message_type,resources,state,error) VALUES (?,?,?,?,?)',
                           (item['message_id'],media['type'],json.dumps(media.get('resources',[]),ensure_ascii=False),'failed' if media.get('error') else 'pending',media.get('error')))
        return result.rowcount == 1

    def bot_message_cursor(self, chat_id):
        """Start watching a group at the present time, never replay its old history on install."""
        with self.db() as db:
            row = db.execute('SELECT last_created_ms FROM bot_message_cursors WHERE chat_id=?', (chat_id,)).fetchone()
            if row:
                return row['last_created_ms']
            db.execute('INSERT OR IGNORE INTO bot_message_cursors VALUES (?,?)',
                       (chat_id, int(time.time() * 1000)))
            return None

    def advance_bot_message_cursor(self, chat_id, created_ms):
        with self.db() as db:
            db.execute('''UPDATE bot_message_cursors SET last_created_ms=?
                          WHERE chat_id=? AND last_created_ms<?''', (created_ms, chat_id, created_ms))

    def record_pairing(self, item):
        with self.db() as db:
            result = db.execute(
                """INSERT OR IGNORE INTO pairing
                   (message_id,code,sender_id,chat_id,chat_type,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (item["message_id"], item["code"], item["sender_id"],
                 item["chat_id"], item["chat_type"], time.time()),
            )
        return result.rowcount == 1

    def latest_pairing(self, code):
        if not code:
            return None
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM pairing WHERE code=? ORDER BY created_at DESC LIMIT 1", (code,)
            ).fetchone()
        return dict(row) if row else None

    def pending(self):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM inbound WHERE task_id IS NULL AND NOT EXISTS (SELECT 1 FROM inbound_media m WHERE m.message_id=inbound.message_id AND m.state='failed' AND m.notice_id IS NOT NULL) ORDER BY created_at")]

    def submitted(self):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                """SELECT * FROM inbound WHERE task_id IS NOT NULL
                   AND (notified_status IS NULL OR notified_status='需要你处理')
                   ORDER BY created_at""")]

    def pending_acks(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("""SELECT * FROM inbound
                WHERE ack_status='pending' AND notified_status IS NULL AND created_at>?
                AND (ack_last_attempt IS NULL OR ack_last_attempt<?)
                ORDER BY created_at LIMIT 30""", (time.time()-600,time.time()-5))]

    def ack_attempt(self, message_id):
        with self.db() as db:
            row=db.execute("""UPDATE inbound SET ack_last_attempt=?
                WHERE message_id=? AND ack_status='pending' AND notified_status IS NULL AND created_at>?
                AND (ack_last_attempt IS NULL OR ack_last_attempt<?) RETURNING *""",
                (time.time(),message_id,time.time()-600,time.time()-5)).fetchone()
        return dict(row) if row else None

    def mark_acknowledged(self, message_id, outbound_id):
        with self.db() as db:
            db.execute("UPDATE inbound SET ack_status='sent',ack_id=?,updated_at=? WHERE message_id=? AND ack_status='pending'",
                       (outbound_id,time.time(),message_id))

    def linked_task(self, message_id):
        if not message_id:
            return None, False
        with self.db() as db:
            row = db.execute(
                "SELECT task_id FROM inbound WHERE message_id=? OR outbound_id=? OR ack_id=?",
                (message_id, message_id, message_id),
            ).fetchone()
        return (row["task_id"], True) if row else (None, False)

    def linked_source(self,message_id):
        if not message_id: return None
        with self.db() as db:
            row=db.execute('SELECT * FROM inbound WHERE message_id=? OR outbound_id=? OR ack_id=? ORDER BY created_at DESC LIMIT 1',
                           (message_id,message_id,message_id)).fetchone()
        return dict(row) if row else None

    def original_source(self, source):
        seen = set()
        while source['message_id'] not in seen:
            seen.add(source['message_id'])
            parent = self.linked_source(source.get('parent_id') or source.get('root_id'))
            if not parent or parent['chat_id'] != source['chat_id'] or parent['sender_id'] != source['sender_id']:
                break
            source = parent
        return source

    def private_history(self, item):
        """Ordered authorized receipts from this exact private conversation only."""
        with self.db() as db:
            rows = [dict(r) for r in db.execute('''SELECT * FROM inbound
                WHERE app_id=? AND connection_id IS ? AND chat_id=? AND sender_id=?
                AND chat_type='p2p' AND rowid < (SELECT rowid FROM inbound WHERE message_id=?)
                ORDER BY rowid DESC LIMIT 100''',
                (item['app_id'],item.get('connection_id'),item['chat_id'],item['sender_id'],item['message_id']))]
        return [r for r in reversed(rows) if prompt_source(r)
                and not json.loads(r.get('route_json') or '{}').get('scheduled')]

    def private_submission(self, mid, payload=None):
        with self.db() as db:
            if payload is not None:
                db.execute('INSERT OR IGNORE INTO private_submissions VALUES (?,?)',
                           (mid,json.dumps(payload,ensure_ascii=False)))
            row=db.execute('SELECT payload FROM private_submissions WHERE message_id=?',(mid,)).fetchone()
        return json.loads(row[0]) if row else None

    def mark_submitted(self, message_id, task_id):
        with self.db() as db:
            db.execute("UPDATE inbound SET task_id=?,updated_at=? WHERE message_id=? AND task_id IS NULL",
                       (task_id, time.time(), message_id))

    def mark_notified(self, message_id, status, outbound_id):
        with self.db() as db:
            db.execute(
                "UPDATE inbound SET notified_status=?,outbound_id=?,updated_at=? WHERE message_id=?",
                (status, outbound_id, time.time(), message_id),
            )

    def defer_topic_event(self,payload):
        message=payload['event']['message']
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS pending_topic_events (message_id TEXT PRIMARY KEY,payload TEXT NOT NULL,created_at REAL NOT NULL)')
            db.execute('INSERT OR IGNORE INTO pending_topic_events VALUES (?,?,?)',(message['message_id'],json.dumps(payload,ensure_ascii=False),time.time()))

    def deferred_topic_events(self):
        with self.db() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pending_topic_events'").fetchone():return []
            return [dict(r) for r in db.execute('SELECT * FROM pending_topic_events ORDER BY created_at LIMIT 20')]

    def clear_topic_event(self,mid):
        with self.db() as db:db.execute('DELETE FROM pending_topic_events WHERE message_id=?',(mid,))

class BridgeClient:
    def __init__(self, base_url, connection_key):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                or parsed.port is None or parsed.username or parsed.password
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("Feishu worker must use the loopback Bridge endpoint")
        self.base_url = base_url.rstrip("/")
        self.connection_key = connection_key

    def _call(self, method, path, payload=None):
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        req = request.Request(
            self.base_url + path, data=body, method=method,
            headers={
                "Authorization": "Bearer " + self.connection_key,
                "Content-Type": "application/json",
            },
        )
        with request.urlopen(req, timeout=3) as response:
            return json.load(response)

    def submit(self, message, request_id, task_id=None, *, continue_from=None):
        payload = {"message": message, "request_id": request_id}
        if task_id:
            payload["task_id"] = task_id
        if continue_from:
            payload['continue_from'] = continue_from
        return self._call("POST", "/v1/tasks", payload)

    def task(self, task_id):
        return self._call("GET", "/v1/tasks/" + task_id)

    def recover(self, task_id):
        return self._call('POST','/v1/tasks/'+task_id+'/recover',{})

    def backend(self):
        return self._call("GET", "/v1/backend")["execution_backend"]

    def topic_resolve(self,data):return self._call('POST','/v1/group-topics/resolve',data)
    def group_join(self,data):return self._call('POST','/v1/group-topics/join',data)
    def topic(self,ident):return self._call('GET','/v1/group-topics/'+ident)
    def topic_alias(self,ident,mid):return self._call('POST','/v1/group-topics/alias',{'topic_id':ident,'message_id':mid})
    def topic_submit(self,ident,mid,prompt,rid):return self._call('POST','/v1/group-topics/submit',{'topic_id':ident,'message_id':mid,'message':prompt,'request_id':rid})


class FeishuSender:
    def __init__(self, app_id, app_secret):
        import lark_oapi as lark
        self.lark = lark
        self.app_id=app_id
        self.client = lark.Client.builder().app_id(app_id).app_secret(app_secret).timeout(60).build()

    def list_group_messages(self, chat_id, start_time, page_token=None):
        builder = (self.lark.im.v1.ListMessageRequest.builder()
                   .container_id_type('chat').container_id(chat_id)
                   .start_time(str(start_time)).sort_type('ByCreateTimeDesc').page_size(50))
        if page_token:
            builder = builder.page_token(page_token)
        return self.client.im.v1.message.list(builder.build())

    def download_resource(self,message_id,file_key,resource_type,output,max_bytes):
        req=(self.lark.im.v1.GetMessageResourceRequest.builder().message_id(message_id).file_key(file_key).type(resource_type).build())
        response=self.client.im.v1.message_resource.get(req)
        if not response.success():
            if response.code==234037:raise MediaError('resource_too_large','飞书不支持下载超过 100 MB 的消息附件，请改发可访问的云端链接。')
            if response.code in (234009,99991672):raise MediaError('resource_permission','机器人缺少消息附件读取权限。在飞书开发者后台的“权限管理 → 应用身份权限”中，搜索“获取与发送单聊、群组消息”（im:message），开通并发布生效。若已有 im:message:readonly，也可满足此接口；无需重复申请。im:resource 不能替代消息读取权限。完成后重新发送附件。\nhttps://open.feishu.cn/app/'+self.app_id+'/auth')
            if response.code in (234003,234004,234005):raise MediaError('resource_unavailable','附件不属于这条消息、已删除或机器人无法访问，请在当前会话重新发送原文件。')
            raise RuntimeError('Feishu resource download failed: code='+str(response.code))
        source=response.file
        if source is None:raise RuntimeError('missing_resource_body')
        size=0
        try:
            while True:
                chunk=source.read(1024*1024)
                if not chunk:break
                size+=len(chunk)
                if size>max_bytes:raise MediaError('resource_too_large','附件超过 100 MB 接收限制，请改发可访问的云端链接。')
                output.write(chunk)
        finally:source.close()

    def send_text(self,open_id,request_id,body):
        lark=self.lark
        stable_uuid=str(uuid.uuid5(uuid.NAMESPACE_URL,'codex-bridge-v2:'+request_id))
        req=(lark.im.v1.CreateMessageRequest.builder().receive_id_type('open_id')
             .request_body(lark.im.v1.CreateMessageRequestBody.builder().receive_id(open_id)
                .msg_type('text').content(json.dumps({'text':body},ensure_ascii=False)).uuid(stable_uuid).build()).build())
        response=self.client.im.v1.message.create(req)
        if not response.success():raise RuntimeError('Feishu send failed: code='+str(response.code))
        return getattr(response.data,'message_id',None)

    def reply(self, message_id, status, body):
        lark = self.lark
        stable_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "codex-bridge-v2:" + message_id + ":" + status))
        req = (
            lark.im.v1.ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                lark.im.v1.ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(json.dumps({"text": body}, ensure_ascii=False))
                .uuid(stable_uuid)
                .build()
            ).build()
        )
        response = self.client.im.v1.message.reply(req)
        if not response.success():
            raise RuntimeError("Feishu reply failed: code=" + str(response.code))
        return getattr(response.data, "message_id", None)

    def send_group(self,chat_id,request_id,body):
        stable=str(uuid.uuid5(uuid.NAMESPACE_URL,'codex-bridge-v2:group:'+chat_id+':'+request_id))
        req=(self.lark.im.v1.CreateMessageRequest.builder().receive_id_type('chat_id').request_body(
            self.lark.im.v1.CreateMessageRequestBody.builder().receive_id(chat_id).msg_type('text')
            .content(json.dumps({'text':body},ensure_ascii=False)).uuid(stable).build()).build())
        response=self.client.im.v1.message.create(req)
        if not response.success():raise RuntimeError('Feishu group send failed: code='+str(response.code))
        return getattr(response.data,'message_id',None)

class FeishuAdapter:
    def __init__(self, config, inbox, bridge, sender, client_factory=None):
        self.config, self.inbox, self.bridge, self.sender = config, inbox, bridge, sender
        self.client_factory=client_factory
        self.clients={}
        self.media=MediaReceiver(inbox,sender)
        self.next_bot_poll=0.0

    def acknowledge(self, message_id):
        """Confirm an accepted user message before queueing; retry with the same Feishu UUID."""
        item=self.inbox.ack_attempt(message_id)
        if not item:return
        body='我在，消息收到了。处理结果会发回这里。'
        try:
            if item['chat_type']=='group':
                outbound=self.sender.send_group(item['chat_id'],'received:'+message_id,body)
            else:
                outbound=self.sender.reply(message_id,'received',body)
            if not outbound:raise RuntimeError('missing_ack_id')
            self.inbox.mark_acknowledged(message_id,outbound)
        except Exception:
            LOG.warning('Feishu receipt pending message_id=%s',message_id)

    def accept_with_ack(self, item):
        item['ack']=True
        accepted=self.inbox.accept(item)
        if accepted:self.acknowledge(item['message_id'])
        return accepted

    def poll_bot_messages(self):
        """Feishu does not push messages sent by other apps; read only explicit @s."""
        if time.monotonic() < self.next_bot_poll:
            return
        self.next_bot_poll=time.monotonic()+20
        bot_id=self.config.get('bot_open_id')
        if not bot_id or not hasattr(self.sender,'list_group_messages'):
            return
        for chat_id,group in (self.config.get('group_configs') or {}).items():
            if (chat_id not in self.config.get('allowed_chat_ids',[]) or
                    not group.get('enabled',True) or not group.get('topic_collaboration')):
                continue
            cursor=self.inbox.bot_message_cursor(chat_id)
            if cursor is None:
                continue
            try:
                items=[];page_token=None;complete=False
                for _ in range(20):
                    response=self.sender.list_group_messages(chat_id,max(0,cursor//1000-1),page_token)
                    if not response.success():
                        raise RuntimeError('Feishu message history code='+str(response.code))
                    data=response.data
                    page=list(getattr(data,'items',None) or [])
                    items.extend(page)
                    if not getattr(data,'has_more',False) or any(int(m.create_time)<cursor for m in page):
                        complete=True;break
                    page_token=getattr(data,'page_token',None)
                    if not page_token:
                        break
                if not complete:
                    LOG.warning('Bot message history is incomplete for chat=%s; cursor retained',chat_id[-6:])
                    continue
                for message in sorted(items,key=lambda m:(int(m.create_time),str(m.message_id))):
                    if int(message.create_time)<cursor:
                        continue
                    sender=getattr(message,'sender',None)
                    sender_id=getattr(sender,'id',None)
                    if (getattr(sender,'sender_type',None)!='app' or not sender_id or
                            sender_id==self.config.get('app_id')):
                        continue
                    mentions=getattr(message,'mentions',None) or []
                    if not any(getattr(mention,'id',None)==bot_id for mention in mentions):
                        continue
                    self.handle_event({'header':{'app_id':self.config['app_id']},'event':{
                        'sender':{'sender_type':'app','sender_id':{'open_id':sender_id}},
                        'message':{'message_id':message.message_id,'chat_id':chat_id,'chat_type':'group',
                                   'message_type':message.msg_type,'content':message.body.content,
                                   'parent_id':getattr(message,'parent_id',None),
                                   'root_id':getattr(message,'root_id',None),
                                   'mentions':[{'id':{'open_id':m.id},'key':m.key,'name':m.name}
                                               for m in mentions]},
                    }})
                if items:
                    self.inbox.advance_bot_message_cursor(chat_id,max(int(m.create_time) for m in items))
            except Exception:
                LOG.exception('Could not check bot messages for chat=%s',chat_id[-6:])

    def client_for(self,connection_id):
        if not connection_id or connection_id==self.config.get('connection_id'): return self.bridge
        if connection_id not in self.clients:
            if self.client_factory: self.clients[connection_id]=self.client_factory(connection_id)
            else:
                # Resolve even removed group routes through the original saved connection.
                with request.urlopen(self.bridge.base_url+'/api/connections/'+connection_id,timeout=3) as response:
                    connection=json.load(response)
                self.clients[connection_id]=BridgeClient(self.bridge.base_url,connection['api_key'])
        return self.clients[connection_id]

    def handle_group_added(self,payload):
        if (payload.get('header') or {}).get('app_id') not in (None,self.config.get('app_id')):return False
        event=payload.get('event') or {};operator=event.get('operator_id') or {}
        chat=event.get('chat_id');owner=operator.get('open_id')
        if not chat or not owner:return False
        join={'chat_id':chat,'sender_id':owner}
        if isinstance(event.get('name'),str):join['name']=event['name'][:60]
        result=self.bridge.group_join(join)
        route=result.get('route')
        if route:
            self.config.setdefault('group_configs',{})[chat]=route
            chats=self.config.setdefault('allowed_chat_ids',[])
            if chat not in chats:chats.append(chat)
            return True
        return False

    def handle_event(self, payload, *, retry_topic=False):
        if (payload.get('header') or {}).get('app_id') not in (None, self.config.get('app_id')):
            return False
        event = payload.get("event") or {}
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        sender_type=sender.get('sender_type')
        if sender_type not in ('user','app'):
            return False
        message_type=message.get('message_type')
        sender_id = (sender.get("sender_id") or {}).get("open_id")
        message_id = message.get("message_id")
        chat_id = message.get("chat_id")
        chat_type = message.get("chat_type")
        if not sender_id or not message_id or not chat_id or chat_type not in ("p2p", "group"):
            return False
        if sender_type=='app':
            # App messages are accepted only in an already configured group and
            # only when they explicitly @ this bot. DMs and self-echoes stay out.
            if (chat_type!='group' or chat_id not in self.config.get('allowed_chat_ids',[]) or
                    sender_id in (self.config.get('app_id'),self.config.get('bot_open_id')) or
                    not any((m.get('id') or {}).get('open_id')==self.config.get('bot_open_id')
                            for m in message.get('mentions') or [])):
                return False
        topic=None
        group=(self.config.get('group_configs') or {}).get(chat_id,{}) if chat_type=='group' else {}
        dispatcher=self.config.get('task_dispatcher') or {}
        activating=(chat_type=='group' and chat_id not in self.config.get('allowed_chat_ids',[])
                    and sender_id in (dispatcher.get('owner_open_id'),self.config.get('auto_group_owner_open_id'))
                    and any((m.get('id') or {}).get('open_id')==self.config.get('bot_open_id') for m in message.get('mentions') or []))
        if activating:
            try:route=self.bridge.group_join({'chat_id':chat_id,'sender_id':sender_id}).get('route')
            except Exception:
                if retry_topic:raise
                self.inbox.defer_topic_event(payload);return True
            if not route:return False
            self.config.setdefault('group_configs',{})[chat_id]=route
            self.config.setdefault('allowed_chat_ids',[]).append(chat_id)
            group=route
        shared=(chat_type=='group' and chat_id in self.config.get('allowed_chat_ids',[]) and group.get('enabled',True)
                and group.get('topic_collaboration') and not self.config.get('assistant_manager'))
        if chat_type == "group":
            bot_id = self.config.get("bot_open_id")
            mentions = message.get("mentions") or []
            bot_mentions = [m for m in mentions if (m.get("id") or {}).get("open_id") == bot_id]
            if shared:
                # Only targeted roots or replies can become shared work; ordinary group chat is ignored.
                if not mentions and not message.get('parent_id') and not message.get('root_id'):return False
                if not bot_mentions and not message.get('parent_id') and not message.get('root_id'):return False
                if not bot_id:return False
                client=self.client_for(group.get('connection_id') or self.config.get('connection_id'))
                try:
                    data={'chat_id':chat_id,'message_id':message_id,'parent_id':message.get('parent_id'),
                         'root_id':message.get('root_id'),'mentions':[(m.get('id') or {}).get('open_id','') for m in mentions]}
                    topic=client.topic_resolve(data).get('topic')
                except Exception:
                    if retry_topic:raise
                    self.inbox.defer_topic_event(payload)
                    LOG.warning('Group topic receipt deferred message_id=%s',message_id);return True
                if not topic:return False
            elif not bot_id or not bot_mentions:
                # A pure attachment cannot always carry @. A reply to this user's
                # existing authorized task is explicit targeting, never ambient group intake.
                previous=self.inbox.linked_source(message.get('parent_id')) or self.inbox.linked_source(message.get('root_id'))
                if (message_type not in ('image','file','audio','media') or not previous or not prompt_source(previous)
                    or previous['chat_id']!=chat_id or previous['sender_id']!=sender_id
                    or previous.get('app_id')!=self.config.get('app_id')):return False
        try:
            content = json.loads(message.get("content") or "{}")
        except (ValueError, TypeError):
            return False
        media_info=None
        try:
            body,resources=parse_media(message_type,content)
            if resources:media_info={'type':message_type,'resources':resources}
        except MediaError as exc:
            body='收到一条无法解析的飞书消息。';media_info={'type':str(message_type),'resources':[],'error':exc.detail}
        if chat_type == "group":
            for mention in bot_mentions:
                body = body.replace(mention.get("key") or "\0", "", 1)
        body = body.strip()
        if not body or len(body) > 12000:
            return False
        if media_info is None and parse_consent(body):
            if chat_type!='p2p':return False
            try:event_time=float(message['create_time'])/1000
            except (KeyError,TypeError,ValueError):event_time=time.time()
            # This dedicated route can only approve after the service verifies the saved creator.
            return self.inbox.accept({'message_id':message_id,'chat_id':chat_id,'sender_id':sender_id,
                'text':body,'prompt':'','connection_id':self.config.get('connection_id'),
                'parent_id':message.get('parent_id'),'root_id':message.get('root_id'),
                'app_id':self.config.get('app_id'),'chat_type':chat_type,
                'route_json':json.dumps({'mode':'assistant_authorization','event_time':event_time})})
        # Management is a dedicated route, never a full-access Codex task.
        manager=self.config.get('assistant_manager')
        if manager:
            if chat_type!='p2p' or sender_id!=manager.get('admin_open_id'):
                return False
            if media_info is not None:
                media_info['error']='助手管理员用于创建和管理助手。请把图片、视频、语音或文件发送给对应的业务助手。'
                return self.accept_with_ack({'message_id':message_id,'chat_id':chat_id,'sender_id':sender_id,'text':body,'prompt':'',
                    'connection_id':self.config.get('connection_id'),'app_id':self.config.get('app_id'),'chat_type':chat_type,
                    'route_json':'{"mode":"media_notice"}','media':media_info})
            return self.accept_with_ack({'message_id':message_id,'chat_id':chat_id,'sender_id':sender_id,
                'text':body,'prompt':'','parent_id':message.get('parent_id'),'root_id':message.get('root_id'),
                'connection_id':self.config.get('connection_id'),'app_id':self.config.get('app_id'),
                'chat_type':chat_type,'route_json':json.dumps({'mode':'assistant_manager'})})
        pairing_code = self.config.get("pairing_code")
        if (media_info is None and pairing_code and time.time() < self.config.get("pairing_expires_at", 0)
                and body == pairing_code):
            return self.inbox.record_pairing({
                "message_id": message_id, "code": pairing_code,
                "sender_id": sender_id, "chat_id": chat_id, "chat_type": chat_type,
            })
        # Group authorization covers all members; personal authorization is for DMs.
        # Follow-ups still require the original sender and chat below.
        if chat_type == "group":
            if chat_id not in self.config.get("allowed_chat_ids", []) and not topic:
                return False
        elif sender_id not in self.config.get("allowed_open_ids", []):
            return False
        route = {'mode': 'prompt', 'connection_id': self.config.get('connection_id'),
                 'prompt_template': self.config.get('prompt_template', DEFAULT_PROMPT_TEMPLATE)}
        if chat_type=='p2p':
            route['private_conversation_v1']=True
            if media_info is None and body in ('/new','新对话','重新开始对话'):
                route['new_conversation']=True
                body='用户已开启一段新对话。请简短告知可以开始，不继续执行上一段对话的任务。'
        group = (self.config.get('group_configs') or {}).get(chat_id, {}) if chat_type == 'group' else {}
        if group.get('enabled') is False:
            return False
        route.update({k: group[k] for k in ('connection_id', 'prompt_template', 'name') if k in group})
        if topic:route.update(topic['route'])
        parent = (self.inbox.linked_source(message.get('parent_id'))
                  or self.inbox.linked_source(message.get('root_id')))
        if parent and not topic:
            if (parent['chat_id'] != chat_id or parent['sender_id'] != sender_id
                    or parent.get('app_id') not in (None, self.config.get('app_id'))):
                return False
            # Old tasks are read-only after the switch; start a fresh message instead.
            if not prompt_source(parent):
                return False
            if chat_type!='p2p':
                frozen = json.loads(parent.get('route_json') or '{}')
                route.update({k: frozen[k] for k in ('connection_id', 'prompt_template', 'name') if k in frozen})
                route['connection_id'] = parent.get('connection_id') or route['connection_id']
        try:
            from assistant_self_update import applied_rule, instruction
            from assistant_schedules import instruction as schedule_instruction
            template = route['prompt_template']
            template += applied_rule(self.config, chat_id if chat_type == 'group' else None)
            template += instruction(self.config, None, message_id, sender_id, chat_type, chat_id,
                                    body, has_media=media_info is not None, sender_type=sender_type)
            template += schedule_instruction(self.config, None, message_id, sender_id,
                                             body, has_media=media_info is not None)
            if topic:
                speaker=hashlib.sha256(sender_id.encode()).hexdigest()[:6]
                targets='、'.join(str(m.get('name') or m.get('key') or '') for m in (message.get('mentions') or []))
                body=f'【群话题补充；发言成员 {speaker}'+('；点名 '+targets if targets else '')+'】\n'+body
            prompt = render_prompt(template, body, {
                'source_message_id': message_id, 'chat_id': chat_id,
                'app_id': self.config.get('app_id'),
                'parent_id': message.get('parent_id'), 'root_id': message.get('root_id'),
            })
        except BridgeError:
            LOG.warning("Feishu message exceeds the Bridge prompt limit message_id=%s", message_id)
            if media_info is None:return False
            media_info['error']='附件与处理规则合成后超过消息长度限制，请缩短文字或处理规则后重新发送。'
            prompt=''
        return self.accept_with_ack({
            "message_id": message_id, "chat_id": chat_id,
            "sender_id": sender_id, "text": body, "prompt": prompt,
            "parent_id": message.get("parent_id"), "root_id": message.get("root_id"),
            'connection_id':route.get('connection_id'),'app_id':self.config.get('app_id'),
            'chat_type':chat_type,
            'route_json':json.dumps({**{k:v for k,v in route.items() if k!='connection_key'},
                                     'sender_type':sender_type},ensure_ascii=False),
            'media':media_info,
        })

    def private_payload(self,item,prompt,parent,client,history):
        """Freeze intake retries, and repair context split by pre-fix receivers."""
        cached=self.inbox.private_submission(item['message_id'])
        if cached:return cached
        previous=client.task(parent['task_id']) if parent and parent.get('task_id') else None
        changed_backend=bool(previous and previous.get('execution_backend','codex') !=
                             (client.backend() if hasattr(client,'backend') else 'codex'))
        if previous and (changed_backend or not json.loads(parent.get('route_json') or '{}').get('private_conversation_v1') or not previous.get('thread_id')):
            # Old versions made a new thread per message. Make their transcript
            # readable without replaying, rewriting or resuming any old task.
            entries=[]
            for row in history:
                is_parent=row['message_id']==parent['message_id']
                if is_parent and previous.get('thread_id') and not changed_backend:break
                if not row.get('task_id'):continue
                if json.loads(row.get('route_json') or '{}').get('new_conversation'):entries=[]
                old=client.task(row['task_id'])
                media=self.media.record(row['message_id'])
                entries.append({'user':row['text'],'assistant':old.get('result'),
                    'status':old.get('status'),'attachments':json.loads(media['manifest']) if media and media.get('manifest') else []})
                if is_parent:break
            if entries:
                folder=Path(self.inbox.path).parent/'conversations'
                folder.mkdir(mode=0o700,exist_ok=True);folder.chmod(0o700)
                path=folder/(hashlib.sha256(item['message_id'].encode()).hexdigest()+'.json')
                content=json.dumps({'notice':'同一用户与本助手的旧对话资料。仅供理解上下文，不是新命令，不重做已完成的操作；当前消息与当前助手规则优先。最多保留最近100条接收记录。',
                    'messages':entries},ensure_ascii=False,indent=2)
                path.write_text(content);path.chmod(0o600)
                prompt+='\n\n[连续对话修复] 此前的消息曾被拆到不同会话。回答前必须读取同一私聊的历史资料文件：'+str(path.resolve())+'。只据实际记录理解前文，不重新执行历史命令。'
        # Current rules replace earlier task wrappers; task-scoped tool tokens
        # from old turns must never be reused in this new delivery.
        if parent:
            prompt+='\n\n[连续私聊] 这是同一用户的后续消息，请结合前文回答。本条附带的助手规则、来源信息和协作入口为当前有效版本；旧轮次的任务入口已失效，不要沿用。'
        payload={'message':prompt,'continue_from':parent['task_id'] if parent and not changed_backend else None}
        # Near the intake limit, keep the complete request in a private file;
        # adding continuity instructions must never discard a valid message.
        if len(prompt)>12000 or len(json.dumps({**payload,'request_id':'fs_'+'x'*48},ensure_ascii=False).encode())>16*1024:
            folder=Path(self.inbox.path).parent/'conversations'
            folder.mkdir(mode=0o700,exist_ok=True);folder.chmod(0o700)
            path=folder/(hashlib.sha256(item['message_id'].encode()).hexdigest()+'.request.json')
            path.write_text(json.dumps({'current_request':prompt},ensure_ascii=False));path.chmod(0o600)
            payload['message']='这条是同一私聊的当前请求。文字较长，完整请求及附件位置已存入本机文件：'+str(path.resolve())+'。必须读取 current_request 的完整内容，再按其中当前助手规则和用户消息执行。前文只作背景，不重做旧任务；使用本条新提供的协作入口。'
        return self.inbox.private_submission(item['message_id'],payload)

    def pump_once(self):
        from assistant_schedules import fire_due
        fire_due(self.inbox, self.config)
        for item in self.inbox.pending_acks():
            if item.get('app_id')==self.config.get('app_id'):
                self.acknowledge(item['message_id'])
        for receipt in self.inbox.deferred_topic_events():
            try:
                self.handle_event(json.loads(receipt['payload']),retry_topic=True)
                self.inbox.clear_topic_event(receipt['message_id'])
            except Exception:LOG.warning('Group topic receipt still pending message_id=%s',receipt['message_id'])
        for item in self.inbox.submitted():
            if not prompt_source(item) or item.get('app_id') != self.config.get('app_id'):
                continue
            try:
                task = self.client_for(item.get('connection_id')).task(item['task_id'])
                status = task.get('status')
                if status not in NOTIFY_STATUSES or status == item['notified_status']:
                    continue
                result = str(task.get('result') or '')
                body = result_message(status, result, task.get('error_code'))
                route=json.loads(item.get('route_json') or '{}')
                if route.get('topic_id'):
                    client=self.client_for(item.get('connection_id'));topic=client.topic(route['topic_id'])
                    if topic.get('discussion_pending'):continue
                    if topic.get('discussion_failed'):body='部分协作消息未能发回群。\n'+body
                    outbound_id=self.sender.send_group(item['chat_id'],'result:'+item['message_id']+':'+status,body)
                    if outbound_id:client.topic_alias(topic['id'],outbound_id)
                elif route.get('scheduled'):
                    if item.get('chat_type') == 'group':
                        outbound_id = self.sender.send_group(item['chat_id'],
                            'schedule:'+item['message_id']+':'+status, body)
                    else:
                        outbound_id = self.sender.send_text(item['sender_id'],
                            'schedule:'+item['message_id']+':'+status, body)
                else:outbound_id = self.sender.reply(item['message_id'], status, body)
                self.inbox.mark_notified(item['message_id'], status, outbound_id)
            except Exception:
                LOG.exception('Could not notify Feishu message_id=%s', item['message_id'])

        for item in self.inbox.pending():
            if item.get('app_id')==self.config.get('app_id') and json.loads(item.get('route_json') or '{}').get('mode')=='media_notice':
                try:self.media.prepare(item)
                except Exception:LOG.warning('Feishu media notice pending message_id=%s',item['message_id'])
                continue
            if not prompt_source(item) or item.get('app_id') != self.config.get('app_id'):
                continue
            parent_id = item['parent_id'] or item['root_id']
            parent = self.inbox.linked_source(item['parent_id']) or self.inbox.linked_source(item['root_id'])
            parent_task = parent.get('task_id') if parent else None
            if parent and not parent_task:
                failed_media=self.media.record(parent['message_id'])
                if not failed_media or failed_media['state']!='failed':continue
            request_id = 'fs_' + hashlib.sha256(
                (item.get('app_id') + ':' + item['message_id']).encode()).hexdigest()[:48]
            try:
                client = self.client_for(item.get('connection_id'))
                route=json.loads(item.get('route_json') or '{}')
                private=item.get('chat_type')=='p2p'
                history=([] if route.get('scheduled') else self.inbox.private_history(item)) if private else []
                latest=history[-1] if history else None
                if private and not route.get('scheduled'):
                    # Even explicit branch replies wait for earlier receipts;
                    # a burst of text + media is never dispatched concurrently.
                    if latest:
                        if not latest.get('task_id'):
                            media=self.media.record(latest['message_id'])
                            if not media or media['state']!='failed':continue
                        else:
                            prior = client.task(latest['task_id'])
                            if (prior.get('error_code') == 'delivery_uncertain'
                                    and prior.get('status') == '需要你处理'
                                    and item['text'].strip().rstrip('。！!') in ('确认','确定','继续','重试','恢复')
                                    and (not parent or parent['task_id'] == latest['task_id'])):
                                # Handle recovery before the normal serialization gate.
                                # Only the same user's scoped prior receipt is selected;
                                # this neither approves a command nor replays a message.
                                client.recover(latest['task_id'])
                                outbound_id = self.sender.reply(item['message_id'],'recovery_received',
                                    '已收到。我正在核查原任务的交接状态，不会另开任务或重复执行。你不需要再发确认；恢复后结果会回到这里。')
                                self.inbox.mark_submitted(item['message_id'],latest['task_id'])
                                self.inbox.mark_notified(item['message_id'],'recovery_received',outbound_id)
                                continue
                            if prior.get('status') in ('执行中','等待 Codex','需要你处理'):continue
                    if route.get('new_conversation'):parent=None
                    else:parent=parent or next((r for r in reversed(history) if r.get('task_id')),None)
                    if parent and (parent.get('app_id')!=item['app_id'] or parent.get('connection_id')!=item.get('connection_id')):
                        parent=None
                    parent_task=parent.get('task_id') if parent else None
                topic=client.topic(route['topic_id']) if route.get('topic_id') else None
                if topic:parent_task=topic['task_id']
                # Serialize follow-ups on the same task, while other tasks can run.
                if parent_task and client.task(parent_task).get('status') in ('执行中', '等待 Codex', '需要你处理'):
                    continue
                prompt=self.media.prepare(item)
                if prompt is None:continue
                if private:
                    frozen=self.private_payload(item,prompt,parent,client,history)
                    result=client.submit(frozen['message'],request_id,continue_from=frozen['continue_from'])
                else:
                    result = (client.topic_submit(topic['id'],item['message_id'],prompt,request_id) if topic
                              else client.submit(prompt, request_id, parent_task))
                self.inbox.mark_submitted(item['message_id'], result['task_id'])
                LOG.info('Feishu message queued message_id=%s task_id=%s', item['message_id'], result['task_id'])
            except Exception:
                LOG.exception('Could not submit Feishu message_id=%s', item['message_id'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--bridge-url", default="http://127.0.0.1:8797")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    config = load(args.config)
    # Older onboarding bots have a verified owner in their saved creation record.
    # Keep their private config untouched while enabling first-@ group setup.
    app_ref=Path(args.config).parent.name if Path(args.config).name=='config.json' else 'default'
    data_root=Path(args.config).parent.parent.parent if app_ref!='default' else Path(args.config).parent
    for job_path in data_root.glob('assistant-onboarding/asst_*/job.json'):
        job=load(job_path)
        if job.get('app_ref')!=app_ref or job.get('kind')=='manager':continue
        credentials=load(job_path.parent/'credentials.json')
        if credentials.get('app_id')==config.get('app_id') and credentials.get('owner_open_id'):
            config.setdefault('auto_group_owner_open_id',credentials['owner_open_id'])
            break
    if not config.get("enabled"):
        raise SystemExit("Feishu receiver is disabled")
    adapter = FeishuAdapter(
        config, Inbox(args.state_db),
        BridgeClient(args.bridge_url, config["connection_key"]),
        FeishuSender(config["app_id"], config["app_secret"]),
    )
    def pump():
        while True:
            try:
                adapter.pump_once()
            except Exception:
                LOG.exception("Feishu inbox pump failed")
            time.sleep(5)

    threading.Thread(target=pump, daemon=True).start()
    def bot_poll():
        while True:
            try:
                adapter.poll_bot_messages()
            except Exception:
                LOG.exception('Feishu bot message polling failed')
            time.sleep(2)

    threading.Thread(target=bot_poll, daemon=True).start()
    import lark_oapi as lark
    def on_message(data):
        adapter.handle_event(json.loads(lark.JSON.marshal(data)))
    def on_group_added(data):
        try:adapter.handle_group_added(json.loads(lark.JSON.marshal(data)))
        except Exception:LOG.exception('Could not configure group after bot invitation')

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message)
               .register_p2_im_chat_member_bot_added_v1(on_group_added)
               .build())
    LOG.info("Starting Feishu long connection for app_id=%s", config["app_id"])
    lark.ws.Client(config["app_id"], config["app_secret"], event_handler=handler).start()


if __name__ == "__main__":
    main()
