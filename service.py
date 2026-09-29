#!/usr/bin/env python3
"""Codex Bridge MVP: local transport, durable task inbox, and V3.2 panel."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import resource
import secrets
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from codex_http_bridge import AppServerClient, BridgeError, RpcError, Bridge
from feishu_settings import load as load_feishu_settings, public as public_feishu_settings
from feishu_settings import complete_pairing, save as save_feishu_settings, start_pairing
from feishu_probe import probe as probe_feishu
from feishu_supervisor import FeishuSupervisor
from feishu_worker import Inbox
from legacy_history import LegacyHistory

LOG = logging.getLogger("codex_bridge")
ROOT = Path(__file__).resolve().parent
STATUSES = {"等待 Codex", "执行中", "需要你处理", "已完成", "失败", "已停止"}
MAX_BODY = 16 * 1024
CODEX_APPROVAL_POLICY = "never"
CODEX_SANDBOX_MODE = "danger-full-access"
CODEX_REASONING_EFFORT = "medium"
MAX_ACTIVE_TASKS = 2
TARGET_NOFILE_LIMIT = 4096


def now():
    return time.time()


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def raise_nofile_limit(target=TARGET_NOFILE_LIMIT):
    """Raise this service's soft limit before launching its Codex app-server."""
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        desired = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if soft < desired:
            resource.setrlimit(resource.RLIMIT_NOFILE, (desired, hard))
            soft = desired
        LOG.info("file descriptor limit soft=%s hard=%s", soft, hard)
    except (OSError, ValueError) as exc:
        LOG.warning("could not raise file descriptor limit: %s", exc)


class LocalCodexTransport:
    """Reuses the verified HTTP Bridge App Server client and readback rule."""
    def __init__(self, client=None, verify_seconds=90):
        self.client = client or AppServerClient()
        self.verify_seconds = verify_seconds

    def start_thread(self, cwd):
        response = self.client.request("thread/start", {
            "cwd": cwd, "approvalPolicy": CODEX_APPROVAL_POLICY, "sandbox": CODEX_SANDBOX_MODE,
            "ephemeral": False, "serviceName": "codex_bridge",
        })
        thread_id = response.get("thread", {}).get("id")
        if not thread_id:
            raise RuntimeError("Codex did not return a thread ID")
        return thread_id

    def send(self, thread_id, message):
        response = self.client.request("turn/start", {
            "threadId": thread_id, "input": [{"type": "text", "text": message}],
            "approvalPolicy": CODEX_APPROVAL_POLICY,
            "sandboxPolicy": {"type": "dangerFullAccess"},
            "effort": CODEX_REASONING_EFFORT,
        })
        turn_id = response.get("turn", {}).get("id")
        deadline = time.monotonic() + self.verify_seconds
        logged = False
        while True:
            try:
                thread = self.read(thread_id)
                if Bridge._message_seen(thread, message):
                    return turn_id
                if not logged:
                    LOG.info("awaiting Codex readback thread=%s turns=%s status=%s", thread_id,
                             len(thread.get("turns") or []), thread.get("status"))
                    logged = True
            except (RpcError, BridgeError) as exc:
                if not logged:
                    LOG.info("Codex readback pending thread=%s reason=%s", thread_id, type(exc).__name__)
                    logged = True
            if time.monotonic() >= deadline:
                raise RuntimeError("Codex accepted the message but readback is uncertain")
            time.sleep(0.4)

    def read(self, thread_id):
        return self.client.request("thread/read", {"threadId": thread_id, "includeTurns": True}).get("thread", {})

    def resume_thread(self, thread_id):
        self.client.request("thread/resume", {
            "threadId": thread_id,
            "approvalPolicy": CODEX_APPROVAL_POLICY,
            "sandbox": CODEX_SANDBOX_MODE,
        })

    def release_idle(self):
        # A loaded app-server thread holds the writer lock, preventing CLI resume.
        self.client.close()

    def close(self):
        self.client.close()


class Store:
    def __init__(self, db_path, projects_path, codex_state_path=None):
        self.db_path = str(db_path)
        self.projects_path = Path(projects_path)
        self.codex_state_path = Path(codex_state_path) if codex_state_path else None
        self._project_discovery_warning_at = 0.0
        self.history = LegacyHistory(Path(db_path).parent / "feishu-inbox.sqlite3")
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _init(self):
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS connections (
              id TEXT PRIMARY KEY, source TEXT NOT NULL, project_id TEXT NOT NULL,
              api_key TEXT NOT NULL, api_key_hash TEXT UNIQUE NOT NULL,
              status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY, connection_id TEXT NOT NULL REFERENCES connections(id),
              thread_id TEXT, status TEXT NOT NULL, result TEXT, error_code TEXT,
              created_at REAL NOT NULL, updated_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
              connection_id TEXT NOT NULL REFERENCES connections(id),
              request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
              content TEXT NOT NULL, delivery TEXT NOT NULL, turn_id TEXT,
              created_at REAL NOT NULL, updated_at REAL NOT NULL,
              UNIQUE(connection_id,request_id));
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS approvals (
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
              method TEXT NOT NULL, detail TEXT NOT NULL, state TEXT NOT NULL,
              created_at REAL NOT NULL, updated_at REAL NOT NULL);
            """)
            # An interrupted send cannot be replayed safely without a Codex readback.
            db.execute("UPDATE messages SET delivery='uncertain' WHERE delivery='sending'")
            db.execute("UPDATE tasks SET status='需要你处理', error_code='delivery_uncertain' WHERE id IN (SELECT task_id FROM messages WHERE delivery='uncertain')")
            db.execute("UPDATE approvals SET state='unavailable' WHERE state='pending'")
            db.execute("""UPDATE tasks SET status='需要你处理',
                result='Codex 审批连接已中断，请在 Codex 中查看任务并重新发起需要审批的操作。',
                error_code='approval_unavailable' WHERE id IN
                (SELECT task_id FROM approvals WHERE state='unavailable')""")
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def _codex_projects(self):
        """Read local Codex projects on each request so new projects appear immediately."""
        state_path = self.codex_state_path
        if state_path is None:
            codex_home = Path(os.getenv("CODEX_HOME", Path.home() / ".codex"))
            candidates = sorted(codex_home.glob("state_*.sqlite"),
                                key=lambda p: int(p.stem.split("_")[-1]) if p.stem.split("_")[-1].isdigit() else -1)
            state_path = candidates[-1] if candidates else None
        if state_path is None or not state_path.is_file():
            return []
        try:
            with sqlite3.connect(state_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as db:
                projects = db.execute("""SELECT p.id, p.name, r.path FROM projects p
                    JOIN project_roots r ON r.project_id=p.id AND r.position=0
                    ORDER BY p.position, p.created_at_ms""").fetchall()
                self._project_discovery_warning_at = 0.0
                return projects
        except sqlite3.Error as exc:
            if now() - self._project_discovery_warning_at >= 60:
                LOG.warning("Codex project discovery unavailable at %s: %s", state_path, exc)
                self._project_discovery_warning_at = now()
            return []

    def projects(self):
        configured = json.loads(self.projects_path.read_text(encoding="utf-8")) if self.projects_path.exists() else {}
        result = []
        paths = set()
        for project_id, path in configured.items():
            # Keep inaccessible configured projects visible so their path can be repaired.
            result.append({"id": project_id, "name": Path(path).name, "path": path})
            try:
                paths.add(str(Path(path).resolve()))
            except OSError:
                paths.add(path)
        for project_id, name, path in self._codex_projects():
            if project_id in configured or not Path(path).is_dir():
                continue
            resolved = str(Path(path).resolve())
            if resolved in paths:
                continue
            result.append({"id": project_id, "name": name, "path": path})
            paths.add(resolved)
        return result

    def project(self, project_id):
        configured = json.loads(self.projects_path.read_text(encoding="utf-8")) if self.projects_path.exists() else {}
        if project_id in configured:
            path = configured[project_id]
            item = {"id": project_id, "name": Path(path).name, "path": path}
        else:
            item = next((p for p in self.projects() if p["id"] == project_id), None)
        if item:
            try:
                mode = Path(item["path"]).stat().st_mode
            except OSError as exc:
                raise BridgeError(503, "project_unavailable", "项目目录暂时无法访问") from exc
            if not stat.S_ISDIR(mode):
                raise BridgeError(503, "project_unavailable", "项目位置不是文件夹")
            return item
        raise BridgeError(404, "project_unavailable", "项目位置不可用，请重新选择项目")

    def update_project_path(self, project_id, path):
        if not isinstance(path, str) or not Path(path).is_absolute() or not Path(path).is_dir():
            raise BridgeError(400, "invalid_project_path", "请选择现有的项目文件夹")
        raw = json.loads(self.projects_path.read_text(encoding="utf-8"))
        if project_id not in raw and not any(item[0] == project_id for item in self._codex_projects()):
            with self.db() as db:
                connected = db.execute("SELECT 1 FROM connections WHERE project_id=? LIMIT 1", (project_id,)).fetchone()
            if not connected:
                raise BridgeError(404, "project_not_found", "项目不存在")
        raw[project_id] = str(Path(path).resolve(strict=True))
        temp = self.projects_path.with_suffix(".tmp")
        temp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(self.projects_path)
        return self.project(project_id)

    def create_connection(self, source, project_id):
        if not isinstance(source, str) or not 1 <= len(source.strip()) <= 60:
            raise BridgeError(400, "invalid_source", "请填写任务来源")
        self.project(project_id)
        cid = "conn_" + uuid.uuid4().hex[:16]
        key = "cb_" + secrets.token_urlsafe(36)
        t = now()
        with self.db() as db:
            db.execute("INSERT INTO connections VALUES (?,?,?,?,?,?,?,?)",
                       (cid, source.strip(), project_id, key, digest(key), "待验证", t, t))
        return self.connection(cid, reveal=True)

    def connection(self, cid, reveal=False):
        with self.db() as db:
            row = db.execute("SELECT * FROM connections WHERE id=?", (cid,)).fetchone()
        if not row:
            raise BridgeError(404, "connection_not_found", "连接不存在")
        obj = dict(row)
        obj["project_name"] = next((p["name"] for p in self.projects() if p["id"] == obj["project_id"]), obj["project_id"])
        obj.pop("api_key_hash", None)
        if not reveal:
            obj.pop("api_key", None)
        return obj

    def connections(self):
        with self.db() as db:
            ids = [r["id"] for r in db.execute("SELECT id FROM connections ORDER BY created_at DESC")]
        return [self.connection(cid) for cid in ids]

    def authenticate(self, bearer):
        if not bearer.startswith("Bearer "):
            raise BridgeError(401, "unauthorized", "连接密钥无效")
        key_hash = digest(bearer[7:])
        with self.db() as db:
            row = db.execute("SELECT * FROM connections WHERE api_key_hash=?", (key_hash,)).fetchone()
        if not row or not hmac.compare_digest(row["api_key_hash"], key_hash):
            raise BridgeError(401, "unauthorized", "连接密钥无效")
        if row["status"] == "已停用":
            raise BridgeError(403, "connection_disabled", "连接已停用")
        return dict(row)

    def update_connection(self, cid, payload):
        status = payload.get("status")
        if status not in ("已停用", "待验证", "可用") or set(payload) != {"status"}:
            raise BridgeError(400, "invalid_status", "连接状态无效")
        with self.db() as db:
            if status == "待验证" and db.execute(
                "SELECT 1 FROM messages WHERE connection_id=? AND delivery='delivered' LIMIT 1", (cid,)
            ).fetchone():
                status = "可用"
            if db.execute("UPDATE connections SET status=?,updated_at=? WHERE id=?", (status, now(), cid)).rowcount == 0:
                raise BridgeError(404, "connection_not_found", "连接不存在")
        return self.connection(cid)

    def submit(self, connection, payload, request_id):
        if not isinstance(payload, dict) or set(payload) - {"task_id", "message", "request_id"}:
            raise BridgeError(400, "invalid_request", "任务内容格式无效")
        message = payload.get("message")
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= 12000:
            raise BridgeError(400, "invalid_message", "任务内容需为 1 到 12000 字")
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 128 or not all(x.isalnum() or x in "-_.:" for x in request_id):
            raise BridgeError(400, "invalid_request_id", "request_id 需为 8 到 128 位字母、数字或 -_.:")
        requested_task = payload.get("task_id")
        if requested_task is not None and (not isinstance(requested_task, str) or len(requested_task) > 100):
            raise BridgeError(400, "invalid_task_id", "task_id 无效")
        if requested_task and requested_task in self.history.tasks():
            raise BridgeError(409, "historical_task", "旧版任务仅供查看，请从飞书发送新消息创建任务")
        fingerprint = digest(json.dumps([requested_task, message], ensure_ascii=False))
        cid = connection["id"]
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM messages WHERE connection_id=? AND request_id=?", (cid, request_id)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise BridgeError(409, "idempotency_conflict", "request_id 已用于另一条消息")
                task = db.execute("SELECT * FROM tasks WHERE id=?", (existing["task_id"],)).fetchone()
                return 200, {"task_id": task["id"], "status": task["status"], "duplicate": True}
            if requested_task:
                task = db.execute("SELECT * FROM tasks WHERE id=? AND connection_id=?", (requested_task, cid)).fetchone()
                if not task:
                    raise BridgeError(404, "task_not_found", "这个连接中没有该任务")
                task_id = task["id"]
            else:
                task_id = "task_" + uuid.uuid4().hex[:20]
                t = now()
                db.execute("INSERT INTO tasks VALUES (?,?,NULL,'等待 Codex',NULL,NULL,?,?)", (task_id, cid, t, t))
            t = now()
            db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,'queued',NULL,?,?)",
                       ("msg_" + uuid.uuid4().hex[:20], task_id, cid, request_id, fingerprint, message.strip(), t, t))
            db.execute("UPDATE tasks SET status='等待 Codex',result=NULL,error_code=NULL,updated_at=? WHERE id=?", (t, task_id))
        return 202, {"task_id": task_id, "status": "等待 Codex", "duplicate": False}

    def tasks(self):
        with self.db() as db:
            rows = db.execute("""SELECT t.*, c.source,c.project_id FROM tasks t JOIN connections c ON c.id=t.connection_id ORDER BY t.updated_at DESC""").fetchall()
            first = {r["task_id"]:r["content"] for r in db.execute("SELECT task_id,content FROM messages ORDER BY created_at DESC")}
        names = {p["id"]:p["name"] for p in self.projects()}
        archived = self.history.tasks()
        return [{**dict(r), **archived.get(r["id"], {}), "title": first.get(r["id"], ""), "project_name": names.get(r["project_id"], r["project_id"])} for r in rows]

    def task(self, task_id, connection_id=None):
        with self.db() as db:
            if connection_id:
                row = db.execute("SELECT t.*,c.source,c.project_id FROM tasks t JOIN connections c ON c.id=t.connection_id WHERE t.id=? AND t.connection_id=?", (task_id, connection_id)).fetchone()
            else:
                row = db.execute("SELECT t.*,c.source,c.project_id FROM tasks t JOIN connections c ON c.id=t.connection_id WHERE t.id=?", (task_id,)).fetchone()
            messages = [dict(r) for r in db.execute("SELECT id,content,delivery,created_at,turn_id FROM messages WHERE task_id=? ORDER BY created_at", (task_id,))]
            approval = db.execute("SELECT id,method,detail,state FROM approvals WHERE task_id=? ORDER BY created_at DESC LIMIT 1", (task_id,)).fetchone()
        if not row:
            raise BridgeError(404, "task_not_found", "任务不存在")
        obj = {**dict(row), **self.history.tasks().get(task_id, {})}
        obj["messages"] = messages
        obj["approval"] = dict(approval) if approval and approval["state"] == "pending" else None
        obj["title"] = messages[0]["content"] if messages else ""
        obj["project_name"] = next((p["name"] for p in self.projects() if p["id"] == obj["project_id"]), obj["project_id"])
        return obj

    def add_approval(self, thread_id, method, detail):
        with self.db() as db:
            task = db.execute("SELECT id FROM tasks WHERE thread_id=? ORDER BY created_at DESC LIMIT 1", (thread_id,)).fetchone()
            if not task:
                return None
            approval_id = "approval_" + uuid.uuid4().hex[:20]
            t = now()
            db.execute("INSERT INTO approvals VALUES (?,?,?,?,'pending',?,?)",
                       (approval_id, task["id"], method, detail[:2000], t, t))
            db.execute("UPDATE tasks SET status='需要你处理',result=?,error_code='approval_required',updated_at=? WHERE id=?",
                       ("Codex 正在等待你确认操作。" + (" " + detail[:500] if detail else ""), t, task["id"]))
        return approval_id

    def approval(self, approval_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if not row:
            raise BridgeError(404, "approval_not_found", "待处理操作不存在")
        return dict(row)

    def resolve_approval(self, approval_id):
        with self.db() as db:
            if db.execute("UPDATE approvals SET state='resolved',updated_at=? WHERE id=? AND state='pending'", (now(), approval_id)).rowcount == 0:
                raise BridgeError(409, "approval_unavailable", "待处理操作已失效")

    def set_task(self, task_id, status, result=None, error_code=None):
        if status not in STATUSES:
            raise BridgeError(400, "invalid_status", "任务状态无效")
        with self.db() as db:
            if db.execute("UPDATE tasks SET status=?,result=COALESCE(?,result),error_code=?,updated_at=? WHERE id=?",
                          (status, result, error_code, now(), task_id)).rowcount == 0:
                raise BridgeError(404, "task_not_found", "任务不存在")
        return self.task(task_id)


class Engine:
    def __init__(self, store, transport=None, interval=2, max_active=MAX_ACTIVE_TASKS, history_inbox_path=None):
        if max_active < 1:
            raise ValueError("max_active must be positive")
        self.store = store
        self.transport = transport or LocalCodexTransport()
        self.interval = interval
        self.max_active = max_active
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.approval_lock = threading.Lock()
        self.pending_approvals = {}
        self.worker = None
        self.history = LegacyHistory(history_inbox_path) if history_inbox_path else store.history
        self.archived_tasks = {}
        if hasattr(self.transport, "client"):
            self.transport.client.on_server_request = self._on_server_request

    def _on_server_request(self, request):
        method = request.get("method")
        if method not in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            return False
        params = request.get("params") or {}
        detail = params.get("reason") or params.get("command") or "Codex 请求确认一个操作。"
        approval_id = self.store.add_approval(params.get("threadId"), method, str(detail))
        if not approval_id:
            return False
        with self.approval_lock:
            self.pending_approvals[approval_id] = request["id"]
        LOG.info("approval pending id=%s thread=%s", approval_id, params.get("threadId"))
        return True

    def resolve_approval(self, approval_id, decision):
        if decision not in ("accept", "decline"):
            raise BridgeError(400, "invalid_decision", "请选择允许或拒绝")
        approval = self.store.approval(approval_id)
        if approval["state"] != "pending":
            raise BridgeError(409, "approval_unavailable", "待处理操作已失效")
        with self.approval_lock:
            rpc_id = self.pending_approvals.get(approval_id)
            if rpc_id is None:
                raise BridgeError(409, "approval_unavailable", "Codex 已断开，请重新打开任务")
            try:
                self.transport.client._write({"id": rpc_id, "result": {"decision": decision}})
            except Exception as exc:
                raise BridgeError(503, "codex_unavailable", "Codex 暂时未连接") from exc
            self.pending_approvals.pop(approval_id, None)
        self.store.resolve_approval(approval_id)
        self.store.set_task(approval["task_id"], "执行中", "Codex 已收到你的决定。", None)
        return self.store.task(approval["task_id"])

    def start(self):
        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                LOG.exception("background sync failed")
            self.stop.wait(self.interval)

    def tick(self):
        if not self.lock.acquire(False):
            return
        try:
            self.archived_tasks = self.history.tasks()
            self._refresh()
            if self._codex_desktop_open():
                self._deliver_one()
        finally:
            self.lock.release()

    @staticmethod
    def _codex_desktop_open():
        if os.getenv("BRIDGE_REQUIRE_APP", "0") != "1" or sys.platform != "darwin":
            return True
        try:
            process_list = subprocess.check_output(["ps", "-axo", "command="], text=True, timeout=3)
            return any(line.strip().startswith((
                "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
                "/Applications/Codex.app/Contents/MacOS/Codex",
            )) for line in process_list.splitlines())
        except (OSError, subprocess.SubprocessError):
            return False

    def _refresh(self):
        with self.store.db() as db:
            rows = db.execute("""SELECT id,thread_id,status FROM tasks WHERE thread_id IS NOT NULL
                AND error_code IS NOT 'workflow_blocked' AND
                (status IN ('执行中','需要你处理') OR
                 (status='等待 Codex' AND EXISTS (SELECT 1 FROM messages WHERE task_id=tasks.id AND delivery='superseded')
                  AND NOT EXISTS (SELECT 1 FROM messages WHERE task_id=tasks.id AND delivery IN ('queued','sending','uncertain'))))""").fetchall()
        for row in rows:
            if row["id"] in self.archived_tasks:
                continue
            try:
                thread = self.transport.read(row["thread_id"])
            except Exception:
                continue
            turns = thread.get("turns") or []
            if not turns:
                continue
            last = turns[-1]
            state = last.get("status")
            with self.store.db() as db:
                expected = db.execute("""SELECT turn_id FROM messages WHERE task_id=?
                    AND delivery='delivered' ORDER BY created_at DESC LIMIT 1""", (row['id'],)).fetchone()
            if expected and expected['turn_id'] and last.get('id') and last['id'] != expected['turn_id']:
                # Disk readback may lag a newly started desktop turn. Never let
                # the preceding completed turn finish the new stage.
                continue
            if state == 'interrupted' and (thread.get('status') or {}).get('type') == 'notLoaded':
                # Another app-server owns the live thread. Its partial persisted
                # history is not authoritative evidence of an interruption.
                continue
            items = last.get("items") or []
            final = next((x.get("text") for x in reversed(items) if x.get("type") == "agentMessage" and x.get("text")), None)
            if state == "completed":
                self.store.set_task(row["id"], "已完成", final)
            elif state == "failed":
                self.store.set_task(row["id"], "失败", str(last.get("error") or "Codex 执行失败"), "codex_failed")
            elif state == "interrupted":
                self.store.set_task(row["id"], "已停止")
            elif state in ("inProgress", "in_progress") and row["status"] != "需要你处理":
                self.store.set_task(row["id"], "执行中")
        with self.store.db() as db:
            active = any(r[0] not in self.archived_tasks for r in db.execute("SELECT id FROM tasks WHERE status IN ('执行中','需要你处理')"))
        if not active:
            release = getattr(self.transport, "release_idle", None)
            if release:
                release()

    def _deliver_one(self):
        with self.store.db() as db:
            active = sum(r[0] not in self.archived_tasks for r in db.execute("SELECT id FROM tasks WHERE status IN ('执行中','需要你处理')"))
            if active >= self.max_active:
                return
            rows = db.execute("""SELECT m.*,t.thread_id,t.status AS task_status,c.project_id
                FROM messages m JOIN tasks t ON t.id=m.task_id JOIN connections c ON c.id=t.connection_id
                WHERE m.delivery='queued' AND c.status!='已停用'
                AND NOT EXISTS (SELECT 1 FROM messages older WHERE older.task_id=m.task_id AND older.created_at<m.created_at AND older.delivery IN ('queued','sending','uncertain'))
                ORDER BY m.created_at""").fetchall()
        for row in rows:
            if row['task_id'] in self.archived_tasks:
                continue
            if self._deliver_message(dict(row)):
                return

    def _deliver_message(self, msg):
        if msg["thread_id"] and msg["task_status"] in ("执行中", "需要你处理"):
            return False
        try:
            project = self.store.project(msg["project_id"])
            cwd = str(Path(project["path"]).resolve(strict=True))
        except BridgeError as exc:
            if exc.code != "project_unavailable":
                self._delivery_error(msg, exc.code, str(exc), uncertain=False)
                return True
            self._wait_for_project(msg, exc)
            return False
        except OSError as exc:
            self._wait_for_project(msg, exc)
            return False
        if msg["thread_id"]:
            try:
                turns = self.transport.read(msg["thread_id"]).get("turns") or []
                if turns and turns[-1].get("status") in ("inProgress", "in_progress"):
                    return False
                resume = getattr(self.transport, "resume_thread", None)
                if resume:
                    resume(msg["thread_id"])
            except Exception as exc:
                code = 'codex_thread_owned' if 'active writer' in str(exc) else 'codex_resume_unavailable'
                detail = ('原 Codex 任务正由桌面端持有，Bridge 暂时无法续接；请由当前 Codex 任务接续队列。'
                          if code == 'codex_thread_owned' else '暂时无法恢复原 Codex 任务，Bridge 正在重试。')
                with self.store.db() as db:
                    old = db.execute('SELECT error_code FROM tasks WHERE id=?', (msg['task_id'],)).fetchone()
                    if old and old['error_code'] != code:
                        db.execute("UPDATE tasks SET result=?,error_code=?,updated_at=? WHERE id=?", (detail, code, now(), msg['task_id']))
                        LOG.warning('resume deferred task=%s code=%s reason=%s', msg['task_id'], code, type(exc).__name__)
                return False
        try:
            with self.store.db() as db:
                claimed = db.execute("UPDATE messages SET delivery='sending',updated_at=? WHERE id=? AND delivery='queued'", (now(), msg["id"])).rowcount
                if not claimed:
                    return False
            thread_id = msg["thread_id"]
            if not thread_id:
                thread_id = self.transport.start_thread(cwd)
                with self.store.db() as db:
                    db.execute("UPDATE tasks SET thread_id=?,updated_at=? WHERE id=?", (thread_id, now(), msg["task_id"]))
            turn_id = self.transport.send(thread_id, msg["content"])
            with self.store.db() as db:
                db.execute("UPDATE messages SET delivery='delivered',turn_id=?,updated_at=? WHERE id=?", (turn_id, now(), msg["id"]))
                db.execute("""UPDATE tasks SET
                    status=CASE WHEN EXISTS (SELECT 1 FROM approvals WHERE task_id=? AND state='pending')
                        THEN '需要你处理' ELSE '执行中' END,
                    error_code=CASE WHEN EXISTS (SELECT 1 FROM approvals WHERE task_id=? AND state='pending')
                        THEN 'approval_required' ELSE NULL END,
                    result=NULL,
                    updated_at=? WHERE id=?""", (msg["task_id"], msg["task_id"], now(), msg["task_id"]))
                db.execute("UPDATE connections SET status='可用',updated_at=? WHERE id=? AND status='待验证'", (now(), msg["connection_id"]))
            LOG.info("delivered task=%s thread=%s", msg["task_id"], thread_id)
        except BridgeError as exc:
            self._delivery_error(msg, exc.code, str(exc), uncertain=exc.code in ("codex_timeout", "delivery_uncertain"))
        except RpcError as exc:
            self._delivery_error(msg, "codex_rejected", str(exc), uncertain=bool(msg["thread_id"]))
        except Exception as exc:
            LOG.exception("delivery exception task=%s", msg["task_id"])
            self._delivery_error(msg, "delivery_uncertain", str(exc), uncertain=True)
        return True

    def _wait_for_project(self, msg, exc):
        message = "项目暂时无法访问，Bridge 正在重试；如果目录已移动，请在连接设置中重新选择项目。"
        with self.store.db() as db:
            task = db.execute("SELECT status,result,error_code FROM tasks WHERE id=?", (msg["task_id"],)).fetchone()
            if task and (task["status"], task["result"], task["error_code"]) != ("等待 Codex", message, "project_unavailable"):
                db.execute("UPDATE tasks SET status='等待 Codex',result=?,error_code='project_unavailable',updated_at=? WHERE id=?",
                           (message, now(), msg["task_id"]))
                cause = exc.__cause__ if exc.__cause__ else exc
                LOG.warning("project unavailable; delivery deferred task=%s project=%s error=%s",
                            msg["task_id"], msg["project_id"], cause)

    def _delivery_error(self, msg, code, detail, uncertain):
        LOG.warning("delivery failed task=%s code=%s", msg["task_id"], code)
        with self.store.db() as db:
            db.execute("UPDATE messages SET delivery=?,updated_at=? WHERE id=?", ("uncertain" if uncertain else "failed", now(), msg["id"]))
        if code == "project_unavailable":
            self.store.set_task(msg["task_id"], "失败", "项目位置发生变化，请重新选择项目。", code)
        elif uncertain:
            self.store.set_task(msg["task_id"], "需要你处理", "交付结果未能确认，请到 Codex 查看该任务。", code)
        else:
            self.store.set_task(msg["task_id"], "失败", detail, code)

    def close(self):
        self.stop.set()
        if self.worker:
            self.worker.join(timeout=2)
        self.transport.close()


def make_handler(store, engine, base_url, allowed_host=None,
                 feishu_settings_path=None, feishu_supervisor=None, feishu_inbox_path=None):
    static = ROOT / "static"
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            LOG.info("http %s", args[1] if len(args) > 1 else "?")

        def send_json(self, status, obj, headers=None):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def read_json(self, limit=MAX_BODY):
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                raise BridgeError(415, "unsupported_media_type", "需要 JSON 内容")
            try:
                size = int(self.headers.get("Content-Length", ""))
            except ValueError:
                raise BridgeError(411, "length_required", "缺少请求长度")
            if not 0 < size <= limit:
                raise BridgeError(413, "body_too_large", "任务内容过长")
            try:
                return json.loads(self.rfile.read(size))
            except (ValueError, UnicodeDecodeError):
                raise BridgeError(400, "invalid_json", "JSON 内容无效")

        def serve_file(self, path, mime):
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def route(self):
            path = urlsplit(self.path).path
            try:
                allowed_hosts = ({allowed_host} if allowed_host else
                                 {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"})
                if self.headers.get("Host") not in allowed_hosts:
                    raise BridgeError(403, "host_forbidden", "来源无效")
                # Reject cross-origin browser writes, including on the LAN listener.
                if self.command in ("POST", "PATCH") and path.startswith("/api/"):
                    origin = self.headers.get("Origin")
                    if origin and origin != base_url:
                        raise BridgeError(403, "origin_forbidden", "来源无效")
                if self.command == "GET" and path in ("/", "/panel"):
                    return self.serve_file(static / "index.html", "text/html; charset=utf-8")
                if self.command == "GET" and path in ("/static/style.css", "/static/app.js"):
                    mime = "text/css; charset=utf-8" if path.endswith("css") else "application/javascript; charset=utf-8"
                    return self.serve_file(static / path.split("/")[-1], mime)
                if path == "/healthz" and self.command == "GET":
                    return self.send_json(200, {"status": "ok"})
                if path.startswith("/api/feishu") and self.command in ("GET", "PATCH", "POST"):
                    if allowed_host is not None or self.client_address[0] not in ("127.0.0.1", "::1"):
                        raise BridgeError(403, "local_only", "飞书设置只能在本机修改")
                    if feishu_settings_path is None:
                        raise BridgeError(503, "feishu_unavailable", "飞书设置未启用")
                    if path == "/api/feishu" and self.command == "PATCH":
                        save_feishu_settings(feishu_settings_path, self.read_json(), store)
                    elif path == "/api/feishu/setup" and self.command == "POST":
                        data = self.read_json()
                        if not isinstance(data, dict) or set(data) - {"app_id", "app_secret", "project_id"}:
                            raise BridgeError(400, "invalid_request", "无效的飞书快速配置")
                        project_id = data.get("project_id")
                        store.project(project_id)
                        current = load_feishu_settings(feishu_settings_path)
                        app_id = data.get("app_id")
                        app_secret = data.get("app_secret") or (
                            current.get("app_secret") if app_id == current.get("app_id") else None)
                        checked = probe_feishu(app_id, app_secret)
                        old_cid = current.get("connection_id")
                        old_connection = store.connection(old_cid) if old_cid else None
                        connection_id = (old_cid if old_connection and old_connection["project_id"] == project_id
                                         else store.create_connection("飞书机器人", project_id)["id"])
                        changed_app = app_id != current.get("app_id")
                        save_feishu_settings(feishu_settings_path, {
                            "app_id": app_id, "app_secret": app_secret,
                            "connection_id": connection_id, "enabled": False,
                            **({"allowed_open_ids": [], "allowed_chat_ids": []} if changed_app else {}),
                        }, store, internal={
                            "bot_open_id": checked.get("bot_open_id", ""),
                            "bot_name": checked.get("bot_name", ""),
                            "bot_ready": checked["bot_ready"],
                            "pairing_code": "", "pairing_expires_at": 0,
                        })
                    elif path == "/api/feishu/check" and self.command == "POST":
                        config = load_feishu_settings(feishu_settings_path)
                        checked = probe_feishu(config.get("app_id"), config.get("app_secret"))
                        save_feishu_settings(feishu_settings_path, {}, store, internal={
                            "bot_open_id": checked.get("bot_open_id", ""),
                            "bot_name": checked.get("bot_name", ""),
                            "bot_ready": checked["bot_ready"],
                        })
                    elif path == "/api/feishu/pairing" and self.command == "POST":
                        data = self.read_json()
                        if not isinstance(data, dict) or set(data) != {"action"}:
                            raise BridgeError(400, "invalid_request", "无效的配对操作")
                        if data["action"] == "start":
                            start_pairing(feishu_settings_path, store)
                        elif data["action"] == "confirm":
                            config = load_feishu_settings(feishu_settings_path)
                            candidate = (Inbox(feishu_inbox_path).latest_pairing(config.get("pairing_code"))
                                         if feishu_inbox_path and Path(feishu_inbox_path).exists() else None)
                            if not candidate:
                                raise BridgeError(409, "pairing_not_received", "尚未收到配对消息")
                            complete_pairing(feishu_settings_path, store, candidate)
                        else:
                            raise BridgeError(400, "invalid_request", "无效的配对操作")
                    elif path != "/api/feishu" or self.command != "GET":
                        raise BridgeError(404, "not_found", "页面不存在")
                    config = load_feishu_settings(feishu_settings_path)
                    result = public_feishu_settings(config)
                    if (config.get("pairing_code") and feishu_inbox_path
                            and Path(feishu_inbox_path).exists()):
                        result["pairing_candidate"] = Inbox(feishu_inbox_path).latest_pairing(
                            config["pairing_code"])
                    result["runtime"] = feishu_supervisor.status() if feishu_supervisor else "未启动"
                    return self.send_json(200, result)
                if path == "/api/projects" and self.command == "GET":
                    return self.send_json(200, store.projects())
                if path.startswith("/api/projects/") and self.command == "PATCH":
                    data = self.read_json()
                    if not isinstance(data, dict) or set(data) != {"path"}:
                        raise BridgeError(400, "invalid_request", "无效字段")
                    return self.send_json(200, store.update_project_path(path.rsplit("/", 1)[-1], data["path"]))
                if path == "/api/connections" and self.command == "GET":
                    return self.send_json(200, store.connections())
                if path == "/api/connections" and self.command == "POST":
                    data = self.read_json()
                    return self.send_json(201, store.create_connection(data.get("source"), data.get("project_id")))
                if path.startswith("/api/connections/"):
                    cid = path.rsplit("/", 1)[-1]
                    if self.command == "GET":
                        obj = store.connection(cid, reveal=True)
                        obj["endpoint"] = base_url + "/v1/tasks"
                        return self.send_json(200, obj)
                    if self.command == "PATCH":
                        return self.send_json(200, store.update_connection(cid, self.read_json()))
                if path == "/api/tasks" and self.command == "GET":
                    return self.send_json(200, store.tasks())
                if path.startswith("/api/tasks/") and self.command == "GET":
                    return self.send_json(200, store.task(path.rsplit("/", 1)[-1]))
                if path.startswith("/api/tasks/") and self.command == "PATCH":
                    if path.rsplit("/", 1)[-1] in store.history.tasks():
                        raise BridgeError(409, "historical_task", "历史任务仅供查看")
                    data = self.read_json()
                    if set(data) - {"status", "result"}:
                        raise BridgeError(400, "invalid_request", "无效字段")
                    return self.send_json(200, store.set_task(path.rsplit("/", 1)[-1], data.get("status"), data.get("result")))
                if path.startswith("/api/approvals/") and self.command == "POST":
                    data = self.read_json()
                    if not isinstance(data, dict) or set(data) != {"decision"}:
                        raise BridgeError(400, "invalid_request", "无效字段")
                    return self.send_json(200, engine.resolve_approval(path.rsplit("/", 1)[-1], data["decision"]))
                if path == "/v1/tasks" and self.command == "POST":
                    connection = store.authenticate(self.headers.get("Authorization", ""))
                    data = self.read_json()
                    request_id = self.headers.get("Idempotency-Key") or data.get("request_id")
                    status, result = store.submit(connection, data, request_id)
                    return self.send_json(status, result)
                if path.startswith("/v1/tasks/") and self.command == "GET":
                    connection = store.authenticate(self.headers.get("Authorization", ""))
                    task = store.task(path.rsplit("/", 1)[-1], connection["id"])
                    return self.send_json(200, {"task_id": task["id"], "status": task["status"], "result": task["result"]})
                raise BridgeError(404, "not_found", "页面不存在")
            except BridgeError as exc:
                return self.send_json(exc.status, {"error": {"code": exc.code, "message": str(exc), "retryable": exc.retryable}})
            except Exception:
                LOG.exception("request failed")
                return self.send_json(500, {"error": {"code": "internal_error", "message": "服务发生错误"}})

        do_GET = route
        do_POST = route
        do_PATCH = route
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.getenv("BRIDGE_DB", str(ROOT / "data/bridge.sqlite3")))
    parser.add_argument("--projects", default=os.getenv("BRIDGE_PROJECTS", str(ROOT / "projects.json")))
    parser.add_argument("--port", type=int, default=int(os.getenv("BRIDGE_PORT", "8797")))
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    raise_nofile_limit()
    store = Store(args.db, args.projects)
    engine = Engine(store, LocalCodexTransport(AppServerClient(os.getenv("CODEX_BIN", "codex"))),
                    history_inbox_path=Path(args.db).parent / 'feishu-inbox.sqlite3')
    engine.start()
    base_url = f"http://127.0.0.1:{args.port}"
    feishu_settings_path = Path(os.getenv("FEISHU_CONFIG", str(Path(args.db).parent / "feishu.json")))
    supervisor = FeishuSupervisor(
        feishu_settings_path, Path(args.db).parent / "feishu-inbox.sqlite3", base_url)
    supervisor.start()
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port),
        make_handler(store, engine, base_url,
                     feishu_settings_path=feishu_settings_path,
                     feishu_supervisor=supervisor,
                     feishu_inbox_path=Path(args.db).parent / "feishu-inbox.sqlite3"))
    LOG.info("panel at %s", base_url)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        supervisor.close()
        engine.close()


if __name__ == "__main__":
    main()
