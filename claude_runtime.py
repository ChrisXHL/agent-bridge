"""Claude Code execution backend (BRIDGE_BACKEND=claude).

Implements the transport surface the Engine uses (start_thread, send, read,
resume_thread, release_idle, close) on top of `claude -p`. A Bridge thread maps
to one Claude Code session; every send is one turn executed by a child
process. Turns are persisted so readback survives service restarts.
"""
import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

LOG = logging.getLogger("codex_bridge.claude")
# Turns run unattended from Feishu messages: tools outside the allowlist are
# refused (dontAsk). File edits are confined to BRIDGE_WORKSPACE_ROOT.
DEFAULT_PERMISSION_MODE = "dontAsk"
OPEN_TOOLS = ["Bash", "Read", "Glob", "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite", "Skill"]


def workspace_root():
    value = os.getenv("BRIDGE_WORKSPACE_ROOT")
    return Path(value).expanduser().resolve() if value else None


def permission_settings(mcp_servers=(), cwd=None):
    """mcp_servers are rule prefixes like "mcp__tavily"; "mcp__*" is not a valid rule.
    Edits are allowed in the workspace root and in the folder the task runs in
    (a folder the user explicitly picked for this assistant or group)."""
    root = workspace_root()
    if not root:
        scoped = ["Edit", "Write", "NotebookEdit"]
    else:
        folders = [root]
        if cwd:
            here = Path(cwd).resolve()
            if here != root and root not in here.parents:
                folders.append(here)
        scoped = [f"{tool}(/{folder}/**)" for folder in folders for tool in ("Edit", "Write", "NotebookEdit")]
    return json.dumps({"permissions": {"allow": OPEN_TOOLS + scoped + sorted(mcp_servers)}}, ensure_ascii=False)


def mcp_servers_from_init(event):
    return {t.rsplit("__", 1)[0] for t in event.get("tools") or [] if t.startswith("mcp__") and t.count("__") >= 2}


MCP_REFRESH_SECONDS = 600
TURN_TIMEOUT = int(os.getenv("CLAUDE_TURN_TIMEOUT", str(4 * 3600)))
# Host-session variables must not leak into service-launched turns.
_STRIP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
              "CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")


def backend():
    return os.getenv("BRIDGE_BACKEND", "codex").strip().lower()


def resolve_claude_binary(explicit=None):
    explicit = explicit or os.getenv("CLAUDE_BIN")
    if explicit:
        return explicit
    for candidate in (Path.home() / ".local/bin/claude", Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("claude") or "claude"


def child_env():
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.setdefault("HOME", str(Path.home()))
    return env


def run_structured(prompt, schema, directory, log_path, timeout=240):
    """Tool-less structured JSON generation; replacement for `codex exec --output-schema`."""
    cmd = [resolve_claude_binary(), "-p", "--output-format", "json", "--tools", "",
           "--no-session-persistence", "--json-schema", json.dumps(schema, ensure_ascii=False)]
    model = os.getenv("CLAUDE_MODEL")
    if model:
        cmd += ["--model", model]
    with Path(log_path).open("ab") as log:
        p = subprocess.run(cmd, input=prompt.encode(), stdout=subprocess.PIPE, stderr=log,
                           cwd=str(directory), env=child_env(), timeout=timeout)
        log.write(p.stdout[-4000:])
    if p.returncode:
        raise RuntimeError("claude_exec_failed")
    value = json.loads(p.stdout)
    if value.get("is_error") or not isinstance(value.get("structured_output"), dict):
        raise RuntimeError("claude_exec_failed")
    return value["structured_output"]


class ClaudeTransport:
    def __init__(self, state_dir, claude_bin=None, permission_mode=None, model=None):
        self.claude_bin = resolve_claude_binary(claude_bin)
        self.permission_mode = permission_mode or os.getenv("CLAUDE_PERMISSION_MODE", DEFAULT_PERMISSION_MODE)
        self.model = model or os.getenv("CLAUDE_MODEL")
        self.path = str(Path(state_dir) / "claude-threads.sqlite3")
        self.lock = threading.Lock()
        self.procs = {}
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, cwd TEXT NOT NULL,
              session_started INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, seq INTEGER NOT NULL,
              status TEXT NOT NULL, user_text TEXT NOT NULL, agent_text TEXT, error TEXT,
              started_at REAL NOT NULL, finished_at REAL);
            """)
            # A turn cannot outlive the service that supervised it.
            db.execute("UPDATE turns SET status='failed',error='Bridge 重启，Claude Code 执行被中断',finished_at=? "
                       "WHERE status='inProgress'", (time.time(),))
        self.mcp_path = Path(state_dir) / "claude-mcp-servers.json"
        try:
            self.mcp_servers = set(json.loads(self.mcp_path.read_text()))
        except (OSError, ValueError):
            self.mcp_servers = set()
        self.mcp_checked_at = 0.0
        self.mcp_lock = threading.Lock()
        threading.Thread(target=self.refresh_mcp, daemon=True).start()

    def _remember_mcp(self, servers):
        with self.mcp_lock:
            if servers and servers != self.mcp_servers:
                self.mcp_servers = set(servers)
                self.mcp_path.write_text(json.dumps(sorted(servers)))
                LOG.info("claude MCP servers allowed: %s", sorted(servers))
            self.mcp_checked_at = time.time()

    def refresh_mcp(self, cwd=None):
        """Read the init event of a throwaway run to learn installed MCP servers; stops before any model call."""
        cwd = cwd or str(workspace_root() or Path.home())
        cmd = [self.claude_bin, "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence"]
        proc = None
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, env=child_env(), start_new_session=True)
            proc.stdin.write(b"."); proc.stdin.close()
            timer = threading.Timer(90, self._kill, (proc,)); timer.start()
            try:
                for line in proc.stdout:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("type") == "system" and event.get("subtype") == "init":
                        self._remember_mcp(mcp_servers_from_init(event))
                        break
            finally:
                timer.cancel()
        except Exception:
            LOG.warning("claude MCP discovery failed", exc_info=True)
        finally:
            if proc:
                self._kill(proc)

    def _mcp_for_turn(self, cwd):
        if not self.mcp_servers:
            self.refresh_mcp(cwd)
        elif time.time() - self.mcp_checked_at > MCP_REFRESH_SECONDS:
            self.mcp_checked_at = time.time()
            threading.Thread(target=self.refresh_mcp, args=(cwd,), daemon=True).start()
        return set(self.mcp_servers)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return _Closing(db)

    def start_thread(self, cwd):
        thread_id = str(uuid.uuid4())
        with self._db() as db:
            db.execute("INSERT INTO threads VALUES (?,?,0,?)", (thread_id, cwd, time.time()))
        return thread_id

    def send(self, thread_id, message):
        with self._db() as db:
            thread = db.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
            if not thread:
                raise RuntimeError("Claude thread not found")
            if db.execute("SELECT 1 FROM turns WHERE thread_id=? AND status='inProgress'", (thread_id,)).fetchone():
                raise RuntimeError("Claude thread already has an active turn")
            seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM turns WHERE thread_id=?", (thread_id,)).fetchone()[0]
            turn_id = str(uuid.uuid4())
            db.execute("INSERT INTO turns (id,thread_id,seq,status,user_text,started_at) VALUES (?,?,?,?,?,?)",
                       (turn_id, thread_id, seq, "inProgress", message, time.time()))
        worker = threading.Thread(target=self._run, args=(dict(thread), turn_id, message), daemon=True)
        worker.start()
        return turn_id

    def _run(self, thread, turn_id, message):
        cmd = [self.claude_bin, "-p", "--output-format", "stream-json", "--verbose",
               "--permission-mode", self.permission_mode,
               "--settings", permission_settings(self._mcp_for_turn(thread["cwd"]), thread["cwd"])]
        # Read access to Bridge attachments/state and the packaged CLI/docs.
        for extra in (Path(self.path).parent, Path(__file__).resolve().parent):
            cmd += ["--add-dir", str(extra)]
        cmd += ["--resume", thread["id"]] if thread["session_started"] else ["--session-id", thread["id"]]
        if self.model:
            cmd += ["--model", self.model]
        status, text, error, last_text = "failed", None, None, None
        try:
            with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as stderr:
                stdin.write(message.encode()); stdin.seek(0)
                proc = subprocess.Popen(cmd, cwd=thread["cwd"], stdin=stdin, stdout=subprocess.PIPE, stderr=stderr,
                                        env=child_env(), start_new_session=True)
                with self.lock:
                    self.procs[turn_id] = proc
                timer = threading.Timer(TURN_TIMEOUT, self._kill, (proc,))
                timer.start()
                result = None
                try:
                    for line in proc.stdout:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        kind = event.get("type")
                        if kind == "system" and event.get("subtype") == "init":
                            self._mark_started(thread["id"])
                            found = mcp_servers_from_init(event)
                            if found - self.mcp_servers:
                                self._remember_mcp(found | self.mcp_servers)
                        elif kind == "assistant":
                            parts = (event.get("message") or {}).get("content") or []
                            chunk = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
                            if chunk.strip():
                                last_text = chunk
                        elif kind == "result":
                            result = event
                    code = proc.wait()
                finally:
                    timer.cancel()
                if result and not result.get("is_error") and result.get("subtype") == "success":
                    status, text = "completed", result.get("result") or last_text
                elif result:
                    error = result.get("result") or result.get("subtype") or "Claude Code 执行失败"
                elif code in (-signal.SIGTERM, -signal.SIGKILL):
                    status, error = "interrupted", "Claude Code 执行被终止"
                else:
                    stderr.seek(0)
                    tail = stderr.read()[-800:].decode(errors="replace").strip()
                    error = f"Claude Code 退出码 {code}" + (f"：{tail}" if tail else "")
        except Exception as exc:
            LOG.exception("claude turn crashed turn=%s", turn_id)
            error = f"无法启动 Claude Code：{type(exc).__name__}"
        finally:
            with self.lock:
                self.procs.pop(turn_id, None)
        with self._db() as db:
            db.execute("UPDATE turns SET status=?,agent_text=?,error=?,finished_at=? WHERE id=?",
                       (status, text, error, time.time(), turn_id))
        LOG.info("claude turn finished thread=%s turn=%s status=%s", thread["id"], turn_id, status)

    def _mark_started(self, thread_id):
        with self._db() as db:
            db.execute("UPDATE threads SET session_started=1 WHERE id=?", (thread_id,))

    @staticmethod
    def _kill(proc):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass

    def read(self, thread_id):
        with self._db() as db:
            if not db.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone():
                raise RuntimeError("Claude thread not found")
            rows = db.execute("SELECT * FROM turns WHERE thread_id=? ORDER BY seq", (thread_id,)).fetchall()
        turns = []
        for row in rows:
            items = [{"type": "userMessage", "content": [{"type": "text", "text": row["user_text"]}]}]
            if row["agent_text"]:
                items.append({"type": "agentMessage", "text": row["agent_text"]})
            turn = {"id": row["id"], "status": row["status"], "items": items}
            if row["error"]:
                turn["error"] = row["error"]
            turns.append(turn)
        active = bool(turns) and turns[-1]["status"] == "inProgress"
        return {"id": thread_id, "status": {"type": "active" if active else "idle"}, "turns": turns}

    def resume_thread(self, thread_id):
        self.read(thread_id)

    def release_idle(self):
        pass

    def close(self):
        with self.lock:
            procs = list(self.procs.values())
        for proc in procs:
            self._kill(proc)


class _Closing:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *exc):
        self.db.close()
