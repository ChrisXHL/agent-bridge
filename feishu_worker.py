#!/usr/bin/env python3
"""Feishu long-connection receiver for the isolated Bridge 2.0 instance."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
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
from feishu_review import approval_prompt, review_doc_url, selected_voice, template_content
from card_queue import CardQueue
from phase_prompts import review_prompt, review_template
from workflow_runtime import WorkflowRuntime

LOG = logging.getLogger("codex_bridge.feishu")
NOTIFY_STATUSES = {"已完成", "失败", "已停止", "需要你处理"}


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
            db.execute("""CREATE TABLE IF NOT EXISTS review_cards (
                source_message_id TEXT PRIMARY KEY REFERENCES inbound(message_id),
                card_message_id TEXT NOT NULL UNIQUE, task_id TEXT NOT NULL,
                doc_url TEXT NOT NULL, state TEXT NOT NULL,
                voice TEXT, material_url TEXT, created_at REAL NOT NULL,
                updated_at REAL NOT NULL)""")
            columns={row['name'] for row in db.execute('PRAGMA table_info(inbound)')}
            for column in ('connection_id','app_id','route_json','chat_type'):
                if column not in columns:
                    db.execute('ALTER TABLE inbound ADD COLUMN '+column+' TEXT')
        Path(path).chmod(0o600)
        self.cards=CardQueue(self)
        self.workflows=WorkflowRuntime(self)

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
                   (message_id,chat_id,sender_id,text,prompt,parent_id,root_id,created_at,updated_at,connection_id,app_id,route_json,chat_type)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item["message_id"], item["chat_id"], item["sender_id"], item["text"], item["prompt"],
                 item.get("parent_id"), item.get("root_id"), time.time(), time.time(),
                 item.get('connection_id'),item.get('app_id'),item.get('route_json'),item.get('chat_type')),
            )
        return result.rowcount == 1

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
                "SELECT * FROM inbound WHERE task_id IS NULL ORDER BY created_at")]

    def submitted(self):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                """SELECT * FROM inbound WHERE task_id IS NOT NULL
                   AND (notified_status IS NULL OR notified_status='需要你处理')
                   ORDER BY created_at""")]

    def linked_task(self, message_id):
        if not message_id:
            return None, False
        with self.db() as db:
            row = db.execute(
                "SELECT task_id FROM inbound WHERE message_id=? OR outbound_id=?",
                (message_id, message_id),
            ).fetchone()
        return (row["task_id"], True) if row else (None, False)

    def linked_source(self,message_id):
        if not message_id: return None
        with self.db() as db:
            row=db.execute('SELECT * FROM inbound WHERE message_id=? OR outbound_id=? ORDER BY created_at DESC LIMIT 1',
                           (message_id,message_id)).fetchone()
            if not row:
                row=db.execute('''SELECT i.* FROM inbound i JOIN outgoing_cards c ON i.message_id=c.source_message_id
                    WHERE c.message_id=?''',(message_id,)).fetchone()
            if not row:
                row=db.execute('''SELECT i.* FROM inbound i JOIN review_cards c ON i.message_id=c.source_message_id
                    WHERE c.card_message_id=?''',(message_id,)).fetchone()
            if not row:
                row=db.execute('''SELECT i.* FROM inbound i JOIN workflow_runs w ON i.message_id=w.source_id
                    JOIN workflow_reviews r ON r.run_id=w.id WHERE r.message_id=?''',(message_id,)).fetchone()
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

    def mark_submitted(self, message_id, task_id):
        with self.db() as db:
            db.execute("UPDATE inbound SET task_id=?,updated_at=? WHERE message_id=? AND task_id IS NULL",
                       (task_id, time.time(), message_id))
            db.execute("UPDATE outgoing_cards SET state='submitted',updated_at=? WHERE message_id=? AND state='pending'",
                       (time.time(),message_id))
            self.workflows.bind_task(db,message_id,task_id)

    def mark_notified(self, message_id, status, outbound_id):
        with self.db() as db:
            db.execute(
                "UPDATE inbound SET notified_status=?,outbound_id=?,updated_at=? WHERE message_id=?",
                (status, outbound_id, time.time(), message_id),
            )

    def record_review_card(self, item, card_message_id, doc_url):
        with self.db() as db:
            db.execute(
                """INSERT OR IGNORE INTO review_cards
                   (source_message_id,card_message_id,task_id,doc_url,state,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (item["message_id"], card_message_id, item["task_id"],
                 doc_url, "awaiting_review", time.time(), time.time()),
            )

    def has_review_card(self, source_message_id):
        with self.db() as db:
            return db.execute("SELECT 1 FROM review_cards WHERE source_message_id=?",
                              (source_message_id,)).fetchone() is not None

    def accept_review(self, card_message_id, chat_id, operator_id, voice):
        with self.db() as db:
            row = db.execute("""SELECT r.*,i.chat_id,i.sender_id FROM review_cards r
                JOIN inbound i ON i.message_id=r.source_message_id
                WHERE r.card_message_id=?""", (card_message_id,)).fetchone()
            if not row or row["chat_id"] != chat_id or row["sender_id"] != operator_id:
                return "not_found"
            if row["state"] != "awaiting_review":
                return "already_submitted"
            db.execute("""UPDATE review_cards SET state='pending',voice=?,material_url=?,updated_at=?
                WHERE card_message_id=? AND state='awaiting_review'""",
                (voice["voice"], voice["material_url"], time.time(), card_message_id))
            return "accepted"

    def pending_reviews(self):
        with self.db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM review_cards WHERE state='pending' ORDER BY created_at")]

    def mark_review_submitted(self, card_message_id):
        with self.db() as db:
            db.execute("""UPDATE review_cards SET state='submitted',updated_at=?
                WHERE card_message_id=? AND state='pending'""", (time.time(), card_message_id))


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

    def submit(self, message, request_id, task_id=None):
        payload = {"message": message, "request_id": request_id}
        if task_id:
            payload["task_id"] = task_id
        return self._call("POST", "/v1/tasks", payload)

    def task(self, task_id):
        return self._call("GET", "/v1/tasks/" + task_id)


class FeishuSender:
    def __init__(self, app_id, app_secret):
        import lark_oapi as lark
        self.lark = lark
        self.client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

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

    def reply_review_card(self, message_id, doc_url):
        lark = self.lark
        stable_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, "codex-bridge-v2:review:" + message_id))
        req = (
            lark.im.v1.ReplyMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                lark.im.v1.ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(template_content(doc_url))
                .uuid(stable_uuid)
                .build()
            ).build()
        )
        response = self.client.im.v1.message.reply(req)
        if not response.success():
            raise RuntimeError("Feishu review card reply failed: code=" + str(response.code))
        card_message_id = getattr(response.data, "message_id", None)
        if not card_message_id:
            raise RuntimeError("Feishu review card reply had no message ID")
        return card_message_id

    def reply_card(self,message_id,content,request_id):
        lark=self.lark
        req=(lark.im.v1.ReplyMessageRequest.builder().message_id(message_id)
             .request_body(lark.im.v1.ReplyMessageRequestBody.builder().msg_type('interactive')
             .content(content).uuid(str(uuid.uuid5(uuid.NAMESPACE_URL,'codex-bridge-card:'+request_id))).build()).build())
        response=self.client.im.v1.message.reply(req)
        if not response.success(): raise RuntimeError('Feishu card send failed: code='+str(response.code))
        mid=getattr(response.data,'message_id',None)
        if not mid: raise RuntimeError('Feishu card send returned no message ID')
        return mid

    def verify_receipt(self,value,chat_id):
        files=set();receipts=[]
        for mid in value['message_ids']:
            req=self.lark.im.v1.GetMessageRequest.builder().message_id(mid).build()
            response=self.client.im.v1.message.get(req)
            if not response.success():raise BridgeError(502,'receipt_unavailable','无法读回飞书交付消息')
            items=getattr(response.data,'items',None) or []
            if not items:raise BridgeError(409,'receipt_missing','未找到交付消息')
            for item in items:
                if item.chat_id!=chat_id:raise BridgeError(409,'wrong_delivery_target','交付消息不在原任务会话')
                content=json.loads(item.body.content)
                if content.get('file_key'):files.add(content['file_key'])
                receipts.append({'message_id':item.message_id,'chat_id':item.chat_id,'content':content})
        if not set(value['file_keys'])<=files:raise BridgeError(409,'receipt_files_mismatch','回执文件与飞书消息不匹配')
        return {'verified':True,'messages':receipts}


class FeishuAdapter:
    def __init__(self, config, inbox, bridge, sender, client_factory=None):
        self.config, self.inbox, self.bridge, self.sender = config, inbox, bridge, sender
        self.client_factory=client_factory
        self.clients={}

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

    def handle_event(self, payload):
        event = payload.get("event") or {}
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        if sender.get("sender_type") != "user" or message.get("message_type") != "text":
            return False
        sender_id = (sender.get("sender_id") or {}).get("open_id")
        message_id = message.get("message_id")
        chat_id = message.get("chat_id")
        chat_type = message.get("chat_type")
        if not sender_id or not message_id or not chat_id or chat_type not in ("p2p", "group"):
            return False
        if chat_type == "group":
            bot_id = self.config.get("bot_open_id")
            mentions = message.get("mentions") or []
            bot_mentions = [m for m in mentions if (m.get("id") or {}).get("open_id") == bot_id]
            if not bot_id or not bot_mentions:
                return False
        try:
            content = json.loads(message.get("content") or "{}")
            body = content.get("text", "")
        except (ValueError, TypeError):
            return False
        if not isinstance(body, str):
            return False
        if chat_type == "group":
            for mention in bot_mentions:
                body = body.replace(mention.get("key") or "\0", "", 1)
        body = body.strip()
        if not body or len(body) > 12000:
            return False
        pairing_code = self.config.get("pairing_code")
        if (pairing_code and time.time() < self.config.get("pairing_expires_at", 0)
                and body == pairing_code):
            return self.inbox.record_pairing({
                "message_id": message_id, "code": pairing_code,
                "sender_id": sender_id, "chat_id": chat_id, "chat_type": chat_type,
            })
        if sender_id not in self.config.get("allowed_open_ids", []):
            return False
        if chat_type == "group" and chat_id not in self.config.get("allowed_chat_ids", []):
            return False
        route=next((r for r in self.config.get('routes',[]) if r['chat_id']==chat_id),None)
        if route and not route.get('enabled',True): return False
        route=route or {'connection_id':self.config.get('connection_id'),
                       'prompt_template':self.config.get('prompt_template',DEFAULT_PROMPT_TEMPLATE),
                       'review_prompt_template':self.config.get('review_prompt_template',''),
                       'card_types':self.config.get('default_card_types',['voice_review'])}
        parent=self.inbox.linked_source(message.get('parent_id') or message.get('root_id'))
        if parent:
            if parent['chat_id']!=chat_id or parent['sender_id']!=sender_id: return False
            frozen=json.loads(parent.get('route_json') or '{}')
            route={**route,**frozen,'connection_id':parent.get('connection_id') or route.get('connection_id')}
        try:
            run_id=self.inbox.workflows.for_source(parent) if parent else None
            if run_id:
                prompt='用户补充消息（不是卡片审核决定）：'+body+'\n使用 codex-bridge-workflows 读取账本 '+run_id+'；仅处理当前阶段相关补充，不绕过等待审核，不重复执行后续阶段。'
            elif parent and review_template(self.config, parent):
                prompt = review_prompt(self.config, parent, self.inbox.original_source(parent), body, 'review_feedback')
            else:
                prompt = render_prompt(route['prompt_template'], body)
            if not route.get('workflow') and self.config.get('cards') and route.get('card_types'):
                context=('\n\n[Bridge 卡片工具上下文]\n来源消息 ID：'+message_id+
                         '\n可用卡片：'+', '.join(route['card_types'])+
                         '\n需要交互卡片时可调用 bridge_send_card；source_message_id 使用上述来源消息 ID。'
                         '\n若工具不可见，使用本机命令 '+str(Path(__file__).resolve().parent/'scripts/send-card')+
                         ' --source-message-id '+message_id+' --card-type 卡片标识 --variables-json JSON对象 --request-id 本次发卡唯一标识。'
                         '\n卡片清单和字段：'+str(Path(__file__).resolve().parent/'scripts/send-card')+' --list。'
                         '\n提交卡片后结束当前回复，等用户点击回传再继续。已用工具发卡时无需再输出 FEISHU_REVIEW_DOC_URL 标记。')
                prompt=render_prompt('{{message}}',prompt+context)
        except BridgeError:
            LOG.warning("Feishu message exceeds the Bridge prompt limit message_id=%s", message_id)
            return False
        return self.inbox.accept({
            "message_id": message_id, "chat_id": chat_id,
            "sender_id": sender_id, "text": body, "prompt": prompt,
            "parent_id": message.get("parent_id"), "root_id": message.get("root_id"),
            'connection_id':route.get('connection_id'),'app_id':self.config.get('app_id'),
            'chat_type':chat_type,
            'route_json':json.dumps({k:v for k,v in route.items() if k!='connection_key'},ensure_ascii=False),
        })

    def handle_card_action(self, payload):
        workflow=self.inbox.workflows.accept_callback(self.config,payload)
        if workflow:return workflow
        generic=self.inbox.cards.accept(self.config,payload)
        if generic: return generic
        event = payload.get("event") or {}
        action = event.get("action") or {}
        context = event.get("context") or {}
        operator_id = (event.get("operator") or {}).get("open_id")
        chat_id = context.get("open_chat_id")
        card_message_id = context.get("open_message_id")
        if (action.get("tag") != "button" or action.get("name") != "approve_voice_material"
                or not operator_id or operator_id not in self.config.get("allowed_open_ids", [])
                or not chat_id or not card_message_id):
            return "ignored"
        voice = selected_voice(action.get("form_value"))
        if not voice:
            return "invalid_voice"
        return self.inbox.accept_review(card_message_id, chat_id, operator_id, voice)

    def pump_once(self):
        for item in self.inbox.pending():
            if not self.inbox.workflows.dispatch_allowed(item):continue
            policy=json.loads(item.get('route_json') or '{}')
            if policy.get('workflow') and not (item.get('parent_id') or item.get('root_id')):
                self.inbox.workflows.start(item,policy['workflow'],self.config)
                item=self.inbox.cards.source(item['message_id'])
            parent_id = item["parent_id"] or item["root_id"]
            parent=self.inbox.linked_source(parent_id)
            parent_task,parent_known=(parent.get('task_id'),True) if parent else (None,False)
            if parent_known and not parent_task:
                continue
            request_id = "fs_" + hashlib.sha256(
                (self.config["app_id"] + ":" + item["message_id"]).encode()
            ).hexdigest()[:48]
            try:
                client=self.client_for(item.get('connection_id'))
                if parent_task and (self.inbox.cards.is_callback(item['message_id']) or review_template(self.config, item) or self.inbox.workflows.for_source(item)):
                    if client.task(parent_task).get('status') in ('执行中','等待 Codex','需要你处理'): continue
                result = client.submit(item["prompt"] or item["text"], request_id, parent_task)
                self.inbox.mark_submitted(item["message_id"], result["task_id"])
                self.inbox.cards.submitted(item['message_id'])
                LOG.info("Feishu message queued message_id=%s task_id=%s", item["message_id"], result["task_id"])
            except Exception:
                LOG.exception("Could not submit Feishu message_id=%s", item["message_id"])
        for item in self.inbox.submitted():
            try:
                task = self.client_for(item.get('connection_id')).task(item["task_id"])
                status = task.get("status")
                if status not in NOTIFY_STATUSES or status == item["notified_status"]:
                    continue
                result = str(task.get("result") or "")
                run_id=self.inbox.workflows.for_source(item)
                if run_id:
                    run=self.inbox.workflows.get(run_id)
                    current_mid=(run['source_id'] if run['epoch']==1 else 'wf_'+run_id+'_'+str(run['epoch']))
                    if run['state'] in ('waiting_review','checking_review') or (run['state']=='running' and item['message_id']!=current_mid):
                        self.inbox.mark_notified(item['message_id'],status,None)
                        continue
                    if run['state']=='running' and status in ('已完成','失败','已停止'):
                        run=self.inbox.workflows.act({'run_id':run_id,'revision':run['revision'],'action':'fail','reason':'Codex 本轮已结束，但没有提交通过校验的阶段产物；请查看原任务并恢复当前阶段。'})
                    labels={'completed':'全部完成','blocked':'需要处理','cancelled':'已取消','running':'处理中'}
                    body=run['definition']['name']+' · '+run['stage']['name']+'：'+labels.get(run['state'],run['state'])
                    if run['error']:body+='\n'+run['error']
                    outbound_id=self.sender.reply(run['source_id'],str(run['epoch'])+':'+run['state'],body)
                    self.inbox.mark_notified(item['message_id'],status,outbound_id)
                    continue
                doc_url = (review_doc_url(result) if status == "已完成"
                           and not self.inbox.has_review_card(item["message_id"])
                           and not self.inbox.cards.has_source(item['message_id']) else None)
                policy=json.loads(item.get('route_json') or '{}')
                if doc_url and 'voice_review' not in policy.get('card_types',self.config.get('default_card_types',['voice_review'])):
                    doc_url=None
                if doc_url:
                    if self.config.get('cards'):
                        self.inbox.cards.enqueue(self.config,item,'voice_review',{'review_doc_link':doc_url},'legacy_review_marker')
                        self.inbox.mark_notified(item['message_id'],status,None)
                        continue
                    outbound_id = self.sender.reply_review_card(item["message_id"], doc_url)
                    self.inbox.record_review_card(item, outbound_id, doc_url)
                    self.inbox.mark_notified(item["message_id"], status, outbound_id)
                    LOG.info("Feishu review card sent message_id=%s task_id=%s", item["message_id"], item["task_id"])
                    continue
                body = ("Codex 任务 " + item["task_id"] + "：" + status +
                        ("\n" + result[:3500] if result else ""))
                outbound_id = self.sender.reply(item["message_id"], status, body)
                self.inbox.mark_notified(item["message_id"], status, outbound_id)
                LOG.info("Feishu notified message_id=%s status=%s", item["message_id"], status)
            except Exception:
                LOG.exception("Could not notify Feishu message_id=%s", item["message_id"])
        for review in self.inbox.pending_reviews():
            request_id = "fs_review_" + hashlib.sha256(
                (self.config["app_id"] + ":" + review["card_message_id"]).encode()
            ).hexdigest()[:48]
            voice = {"voice": review["voice"], "material_url": review["material_url"]}
            try:
                source=self.inbox.cards.source(review['source_message_id'])
                client=self.client_for(source.get('connection_id'))
                if review_template(self.config, source) and client.task(review['task_id']).get('status') in ('执行中','等待 Codex','需要你处理'):
                    continue
                prompt=review_prompt(self.config,source,self.inbox.original_source(source),
                                     approval_prompt(review['doc_url'],voice),'card_approved')
                client.submit(prompt,
                                   request_id, review["task_id"])
                self.inbox.mark_review_submitted(review["card_message_id"])
                self.inbox.mark_notified(review["source_message_id"], None,
                                         review["card_message_id"])
                LOG.info("Feishu review submitted card=%s task_id=%s",
                         review["card_message_id"], review["task_id"])
            except Exception:
                LOG.exception("Could not submit Feishu review card=%s", review["card_message_id"])
        self.inbox.cards.deliver(self.config,self.sender)
        self.inbox.workflows.pump(self.config,self.sender)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--bridge-url", default="http://127.0.0.1:8797")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    config = load(args.config)
    if not config.get("enabled"):
        raise SystemExit("Feishu receiver is disabled")
    adapter = FeishuAdapter(
        config, Inbox(args.state_db),
        BridgeClient(args.bridge_url, config["connection_key"]),
        FeishuSender(config["app_id"], config["app_secret"]),
    )
    with adapter.inbox.db() as db:
        db.execute('UPDATE inbound SET connection_id=?,app_id=? WHERE connection_id IS NULL',
                   (config['connection_id'],config['app_id']))
        for chat in config.get('allowed_chat_ids',[]):
            db.execute("UPDATE inbound SET chat_type='group' WHERE chat_id=? AND chat_type IS NULL",(chat,))

    def pump():
        while True:
            try:
                adapter.pump_once()
            except Exception:
                LOG.exception("Feishu inbox pump failed")
            time.sleep(5)

    threading.Thread(target=pump, daemon=True).start()
    import lark_oapi as lark
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
    def on_message(data):
        adapter.handle_event(json.loads(lark.JSON.marshal(data)))

    def on_card_action(data):
        result = adapter.handle_card_action(json.loads(lark.JSON.marshal(data)))
        toast_text = {
            "accepted": "审核结果已接收，正在交给 Codex 处理",
            "already_submitted": "这张卡片已经提交过",
            "invalid_voice": "音色选项无效，请重新选择",
            "invalid_form": "提交的字段不符合卡片配置，请检查后重试",
            "stale_review": "这张卡片已过期，请使用当前轮次的审核卡",
            "unresolved_time_conflict": "当前版本仍有配音时长冲突，请提交修改意见或让 Codex 升级配音策略",
            "not_found": "未找到对应的待审核任务",
            "ignored": "无法处理此操作",
        }[result]
        return P2CardActionTriggerResponse({
            "toast": {"type": "success" if result in ("accepted", "already_submitted") else "error",
                      "content": toast_text}
        })

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message)
               .register_p2_card_action_trigger(on_card_action)
               .build())
    LOG.info("Starting Feishu long connection for app_id=%s", config["app_id"])
    lark.ws.Client(config["app_id"], config["app_secret"], event_handler=handler).start()


if __name__ == "__main__":
    main()
