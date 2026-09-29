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

LOG = logging.getLogger("codex_bridge.feishu")
NOTIFY_STATUSES = {"已完成", "失败", "已停止", "需要你处理"}


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

    def mark_notified(self, message_id, status, outbound_id):
        with self.db() as db:
            db.execute(
                "UPDATE inbound SET notified_status=?,outbound_id=?,updated_at=? WHERE message_id=?",
                (status, outbound_id, time.time(), message_id),
            )

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
        route = {'mode': 'prompt', 'connection_id': self.config.get('connection_id'),
                 'prompt_template': self.config.get('prompt_template', DEFAULT_PROMPT_TEMPLATE)}
        parent = self.inbox.linked_source(message.get('parent_id') or message.get('root_id'))
        if parent:
            if parent['chat_id'] != chat_id or parent['sender_id'] != sender_id:
                return False
            # Old tasks are read-only after the switch; start a fresh message instead.
            if not prompt_source(parent):
                return False
            route['connection_id'] = parent.get('connection_id') or route['connection_id']
        try:
            prompt = render_prompt(route['prompt_template'], body)
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

    def pump_once(self):
        for item in self.inbox.submitted():
            if not prompt_source(item):
                continue
            try:
                task = self.client_for(item.get('connection_id')).task(item['task_id'])
                status = task.get('status')
                if status not in NOTIFY_STATUSES or status == item['notified_status']:
                    continue
                result = str(task.get('result') or '')
                body = 'Codex 任务 ' + item['task_id'] + '：' + status + ('\n' + result[:3500] if result else '')
                outbound_id = self.sender.reply(item['message_id'], status, body)
                self.inbox.mark_notified(item['message_id'], status, outbound_id)
            except Exception:
                LOG.exception('Could not notify Feishu message_id=%s', item['message_id'])

        for item in self.inbox.pending():
            if not prompt_source(item):
                continue
            parent_id = item['parent_id'] or item['root_id']
            parent = self.inbox.linked_source(parent_id)
            parent_task = parent.get('task_id') if parent else None
            if parent and not parent_task:
                continue
            request_id = 'fs_' + hashlib.sha256(
                (item.get('app_id') + ':' + item['message_id']).encode()).hexdigest()[:48]
            try:
                client = self.client_for(item.get('connection_id'))
                # Serialize follow-ups on the same task, while other tasks can run.
                if parent_task and client.task(parent_task).get('status') in ('执行中', '等待 Codex', '需要你处理'):
                    continue
                result = client.submit(item['prompt'] or item['text'], request_id, parent_task)
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
    import lark_oapi as lark
    def on_message(data):
        adapter.handle_event(json.loads(lark.JSON.marshal(data)))

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message)
               .build())
    LOG.info("Starting Feishu long connection for app_id=%s", config["app_id"])
    lark.ws.Client(config["app_id"], config["app_secret"], event_handler=handler).start()


if __name__ == "__main__":
    main()
