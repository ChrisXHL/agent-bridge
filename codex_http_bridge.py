#!/usr/bin/env python3
"""Authenticated HTTP -> local Codex app-server thread bridge."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import queue
import sqlite3
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

LOG = logging.getLogger("codex_bridge")
MAX_BODY = 16 * 1024
MAX_MESSAGE = 12_000


class BridgeError(Exception):
    def __init__(self, status: int, code: str, message: str, *, retryable: bool = False, extra=None):
        super().__init__(message)
        self.status, self.code, self.retryable, self.extra = status, code, retryable, extra or {}


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class AppServerClient:
    def __init__(self, codex_bin="codex", timeout=25, on_server_request=None):
        from codex_runtime import resolve_codex_binary
        self.codex_bin, self.timeout = resolve_codex_binary(codex_bin), timeout
        self.on_server_request = on_server_request
        self.proc = None
        self.pending = {}
        self.pending_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.start_lock = threading.Lock()
        self.next_id = 0

    def _start(self):
        with self.start_lock:
            if self.proc and self.proc.poll() is None:
                return
            try:
                self.proc = subprocess.Popen(
                    [self.codex_bin, "app-server", "--stdio"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1,
                )
            except OSError as exc:
                raise BridgeError(503, "codex_unavailable", "Could not start Codex app-server", retryable=True) from exc
            threading.Thread(target=self._read, daemon=True).start()
            threading.Thread(target=self._drain_stderr, args=(self.proc,), daemon=True).start()
            self._call("initialize", {"clientInfo": {
                "name": "codex_http_bridge", "title": "Codex HTTP Bridge", "version": "0.1.0"
            }})
            self._write({"method": "initialized", "params": {}})

    def _drain_stderr(self, proc):
        for line in proc.stderr:
            # Codex may include user data in diagnostics; do not log stderr content.
            if line.strip():
                LOG.debug("codex app-server emitted a diagnostic line")

    def _read(self):
        proc = self.proc
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                LOG.warning("invalid JSON from Codex app-server")
                continue
            request_id = msg.get("id")
            if msg.get("method") and request_id is not None:
                if self.on_server_request:
                    try:
                        if self.on_server_request(msg):
                            continue
                    except Exception:
                        LOG.exception("Codex callback handler failed")
                # Unknown callbacks are never approved automatically.
                self._write({"id": request_id, "error": {"code": -32601, "message": "Unsupported callback"}})
            elif request_id is not None:
                with self.pending_lock:
                    target = self.pending.pop(request_id, None)
                if target:
                    target.put(msg)
        with self.pending_lock:
            waiting = list(self.pending.values()) if proc is self.proc else []
            if proc is self.proc:
                self.pending.clear()
        for target in waiting:
            target.put({"error": {"code": -32000, "message": "Codex app-server connection closed"}})

    def _write(self, payload):
        with self.write_lock:
            if not self.proc or self.proc.poll() is not None:
                raise BridgeError(503, "codex_unavailable", "Codex app-server is not running", retryable=True)
            try:
                self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            except OSError as exc:
                raise BridgeError(503, "codex_unavailable", "Codex app-server connection failed", retryable=True) from exc

    def _call(self, method, params):
        response_queue = queue.Queue(maxsize=1)
        with self.pending_lock:
            self.next_id += 1
            request_id = self.next_id
            self.pending[request_id] = response_queue
        try:
            self._write({"id": request_id, "method": method, "params": params})
            try:
                result = response_queue.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise BridgeError(503, "codex_timeout", f"Codex {method} timed out; outcome is uncertain", retryable=False) from exc
            if "error" in result:
                err = result["error"]
                raise RpcError(err.get("code"), err.get("message", "Unknown Codex error"))
            return result["result"]
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

    def request(self, method, params):
        self._start()
        return self._call(method, params)

    def close(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)


class Bridge:
    def __init__(self, projects_file, db_file, token, codex=None, max_active=2, verify_seconds=35):
        if len(token) < 32 or token.startswith("replace-"):
            raise ValueError("BRIDGE_TOKEN must contain at least 32 characters")
        self.token = token
        self.projects = self._load_projects(projects_file)
        self.db_file = str(db_file)
        Path(self.db_file).parent.mkdir(parents=True, exist_ok=True)
        self.codex = codex or AppServerClient()
        self.slots = threading.BoundedSemaphore(max_active)
        self.verify_seconds = verify_seconds
        self._init_db()

    @staticmethod
    def _load_projects(path):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data:
            raise ValueError("projects file must be a nonempty object of project_id -> absolute path")
        projects = {}
        for project_id, raw_path in data.items():
            if not isinstance(project_id, str) or not project_id or not isinstance(raw_path, str):
                raise ValueError("invalid project mapping")
            path = Path(raw_path)
            if not path.is_absolute() or not path.is_dir():
                raise ValueError(f"project path does not exist: {project_id}")
            projects[project_id] = {"configured": path, "resolved": path.resolve(strict=True)}
        return projects

    def _connect(self):
        conn = sqlite3.connect(self.db_file, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS requests (
                idem_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, project_id TEXT NOT NULL,
                status TEXT NOT NULL, thread_id TEXT, session_id TEXT, turn_id TEXT,
                error_code TEXT, updated_at REAL NOT NULL
            )""")

    def _get(self, key):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM requests WHERE idem_key=?", (key,)).fetchone()
            return dict(row) if row else None

    def _set(self, key, status, **fields):
        with self._connect() as conn:
            changes = {"status": status, "updated_at": time.time(), **fields}
            clause = ", ".join(f"{name}=?" for name in changes)
            guard = "" if status == "submitted" else " AND status != 'submitted'"
            conn.execute(f"UPDATE requests SET {clause} WHERE idem_key=?{guard}", (*changes.values(), key))

    def _reserve(self, key, fingerprint, project_id):
        with self._connect() as conn:
            inserted = conn.execute("INSERT OR IGNORE INTO requests VALUES (?, ?, ?, 'reserved', NULL, NULL, NULL, NULL, ?)",
                                    (key, fingerprint, project_id, time.time())).rowcount == 1
        row = self._get(key)
        if row["fingerprint"] != fingerprint:
            raise BridgeError(409, "idempotency_conflict", "Idempotency-Key was used with different content")
        return inserted, row

    def _resolve_project(self, payload):
        project_id = payload.get("project_id")
        project_path = payload.get("project_path")
        if project_id is None and project_path is None:
            raise BridgeError(400, "project_required", "Provide project_id or project_path")
        if project_id is not None and (not isinstance(project_id, str) or project_id not in self.projects):
            raise BridgeError(404, "project_not_allowed", "Project is not in the allowlist")
        if project_path is not None and (not isinstance(project_path, str) or not Path(project_path).is_absolute()):
            raise BridgeError(400, "invalid_project_path", "project_path must be an absolute path")
        if project_id is None:
            matches = [key for key, value in self.projects.items()
                       if str(value["configured"]) == project_path]
            if len(matches) != 1:
                raise BridgeError(404, "project_not_allowed", "Project path is not in the allowlist")
            project_id = matches[0]
        selected = self.projects[project_id]
        if project_path is not None and project_path != str(selected["configured"]):
            raise BridgeError(400, "project_mismatch", "project_id and project_path do not match")
        try:
            if selected["configured"].resolve(strict=True) != selected["resolved"] or not selected["resolved"].is_dir():
                raise ValueError("changed path")
        except (OSError, ValueError) as exc:
            raise BridgeError(409, "project_path_changed", "Configured project path changed or disappeared") from exc
        return project_id, str(selected["resolved"])

    @staticmethod
    def _message_seen(thread, message):
        for turn in thread.get("turns", []):
            for item in turn.get("items", []):
                if item.get("type") == "userMessage":
                    if any(part.get("type") == "text" and part.get("text") == message
                           for part in item.get("content", [])):
                        return True
        return False

    def _verify(self, thread_id, message):
        deadline = time.monotonic() + self.verify_seconds
        while True:
            try:
                result = self.codex.request("thread/read", {"threadId": thread_id, "includeTurns": True})
                if self._message_seen(result.get("thread", {}), message):
                    return True
            except (RpcError, BridgeError) as exc:
                LOG.warning("readback failed for thread=%s: %s", thread_id, type(exc).__name__)
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.5, deadline - time.monotonic()))

    def create(self, payload, key):
        if not isinstance(payload, dict):
            raise BridgeError(400, "invalid_json", "Body must be a JSON object")
        if set(payload) - {"project_id", "project_path", "message"}:
            raise BridgeError(400, "unknown_field", "Request has unknown fields")
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE:
            raise BridgeError(400, "invalid_message", f"message must contain 1-{MAX_MESSAGE} characters")
        if not isinstance(key, str) or not (8 <= len(key) <= 128) or not all(c.isalnum() or c in "-_.:" for c in key):
            raise BridgeError(400, "invalid_idempotency_key", "Idempotency-Key must be 8-128 safe characters")
        project_id, cwd = self._resolve_project(payload)
        fingerprint = hashlib.sha256(json.dumps([project_id, cwd, message], ensure_ascii=False).encode()).hexdigest()
        existing = self._get(key)
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise BridgeError(409, "idempotency_conflict", "Idempotency-Key was used with different content")
            if existing["status"] == "submitted":
                return 200, self._result(existing, duplicate=True)
            if existing["thread_id"] and self._verify(existing["thread_id"], message):
                self._set(key, "submitted", error_code=None)
                return 200, self._result(self._get(key), duplicate=True)
            raise BridgeError(409, "delivery_uncertain", "Request is in progress or its outcome is uncertain; no new thread was created",
                              extra={"conversation_id": existing["thread_id"], "status": existing["status"]})
        if not self.slots.acquire(blocking=False):
            raise BridgeError(429, "busy", "Too many active requests", retryable=True)
        owner = False
        try:
            inserted, row = self._reserve(key, fingerprint, project_id)
            if not inserted:
                raise BridgeError(409, "delivery_uncertain", "Request was already reserved")
            owner = True
            self._set(key, "thread_start_attempted")
            try:
                created = self.codex.request("thread/start", {
                    "cwd": cwd, "approvalPolicy": "never", "sandbox": "read-only",
                    "ephemeral": False, "serviceName": "codex_http_bridge",
                })
            except RpcError as exc:
                self._set(key, "failed", error_code="codex_rejected_thread")
                raise BridgeError(502, "codex_rejected_thread", f"Codex rejected thread creation: {exc}") from exc
            thread = created.get("thread", {})
            thread_id = thread.get("id")
            if not thread_id:
                raise BridgeError(502, "invalid_codex_response", "Codex did not return a thread id")
            session_id = thread.get("sessionId") or thread_id
            self._set(key, "thread_created", thread_id=thread_id, session_id=session_id)
            self._set(key, "turn_start_attempted")
            try:
                started = self.codex.request("turn/start", {"threadId": thread_id,
                    "input": [{"type": "text", "text": message}]})
            except RpcError as exc:
                self._set(key, "uncertain", error_code="codex_rejected_turn")
                raise BridgeError(502, "codex_rejected_turn", f"Codex rejected first message: {exc}",
                                  extra={"conversation_id": thread_id}) from exc
            turn_id = started.get("turn", {}).get("id")
            self._set(key, "verifying", turn_id=turn_id)
            if not self._verify(thread_id, message):
                self._set(key, "uncertain", error_code="readback_timeout")
                raise BridgeError(503, "readback_timeout", "Codex accepted the turn, but the first message was not verified",
                                  extra={"conversation_id": thread_id})
            self._set(key, "submitted", error_code=None)
            row = self._get(key)
            LOG.info("submitted project_id=%s thread_id=%s turn_id=%s", project_id, thread_id, turn_id)
            return 201, self._result(row)
        except BridgeError as exc:
            row = self._get(key)
            if owner and row and row["status"] not in ("submitted", "failed", "uncertain"):
                self._set(key, "uncertain", error_code=exc.code)
            LOG.warning("request failed key_hash=%s code=%s thread_id=%s", hashlib.sha256(key.encode()).hexdigest()[:12],
                        exc.code, row["thread_id"] if row else None)
            raise
        finally:
            self.slots.release()

    @staticmethod
    def _result(row, duplicate=False):
        return {"conversation_id": row["thread_id"], "session_id": row["session_id"],
                "turn_id": row["turn_id"], "project_id": row["project_id"],
                "status": row["status"], "duplicate": duplicate}


def make_handler(bridge):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CodexHTTPBridge/0.1"

        def log_message(self, format, *args):
            LOG.info("http_status=%s", args[1] if len(args) > 1 else "unknown")

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def _json(self, status, data):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _auth(self):
            supplied = self.headers.get("Authorization", "")
            expected = "Bearer " + bridge.token
            if not hmac.compare_digest(supplied, expected):
                raise BridgeError(401, "unauthorized", "Invalid bearer token")

        def _handle(self):
            request_id = str(uuid.uuid4())
            try:
                self._auth()
                path = urlsplit(self.path).path
                if self.command == "GET" and path == "/healthz":
                    return self._json(200, {"status": "ok"})
                if self.command == "GET" and path.startswith("/v1/requests/"):
                    key = unquote(path[len("/v1/requests/"):])
                    row = bridge._get(key)
                    if not row:
                        raise BridgeError(404, "request_not_found", "Unknown Idempotency-Key")
                    return self._json(200, bridge._result(row))
                if self.command != "POST" or path != "/v1/conversations":
                    raise BridgeError(404, "not_found", "Endpoint not found")
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise BridgeError(415, "unsupported_media_type", "Content-Type must be application/json")
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    raise BridgeError(411, "length_required", "Content-Length is required")
                if length < 1 or length > MAX_BODY:
                    raise BridgeError(413, "body_too_large", "Request body exceeds limit")
                raw = self.rfile.read(length)
                try:
                    payload = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise BridgeError(400, "invalid_json", "Body is not valid JSON")
                status, result = bridge.create(payload, self.headers.get("Idempotency-Key"))
                return self._json(status, result)
            except BridgeError as exc:
                return self._json(exc.status, {"error": {"code": exc.code, "message": str(exc),
                    "retryable": exc.retryable, **exc.extra}, "request_id": request_id})
            except Exception:
                LOG.exception("unexpected error request_id=%s", request_id)
                return self._json(500, {"error": {"code": "internal_error", "message": "Internal error",
                    "retryable": False}, "request_id": request_id})

        def do_POST(self):
            self._handle()

        def do_GET(self):
            self._handle()

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projects", default=os.getenv("BRIDGE_PROJECTS", "projects.json"))
    parser.add_argument("--db", default=os.getenv("BRIDGE_DB", "bridge.sqlite3"))
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    token = os.getenv("BRIDGE_TOKEN", "")
    bridge = Bridge(args.projects, args.db, token,
                    codex=AppServerClient(os.getenv("CODEX_BIN", "codex")))
    host = os.getenv("BRIDGE_HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError("Bridge must bind to loopback; expose it through an authenticated TLS reverse proxy")
    port = int(os.getenv("BRIDGE_PORT", "8787"))
    server = ThreadingHTTPServer((host, port), make_handler(bridge))
    LOG.info("listening host=%s port=%s projects=%s", host, port, len(bridge.projects))
    try:
        server.serve_forever()
    finally:
        server.server_close()
        bridge.codex.close()


if __name__ == "__main__":
    main()
