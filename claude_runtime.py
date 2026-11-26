"""Claude Code execution backend for robots explicitly assigned to Claude.

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


class ClaudeIdentityRequired(RuntimeError):
    """The account is logged in, but Anthropic requires an interactive identity check."""
# Turns run unattended from Feishu messages: tools outside the allowlist are
# refused (dontAsk). File edits are confined to BRIDGE_WORKSPACE_ROOT.
DEFAULT_PERMISSION_MODE = "dontAsk"
OPEN_TOOLS = ["Bash", "Read", "Glob", "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite", "Skill"]


def backend():
    """Local default execution backend (BRIDGE_BACKEND=claude|codex)."""
    return os.getenv("BRIDGE_BACKEND", "codex").strip().lower()


def workspace_root_file():
    config = os.getenv("FEISHU_CONFIG")
    return Path(config).parent / "workspace-root.txt" if config else None


def workspace_root():
    """The state-dir setting wins over the launch environment, so the root can move without reinstalling."""
    setting = workspace_root_file()
    try:
        value = setting.read_text(encoding="utf-8").strip() if setting and setting.exists() else ""
    except OSError:
        value = ""
    value = value or os.getenv("BRIDGE_WORKSPACE_ROOT")
    return Path(value).expanduser().absolute() if value else None


DEFAULT_WORKSPACE_NAME = "Agent Bridge 助手工作区"


def assistant_workspace_root(home=None):
    """Where newly created assistants get their own folder: the configured
    workspace root, else 「Agent Bridge 助手工作区」 on the Desktop. Created on demand."""
    root = workspace_root() or Path(home or Path.home()) / "Desktop" / DEFAULT_WORKSPACE_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def assistant_folder_name(name):
    import re
    return re.sub(r'[/\\:\x00-\x1f]', '-', str(name or '')).strip('. ') or '飞书助手'


def new_assistant_folder(name, taken=(), home=None):
    """A fresh folder named after the assistant inside the workspace root.
    Never reuses a folder another project owns or one that already has files."""
    root, base = assistant_workspace_root(home), assistant_folder_name(name)
    taken = {str(Path(p)) for p in taken}
    for index in range(1, 1000):
        folder = root / (base if index == 1 else "%s %d" % (base, index))
        if str(folder) in taken or (folder.exists() and (not folder.is_dir() or any(folder.iterdir()))):
            continue
        folder.mkdir(parents=True, exist_ok=True)
        return folder
    raise RuntimeError("no free assistant folder")


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
# Appended to every turn's system prompt, so it holds for any bot, frozen group
# topics and resumed sessions alike, independent of each saved prompt.
REPLY_FORMAT = (
    "你的最终回答会原样作为飞书聊天消息发出，飞书聊天不渲染 markdown。"
    "聊天回复只用纯文本：不用 #、**、`、表格、代码块和 - 列表；分点用“1. 2. 3.”或换行。"
    "先判断输出形式：简短结论、确认或进度直接在聊天里说（一般不超过 15 行）；"
    "内容含表格、多级结构、超过约 500 字，或需要保存、评审、后续修改时，"
    "写成飞书文档（文档里可以正常使用 markdown），聊天里只发 3 到 5 行结论和文档链接。"
)
TURN_TIMEOUT = int(os.getenv("CLAUDE_TURN_TIMEOUT", str(4 * 3600)))
# Host-session variables must not leak into service-launched turns.
_STRIP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
              "CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")


def resolve_claude_binary(explicit=None):
    explicit = explicit or os.getenv("CLAUDE_BIN")
    if explicit:
        return explicit
    for candidate in (Path.home() / ".local/bin/claude", Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("claude") or "claude"


def base_env():
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.setdefault("HOME", str(Path.home()))
    return env


def child_env():
    # Local patch: browser login by default; the panel's backup token takes over
    # when that login is missing, expired, or just failed authentication.
    env = base_env()
    try:
        import claude_auth
        token = claude_auth.fallback_token()
    except Exception:
        LOG.exception("claude fallback token check failed")
        token = None
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


def run_structured(prompt, schema, directory, log_path, timeout=240):
    """Tool-less structured JSON generation; replacement for `codex exec --output-schema`."""
    try:
        return _run_structured(prompt, schema, directory, log_path, timeout)
    except ClaudeAuthFailed:
        import claude_auth
        if not claude_auth._read_token():
            raise RuntimeError("claude_exec_failed")
        claude_auth.mark_login_failed()
        LOG.warning("claude login failed; retrying structured run with backup token")
        try:
            return _run_structured(prompt, schema, directory, log_path, timeout)
        except ClaudeAuthFailed as exc:
            raise RuntimeError("claude_exec_failed") from exc


class ClaudeAuthFailed(RuntimeError):
    pass


def _run_structured(prompt, schema, directory, log_path, timeout):
    cmd = [resolve_claude_binary(), "-p", "--output-format", "json", "--tools", "",
           "--no-session-persistence", "--json-schema", json.dumps(schema, ensure_ascii=False)]
    model = os.getenv("CLAUDE_MODEL")
    if model:
        cmd += ["--model", model]
    with Path(log_path).open("ab") as log:
        p = subprocess.run(cmd, input=prompt.encode(), stdout=subprocess.PIPE, stderr=log,
                           cwd=str(directory), env=child_env(), timeout=timeout)
        log.write(p.stdout[-4000:])
    try:
        value = json.loads(p.stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("claude_exec_failed") from exc
    if (value.get("is_error") and value.get("api_error_status") == 400
            and "Identity verification is required to continue" in str(value.get("result", ""))):
        raise ClaudeIdentityRequired("claude_identity_required")
    if value.get("is_error"):
        import claude_auth
        if claude_auth.is_auth_error(str(value.get("result", ""))):
            raise ClaudeAuthFailed("claude_auth_failed")
    if p.returncode or value.get("is_error") or not isinstance(value.get("structured_output"), dict):
        raise RuntimeError("claude_exec_failed")
    return value["structured_output"]


MAX_RESUMES = int(os.getenv("CLAUDE_MAX_RESUMES", "3"))
CONTINUE_NOTE = ("（系统说明：上一轮执行因 Bridge 重启或进程意外退出而中断。请先检查已经完成的部分，"
                 "从中断处继续把原任务做完；不要重复已经完成的对外操作，例如重复发消息、重复创建文档。"
                 "最终回答仍按原任务要求给出完整结果。）")


def _alive(pid, marker):
    """True if pid is still a Claude process for this session (guards against pid reuse)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "stat=,command="], capture_output=True, text=True,
                             timeout=5).stdout.strip()
    except Exception:
        return True
    return bool(out) and not out.startswith("Z") and marker in out


def _killpg(pid):
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        pass


class ClaudeTransport:
    def __init__(self, state_dir, claude_bin=None, permission_mode=None, model=None):
        self.claude_bin = resolve_claude_binary(claude_bin)
        self.permission_mode = permission_mode or os.getenv("CLAUDE_PERMISSION_MODE", DEFAULT_PERMISSION_MODE)
        self.model = model or os.getenv("CLAUDE_MODEL")
        self.path = str(Path(state_dir) / "claude-threads.sqlite3")
        self.lock = threading.Lock()
        self.procs = {}
        self.interrupting = set()
        self.auth_retried = set()
        self.closed = False
        import claude_auth
        claude_auth.configure(state_dir)
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, cwd TEXT NOT NULL,
              session_started INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS turns (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, seq INTEGER NOT NULL,
              status TEXT NOT NULL, user_text TEXT NOT NULL, agent_text TEXT, error TEXT,
              started_at REAL NOT NULL, finished_at REAL);
            """)
            columns = {row[1] for row in db.execute('PRAGMA table_info(turns)')}
            for name, kind in (('model', 'TEXT'), ('pid', 'INTEGER'), ('out_path', 'TEXT'),
                               ('resumes', 'INTEGER NOT NULL DEFAULT 0')):
                if name not in columns:
                    db.execute(f'ALTER TABLE turns ADD COLUMN {name} {kind}')
        # Local patch: turns run detached and write to files, so they outlive a
        # Bridge restart. On startup, reattach to live turns and continue lost ones.
        self.turn_dir = Path(state_dir) / "claude-turns"
        self.turn_dir.mkdir(mode=0o700, exist_ok=True)
        self.mcp_path = Path(state_dir) / "claude-mcp-servers.json"
        try:
            self.mcp_servers = set(json.loads(self.mcp_path.read_text()))
        except (OSError, ValueError):
            self.mcp_servers = set()
        self.mcp_checked_at = 0.0
        self.mcp_lock = threading.Lock()
        threading.Thread(target=self.refresh_mcp, daemon=True).start()
        self.recover()

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

    def _command(self, thread):
        cmd = [self.claude_bin, "-p", "--output-format", "stream-json", "--verbose",
               "--permission-mode", self.permission_mode,
               "--settings", permission_settings(self._mcp_for_turn(thread["cwd"]), thread["cwd"]),
               "--append-system-prompt", REPLY_FORMAT]
        # Read access to Bridge attachments/state and the packaged CLI/docs.
        for extra in (Path(self.path).parent, Path(__file__).resolve().parent):
            cmd += ["--add-dir", str(extra)]
        cmd += ["--resume", thread["id"]] if thread["session_started"] else ["--session-id", thread["id"]]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def _run(self, thread, turn_id, message):
        """Launch one detached attempt of a turn, then follow its output file."""
        try:
            out_path = self.turn_dir / f"{turn_id}.{int(time.time() * 1000)}.jsonl"
            with tempfile.TemporaryFile() as stdin, open(out_path, "ab") as out, \
                    open(out_path.with_suffix(".err"), "ab") as err:
                stdin.write(message.encode()); stdin.seek(0)
                proc = subprocess.Popen(self._command(thread), cwd=thread["cwd"], stdin=stdin, stdout=out,
                                        stderr=err, env=child_env(), start_new_session=True)
            with self._db() as db:
                db.execute("UPDATE turns SET pid=?,out_path=? WHERE id=?", (proc.pid, str(out_path), turn_id))
        except Exception as exc:
            LOG.exception("claude turn could not start turn=%s", turn_id)
            self._finish(turn_id, "failed", None, f"无法启动 Claude Code：{type(exc).__name__}")
            return
        with self.lock:
            self.procs[turn_id] = proc
        self._watch(thread, turn_id, str(out_path), proc.pid, proc)

    def _watch(self, thread, turn_id, out_path, pid, proc=None):
        """Follow a turn's output file until its process ends; works for reattached turns too."""
        with self._db() as db:
            started = db.execute("SELECT started_at FROM turns WHERE id=?", (turn_id,)).fetchone()[0]
        result, last_text, buffer, offset, code = None, None, b"", 0, None
        try:
            while True:
                alive = proc.poll() is None if proc else _alive(pid, thread["id"])
                with open(out_path, "rb") as f:
                    f.seek(offset); chunk = f.read(); offset += len(chunk)
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    kind = event.get("type")
                    if kind == 'assistant' or (kind == 'system' and event.get('subtype') == 'init'):
                        observed = (event.get('message') or {}).get('model') if kind == 'assistant' else event.get('model')
                        if observed:
                            with self._db() as db:
                                db.execute('UPDATE turns SET model=? WHERE id=?', (observed, turn_id))
                    if kind == "system" and event.get("subtype") == "init":
                        self._mark_started(thread["id"]); thread = {**thread, "session_started": 1}
                        found = mcp_servers_from_init(event)
                        if found - self.mcp_servers:
                            self._remember_mcp(found | self.mcp_servers)
                    elif kind == "assistant":
                        parts = (event.get("message") or {}).get("content") or []
                        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
                        if text.strip():
                            last_text = text
                    elif kind == "result":
                        result = event
                if self.closed:
                    return
                if not alive:
                    code = proc.wait() if proc else None
                    break
                if time.time() - started > TURN_TIMEOUT:
                    _killpg(pid)
                time.sleep(0.5)
        except Exception:
            LOG.exception("claude turn watcher crashed turn=%s", turn_id)
        finally:
            with self.lock:
                self.procs.pop(turn_id, None)
        with self._db() as db:
            row = db.execute("SELECT status,user_text,resumes FROM turns WHERE id=?", (turn_id,)).fetchone()
        if not row or row["status"] != "inProgress":
            return
        if result and not result.get("is_error") and result.get("subtype") == "success":
            self._finish(turn_id, "completed", result.get("result") or last_text, None)
        elif result and self._retry_with_token(thread, turn_id, row, result, last_text):
            return
        elif result:
            self._finish(turn_id, "failed", None, result.get("result") or result.get("subtype") or "Claude Code 执行失败")
        elif turn_id in self.interrupting:
            self._finish(turn_id, "interrupted", None, "Claude Code 执行被终止")
        elif time.time() - started > TURN_TIMEOUT:
            self._finish(turn_id, "failed", None, "Claude Code 执行超时")
        elif self._retry_with_token(thread, turn_id, row, {"result": self._err_tail(out_path)}, last_text):
            return
        elif row["resumes"] < MAX_RESUMES:
            # The process vanished without a result (crash, reboot, killed by the
            # system): continue the same conversation instead of failing the task.
            self._continue(thread, turn_id, row)
        else:
            err = ""
            try:
                err = Path(out_path).with_suffix(".err").read_bytes()[-800:].decode(errors="replace").strip()
            except OSError:
                pass
            self._finish(turn_id, "failed", None, "Claude Code 多次中断" + (f"：{err}" if err else ""))

    @staticmethod
    def _err_tail(out_path):
        try:
            return Path(out_path).with_suffix(".err").read_bytes()[-2000:].decode(errors="replace")
        except OSError:
            return ""

    def _retry_with_token(self, thread, turn_id, row, result, last_text):
        """Login failed authentication: rerun this turn once on the backup token."""
        import claude_auth
        if turn_id in self.auth_retried or not claude_auth.is_auth_error(str(result.get("result") or "")):
            return False
        if not claude_auth._read_token():
            return False
        self.auth_retried.add(turn_id)
        claude_auth.mark_login_failed()
        with self._db() as db:
            started = db.execute("SELECT session_started FROM threads WHERE id=?", (thread["id"],)).fetchone()[0]
        thread = {**thread, "session_started": started}
        # Nothing ran yet in the usual case; only if output was produced continue instead of resending.
        message = CONTINUE_NOTE if last_text else row["user_text"]
        LOG.warning("claude login failed; retrying turn with backup token turn=%s", turn_id)
        self._run(thread, turn_id, message)
        return True

    def _continue(self, thread, turn_id, row):
        with self._db() as db:
            db.execute("UPDATE turns SET resumes=resumes+1 WHERE id=?", (turn_id,))
            started = db.execute("SELECT session_started FROM threads WHERE id=?", (thread["id"],)).fetchone()[0]
        thread = {**thread, "session_started": started}
        message = CONTINUE_NOTE if started else row["user_text"]
        LOG.info("claude turn continuing after interruption thread=%s turn=%s", thread["id"], turn_id)
        self._run(thread, turn_id, message)

    def _finish(self, turn_id, status, text, error):
        with self._db() as db:
            db.execute("UPDATE turns SET status=?,agent_text=?,error=?,finished_at=? WHERE id=? AND status='inProgress'",
                       (status, text, error, time.time(), turn_id))
        self.interrupting.discard(turn_id)
        LOG.info("claude turn finished turn=%s status=%s", turn_id, status)

    def recover(self):
        """Reattach to turns still running from a previous Bridge process; continue lost ones."""
        with self._db() as db:
            rows = db.execute("""SELECT t.id,t.pid,t.out_path,th.id AS thread_id,th.cwd,th.session_started
                FROM turns t JOIN threads th ON th.id=t.thread_id WHERE t.status='inProgress'""").fetchall()
        for row in rows:
            thread = {"id": row["thread_id"], "cwd": row["cwd"], "session_started": row["session_started"]}
            if row["pid"] and row["out_path"] and _alive(row["pid"], row["thread_id"]):
                LOG.info("claude turn reattached turn=%s pid=%s", row["id"], row["pid"])
                target, args = self._watch, (thread, row["id"], row["out_path"], row["pid"])
            elif row["out_path"] and Path(row["out_path"]).exists():
                # Finished (or died) while Bridge was down: read what it wrote.
                target, args = self._watch, (thread, row["id"], row["out_path"], row["pid"] or 0)
            else:
                with self._db() as db:
                    full = db.execute("SELECT * FROM turns WHERE id=?", (row["id"],)).fetchone()
                target, args = self._continue, (thread, row["id"], full)
            threading.Thread(target=target, args=args, daemon=True).start()

    def _mark_started(self, thread_id):
        with self._db() as db:
            db.execute("UPDATE threads SET session_started=1 WHERE id=?", (thread_id,))

    @staticmethod
    def _kill(proc):
        _killpg(proc.pid)

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

    def interrupt(self, thread_id):
        """Stop the thread's running turn; _run then records it as interrupted."""
        with self._db() as db:
            row = db.execute("SELECT id FROM turns WHERE thread_id=? AND status='inProgress'", (thread_id,)).fetchone()
        if not row:
            return False
        with self.lock:
            proc = self.procs.get(row["id"])
        with self._db() as db:
            pid = db.execute("SELECT pid FROM turns WHERE id=?", (row["id"],)).fetchone()[0]
        if proc or (pid and _alive(pid, thread_id)):
            self.interrupting.add(row["id"])
            _killpg(proc.pid if proc else pid)
            return True
        # No live process (e.g. lost across a restart): close the record directly.
        with self._db() as db:
            db.execute("UPDATE turns SET status='interrupted',error='已手动取消',finished_at=? WHERE id=?",
                       (time.time(), row["id"]))
        return True

    def resume_thread(self, thread_id):
        self.read(thread_id)

    def release_idle(self):
        pass

    def close(self):
        # Running turns are detached on purpose: the next Bridge process reattaches.
        self.closed = True


class _Closing:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *exc):
        self.db.close()
