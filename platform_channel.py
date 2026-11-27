"""Generic chat-platform channel for Bridge.

A platform (Telegram, Slack, DingTalk, WeCom, ...) is one file in ``platforms/``
that subclasses :class:`PlatformChannel` and implements only the
platform-specific parts: how an account is bound (QR scan or a short form),
how messages are received, and how text is sent back. Everything else —
accounts, per-account folder / prompt / execution tool, the Bridge connection,
continuing the same conversation, "新对话", "记住" rules, ordered delivery,
dedupe, the panel page and its API routes — is shared here so every platform
behaves the same way.

The full rules (product, UI, backend contract, acceptance) live in
``skills/bridge-platform-adapter``. The hand-written WeChat channel
(``weixin_bridge.py``) follows the same rules and is the reference this base
was extracted from.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import importlib.util
import json
import logging
import os
import re
import secrets
import shlex
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib import error, request
from assistant_prompt import autonomy_principles

LOG = logging.getLogger("codex_bridge.platform")
ROOT = Path(__file__).resolve().parent
TERMINAL = {"已完成", "失败", "已停止"}
LOGIN_ACTIVE = ("wait", "scaned", "need_verifycode")
RESET_WORDS = ("新对话", "重新开始对话", "/new")
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,23}$")
DEFAULT_ICON = "M4 6h16v12H4z"


class BridgeCallError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def strip_markdown(text):
    """Most chat apps show raw text; keep content, drop markdown syntax."""
    text = re.sub(r"(?m)^#{1,6}\s+|```\w*", "", text or "")
    return re.sub(r"\*\*(.+?)\*\*", r"\1", text)


class PlatformChannel:
    # ---- identity of the platform (override in the subclass) -------------
    ID = ""                 # lowercase id, also the file name: platforms/<ID>.py
    LABEL = ""              # nav + page title, e.g. "Telegram"
    SUBTITLE = ""           # one line under the page title
    ICON = DEFAULT_ICON     # SVG path "d" for a 24x24 stroke icon
    BOT_NAME = ""           # default name of a newly bound bot, e.g. "Telegram 助手"
    BIND_MODE = "form"      # "qr" (official scan) or "form" (token / app credentials)
    BIND_LABEL = ""         # main button, e.g. "＋ 扫码接入微信" / "＋ 接入 Telegram 机器人"
    BIND_HELP = ""          # one or two sentences shown on the bind card
    BIND_FIELDS = ()        # form mode: ({"key", "label", "secret": bool, "placeholder"}, ...)
    REPLY_LIMIT = 1800      # max characters per outgoing message (split longer replies)
    QR_TTL = 300            # seconds a QR code stays valid

    # ---- platform hooks (implement in the subclass) -----------------------
    def bind_form(self, fields):
        """Form mode. Validate credentials against the platform and return the
        account identity dict: {"bot_id", "owner_user_id"?, "secrets": {...}, ...}.
        Raise ValueError with a user-facing message when they are wrong."""
        raise NotImplementedError

    def qr_begin(self):
        """QR mode. Return {"qr_text": str} or {"qr_png": bytes}, plus any
        "state" the poll needs (kept in memory only)."""
        raise NotImplementedError

    def qr_poll(self, login):
        """QR mode. Called about once a second with the login dict (has "state").
        Return (status, identity_or_None, message): status is one of wait /
        scaned / need_verifycode / confirmed / expired. Use login["code"] for a
        verification number the user typed."""
        raise NotImplementedError

    def receive(self, account):
        """Long-poll the platform once (block up to ~30-40s).
        Return (messages, account_updates). Each message is a dict:
        {"message_id", "chat_id", "chat_type": "user"|"group", "sender_id",
         "text", "attachments": [{"name", "data": bytes} | {"name", "path"}],
         "reply_context": str|None}
        account_updates (e.g. a cursor/offset) are saved into the account.
        Raise SessionExpired when the platform says the login is no longer valid."""
        raise NotImplementedError

    def send_text(self, account, target, text):
        """Send one chunk of plain text. target = {"chat_id", "chat_type",
        "sender_id", "reply_context"}. Return True only when the platform
        confirmed it."""
        raise NotImplementedError

    def is_allowed(self, account, msg):
        """Default: only the person who bound or paired the bot. Override for
        allow-lists, but never accept everyone: tasks run on this computer."""
        owner = account.get("owner_user_id")
        return bool(owner) and str(msg.get("sender_id") or "") == str(owner)

    def format_reply(self, text):
        return strip_markdown(text)

    # ------------------------------------------------------------ setup
    def __init__(self, state_dir, bridge_url, store):
        if not ID_RE.match(self.ID or ""):
            raise ValueError("平台 ID 需为小写字母开头的 2-24 位字母、数字或下划线")
        self.dir = Path(state_dir)
        self.config_path = self.dir / ("platform-%s.json" % self.ID)
        self.db_path = self.dir / ("platform-%s.sqlite3" % self.ID)
        self.qr_path = self.dir / ("platform-%s-qr.png" % self.ID)
        self.media_dir = self.dir / ("platform-%s-media" % self.ID)
        self.bridge_url, self.store = bridge_url, store
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.login = None
        self.poll = {}
        self.threads = {}
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS inbox (message_id TEXT PRIMARY KEY, account TEXT NOT NULL,
                conv TEXT NOT NULL, chat_id TEXT, chat_type TEXT, from_user TEXT NOT NULL, text TEXT NOT NULL,
                reply_context TEXT, state TEXT NOT NULL, task_id TEXT, reset INTEGER DEFAULT 0, error TEXT,
                created_at REAL, updated_at REAL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS conversations (conv TEXT PRIMARY KEY, connection_id TEXT,
                last_task TEXT, reply_context TEXT, updated_at REAL)""")
        os.chmod(self.db_path, 0o600)

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def meta(self):
        """What the panel needs to draw the nav entry and page."""
        return {"id": self.ID, "label": self.LABEL, "subtitle": self.SUBTITLE, "icon": self.ICON,
                "bind_mode": self.BIND_MODE, "bind_label": self.BIND_LABEL or "＋ 接入" + (" " if self.LABEL[:1].isascii() else "") + self.LABEL,
                "bind_help": self.BIND_HELP,
                "bind_fields": [{"key": f["key"], "label": f["label"], "secret": bool(f.get("secret")),
                                 "placeholder": f.get("placeholder", "")} for f in self.BIND_FIELDS]}

    # ------------------------------------------------------------ accounts
    def _read(self):
        try:
            return json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, cfg):
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.config_path)

    def accounts(self):
        return self._read().get("accounts") or []

    def account(self, aid):
        return next((a for a in self.accounts() if a["id"] == aid), None)

    def update_account(self, aid, **kw):
        with self.lock:
            accounts = self.accounts()
            for a in accounts:
                if a["id"] == aid:
                    a.update(kw)
                    self._write({"accounts": accounts})
                    return a
        return None

    def _bot_name(self):
        return self.BOT_NAME or self.LABEL + " 助手"

    def _add_or_refresh(self, identity):
        """Binding an account that is already here refreshes it; a new one becomes a new bot."""
        identity = dict(identity)
        display = identity.pop("name", None)
        identity.update(bound_at=time.time(), session_expired=False)
        with self.lock:
            accounts = self.accounts()
            same = next((a for a in accounts if (identity.get("bot_id") and a.get("bot_id") == identity["bot_id"])
                         or (identity.get("owner_user_id") and not identity.get("bot_id")
                             and a.get("owner_user_id") == identity["owner_user_id"])), None)
            if same:
                self.update_account(same["id"], **identity, enabled=True)
                aid, name = same["id"], same.get("name") or self._bot_name()
            else:
                names = {a.get("name") for a in accounts}
                base = display or self._bot_name()
                name = next(n for n in [base] + ["%s %d" % (base, i) for i in range(2, 99)] if n not in names)
                import claude_runtime
                folder = claude_runtime.assistant_workspace_root() / claude_runtime.assistant_folder_name(name)
                folder.mkdir(parents=True, exist_ok=True)
                aid = self.ID[:6] + "_" + uuid.uuid4().hex[:12]
                if not identity.get("owner_user_id"):
                    identity["pair_code"] = "%06d" % secrets.randbelow(10 ** 6)
                accounts.append({"id": aid, "name": name, "prompt": "", "enabled": True,
                                 "project_id": self.store.register_project_path(str(folder))["id"], **identity})
                self._write({"accounts": accounts})
        self._ensure_receiver(aid)
        return name

    # ------------------------------------------------------------ panel API
    def _public(self, a):
        project = conn = None
        try:
            project = self.store.project(a["project_id"]) if a.get("project_id") else None
            conn = self.store.connection(a["connection_id"]) if a.get("connection_id") else None
        except Exception:
            pass
        poll = self.poll.get(a["id"]) or {"ok": False, "error": None}
        pairing = not a.get("owner_user_id")
        if a.get("session_expired"):
            runtime, tone = ("需要重新扫码" if self.BIND_MODE == "qr" else "需要重新接入"), "fail"
        elif not a.get("enabled"):
            runtime, tone = "已停用", "wait"
        elif poll["error"]:
            runtime, tone = "连接异常", "fail"
        elif pairing and poll["ok"]:
            runtime, tone = "等待配对", "wait"
        else:
            runtime, tone = ("已连接", "done") if poll["ok"] else ("正在连接", "run")
        return {"id": a["id"], "name": a.get("name") or self._bot_name(), "enabled": bool(a.get("enabled")),
                "runtime": runtime, "tone": tone, "error": poll["error"], "bound_at": a.get("bound_at"),
                "project_id": a.get("project_id"), "project_name": project and project["name"],
                "project_path": project and project["path"], "prompt": a.get("prompt") or "",
                "execution_backend": conn["execution_backend"] if conn else None,
                "pair_code": a.get("pair_code") if pairing else None}

    def status(self):
        login = None
        if self.login:
            login = {k: self.login.get(k) for k in ("status", "message")}
            if self.login["status"] in LOGIN_ACTIVE and self.qr_path.exists():
                login["qr_url"] = "/api/platforms/%s/qr.png?v=%d" % (self.ID, self.login["started_at"])
        return {"platform": self.meta(), "accounts": [self._public(a) for a in self.accounts()], "login": login}

    def patch(self, aid, data):
        a = self.account(aid)
        if not a:
            raise ValueError("这个机器人不存在")
        if not isinstance(data, dict) or set(data) - {"name", "enabled", "project_id", "prompt", "execution_backend"}:
            raise ValueError("设置内容无效")
        kw = {}
        if "name" in data:
            name = str(data["name"] or "").strip()
            if not 1 <= len(name) <= 60:
                raise ValueError("请填写 1 到 60 字的名称")
            kw["name"] = name
        if "prompt" in data:
            prompt = str(data["prompt"] or "").strip()
            if prompt and prompt.count("{{message}}") != 1:
                raise ValueError("Prompt 需要保留一个 {{message}}")
            kw["prompt"] = prompt
        if "project_id" in data and data["project_id"] != a.get("project_id"):
            self.store.project(data["project_id"])
            kw.update(project_id=data["project_id"], connection_id=None, connection_key=None)
        if "enabled" in data:
            kw["enabled"] = bool(data["enabled"])
        backend = data.get("execution_backend") or (self._public(a)["execution_backend"] if "project_id" in kw else None)
        if backend not in (None, "codex", "claude"):
            raise ValueError("执行工具无效")
        if kw:
            self.update_account(aid, **kw)
        if backend:
            self._connection(aid)
            self.store.set_execution_backend([self.account(aid)["connection_id"]], backend)
        return self.status()

    def delete(self, aid):
        a = self.account(aid)
        if not a:
            raise ValueError("这个机器人不存在")
        if a.get("connection_id"):
            try:
                conn = self.store.connection(a["connection_id"])
            except Exception:
                conn = None
            if conn and not conn.get("deleted_at"):
                self.store.remove_connection(a["connection_id"])   # refuses while tasks are running
        with self.lock:
            self._write({"accounts": [x for x in self.accounts() if x["id"] != aid]})
        with self.db() as db:
            db.execute("UPDATE inbox SET state='dropped' WHERE account=? AND state IN ('received','submitted','notified')", (aid,))
        self.poll.pop(aid, None)
        return self.status()

    # ------------------------------------------------------------ binding
    def bind(self, data):
        """Form mode entry (POST /api/platforms/<id>/bind)."""
        if self.BIND_MODE != "form":
            raise ValueError("这个平台使用扫码接入")
        keys = {f["key"] for f in self.BIND_FIELDS}
        if not isinstance(data, dict) or set(data) - keys:
            raise ValueError("接入信息无效")
        fields = {k: str(data.get(k) or "").strip() for k in keys}
        missing = [f["label"] for f in self.BIND_FIELDS if not fields[f["key"]] and not f.get("optional")]
        if missing:
            raise ValueError("请填写：" + "、".join(missing))
        name = self._add_or_refresh(self.bind_form(fields))
        self.login = {"status": "confirmed", "message": "「%s」已接入" % name, "started_at": int(time.time())}
        return self.status()

    def start_login(self):
        """QR mode entry (POST /api/platforms/<id>/login)."""
        if self.BIND_MODE != "qr":
            raise ValueError("这个平台使用表单接入")
        with self.lock:
            if self.login and self.login["status"] in LOGIN_ACTIVE and time.time() - self.login["started_at"] < self.QR_TTL:
                return self.status()
            begun = self.qr_begin()
            if begun.get("qr_png"):
                self.qr_path.write_bytes(begun["qr_png"])
            elif begun.get("qr_text"):
                self._render_qr(begun["qr_text"])
            else:
                raise RuntimeError("%s没有返回二维码，请稍后重试" % self.LABEL)
            self.login = {"state": begun.get("state"), "code": None, "started_at": int(time.time()),
                          "status": "wait", "message": begun.get("message") or "用手机扫一扫"}
            threading.Thread(target=self._poll_login, args=(self.login,), daemon=True).start()
        return self.status()

    def _render_qr(self, text):
        env = dict(os.environ, PATH=os.path.expanduser("~/.local/bin") + ":" + os.environ.get("PATH", ""))
        subprocess.run(["node", "-e", "require('qrcode').toFile(process.argv[1],process.argv[2],{width:480,margin:2})",
                        str(self.qr_path), text], cwd=ROOT, env=env, check=True, capture_output=True, timeout=30)

    def submit_verify_code(self, code):
        code = str(code or "").strip()
        if not (self.login and self.login["status"] == "need_verifycode" and code.isdigit()):
            raise ValueError("请输入手机上显示的数字")
        self.login.update(code=code, status="scaned", message="正在核对数字…")
        return self.status()

    def _poll_login(self, lg):
        while self.login is lg and time.time() - lg["started_at"] < self.QR_TTL and not self.stop.is_set():
            if lg["status"] == "need_verifycode":
                time.sleep(1)
                continue
            try:
                status, identity, message = self.qr_poll(lg)
            except Exception:
                LOG.debug("%s qr poll failed", self.ID, exc_info=True)
                time.sleep(1)
                continue
            if status == "confirmed" and identity:
                try:
                    lg.update(status="confirmed", message="「%s」已接入" % self._add_or_refresh(identity))
                except Exception as exc:
                    LOG.exception("%s bind failed", self.ID)
                    lg.update(status="expired", message="接入没有完成：%s" % exc)
                return
            if status == "expired":
                lg.update(status="expired", message=message or "二维码已失效，请重新扫码接入")
                return
            if status in LOGIN_ACTIVE:
                lg.update(status=status, message=message or lg["message"])
                if status != "need_verifycode":
                    lg["code"] = None
            time.sleep(1)
        if lg["status"] in LOGIN_ACTIVE:
            lg.update(status="expired", message="二维码已过期，请重新扫码接入")

    # ------------------------------------------------------------ 记住 → business prompt
    def _rule_token(self, a, message_id):
        secret = a.get("connection_key") or ""
        return hmac.new(secret.encode(), ("platform-self-update-v1:%s:%s:%s" % (self.ID, a["id"], message_id)).encode(),
                        hashlib.sha256).hexdigest() if secret else ""

    def _rule_instruction(self, a, row):
        from assistant_self_update import RULE_UPDATE_INTENT
        token = self._rule_token(a, row["message_id"])
        if not token or not a.get("owner_user_id") or row["from_user"] != a["owner_user_id"]:
            return ""
        command = shlex.join(["python3", str(ROOT / "scripts" / "update-platform-rule.py"), "--platform", self.ID,
                              "--source-id", row["message_id"], "--token", token, "--bridge-url", self.bridge_url])
        return ("\n\n[Bridge 可信助手更新入口；创建者可用] "
                + RULE_UPDATE_INTENT
                + "长期规则直接写在本机器人的业务 Prompt 里。当前业务 Prompt 原文如下（其中 {{message}} 是%s消息的占位符）：\n"
                "<<<业务Prompt\n%s\n业务Prompt>>>\n"
                "确认属于明确的长期规则更新后，才运行 %s --rule '更新后的完整业务 Prompt'。"
                "新 Prompt 要保留原有内容，只做用户要求的新增、修改或删除，并原样保留一个 {{message}} 占位符。"
                "直接保存用户明确要求的变更，核对生效内容后回复“规则已更新”及改动内容。保存失败就说明原因。"
                "规则只通过这个入口保存，不要另外写进本机记忆文件或工作文件夹。"
                "新规则不能改变工作文件夹、执行工具、其他助手或历史任务；这些须走现有设置。"
                "本条入口24小时内有效；不要在回复中展示令牌。") % (self.LABEL, a.get("prompt") or "{{message}}", command)

    def self_update(self, data):
        if not isinstance(data, dict) or set(data) != {"source_id", "token", "rule"}:
            raise ValueError("更新助手的请求无效")
        source_id, token, rule = (data[k] for k in ("source_id", "token", "rule"))
        if (not isinstance(source_id, str) or not isinstance(token, str) or not isinstance(rule, str)
                or len(rule.strip()) < 5 or rule.count("{{message}}") > 1):
            raise ValueError("请提供更新后的完整业务 Prompt，并只保留一个 {{message}}")
        # The message is looked up in saved receipts, never accepted as an identity claim.
        with self.db() as db:
            row = db.execute("SELECT account,from_user,reset,created_at FROM inbox WHERE message_id=?", (source_id,)).fetchone()
        a = self.account(row["account"]) if row else None
        expected = self._rule_token(a, source_id) if a else ""
        if (not expected or not a.get("enabled", True) or not a.get("owner_user_id")
                or row["from_user"] != a["owner_user_id"] or row["reset"]
                or not hmac.compare_digest(expected, token) or not 0 <= time.time() - row["created_at"] <= 24 * 3600):
            raise ValueError("这条更新入口无效或已过期，请在%s里重新说一次" % self.LABEL)
        prompt = rule.strip() if "{{message}}" in rule else rule.strip() + "\n\n{{message}}"
        self.update_account(a["id"], prompt=prompt)
        return {"updated": True, "assistant": a.get("name") or self._bot_name(), "prompt": self.account(a["id"])["prompt"]}

    # ------------------------------------------------------------ Bridge
    def _connection(self, aid):
        with self.lock:
            a = self.account(aid)
            if a.get("connection_id") and a.get("connection_key"):
                try:
                    if not self.store.connection(a["connection_id"]).get("deleted_at"):
                        return a["connection_key"]
                except Exception:
                    pass
            c = self.store.create_connection(a.get("name") or self._bot_name(), a["project_id"])
            self.update_account(aid, connection_id=c["id"], connection_key=c["api_key"])
            return c["api_key"]

    def _bridge(self, aid, method, path, payload=None):
        req = request.Request(self.bridge_url + path, method=method,
                              data=json.dumps(payload).encode() if payload is not None else None,
                              headers={"Authorization": "Bearer " + self._connection(aid), "Content-Type": "application/json"})
        try:
            with request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except error.HTTPError as exc:
            err = (json.loads(exc.read() or b"{}").get("error") or {})
            raise BridgeCallError(err.get("code") or str(exc.code), err.get("message") or str(exc)) from None

    # ------------------------------------------------------------ loops
    def start(self):
        for a in self.accounts():
            self._ensure_receiver(a["id"])
        threading.Thread(target=self._deliver_loop, name="%s-deliver" % self.ID, daemon=True).start()

    def close(self):
        self.stop.set()

    def _ensure_receiver(self, aid):
        with self.lock:
            if not (self.threads.get(aid) and self.threads[aid].is_alive()):
                self.threads[aid] = threading.Thread(target=self._receive_loop, args=(aid,),
                                                     name="%s-receive-%s" % (self.ID, aid), daemon=True)
                self.threads[aid].start()

    def _receive_loop(self, aid):
        poll = self.poll.setdefault(aid, {"ok": False, "error": None})
        while not self.stop.is_set():
            a = self.account(aid)
            if not a:
                return      # deleted
            if not (a.get("enabled") and a.get("project_id")) or a.get("session_expired"):
                poll.update(ok=False, error=None)
                self.stop.wait(2)
                continue
            try:
                messages, updates = self.receive(a)
            except SessionExpired:
                self.update_account(aid, session_expired=True)
                poll.update(ok=False, error="登录已失效")
                continue
            except Exception as exc:
                if "timed out" not in str(exc):
                    LOG.warning("%s receive failed: %s", self.ID, exc)
                    poll.update(ok=False, error="网络错误")
                    self.stop.wait(5)
                continue
            poll.update(ok=True, error=None)
            for msg in messages or []:
                try:
                    self.accept(a, msg)
                except Exception:
                    LOG.exception("%s inbound message failed", self.ID)
            if updates:
                self.update_account(aid, **updates)

    def accept(self, a, msg):
        """Store one normalized inbound message (idempotent by message_id)."""
        if not a.get("owner_user_id"):
            return self._pair(a, msg)
        if not self.is_allowed(a, msg):
            return False
        sender = str(msg.get("sender_id") or "")
        chat_type = "group" if msg.get("chat_type") == "group" else "user"
        chat_id = str(msg.get("chat_id") or sender)
        mid = a["id"] + ":" + str(msg.get("message_id") or uuid.uuid4().hex)
        conv = "%s:%s:%s" % (a["id"], chat_type, chat_id) + (":" + sender if chat_type == "group" else "")
        parts = [str(msg.get("text") or "").strip()]
        for att in msg.get("attachments") or []:
            path = self._save_attachment(mid, att)
            parts.append("附件已保存在本机：" + path if path else "[附件下载失败：%s]" % (att.get("name") or "文件"))
        text = "\n".join(p for p in parts if p).strip()
        if not text:
            return False
        t = time.time()
        with self.db() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO inbox (message_id,account,conv,chat_id,chat_type,from_user,text,reply_context,"
                "state,reset,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'received',?,?,?)",
                (mid, a["id"], conv, chat_id, chat_type, sender, text, msg.get("reply_context"),
                 int(text in RESET_WORDS), t, t)).rowcount
            db.execute("INSERT INTO conversations (conv,reply_context,updated_at) VALUES (?,?,?) ON CONFLICT(conv) DO "
                       "UPDATE SET reply_context=COALESCE(excluded.reply_context,reply_context),updated_at=excluded.updated_at",
                       (conv, msg.get("reply_context"), t))
        return bool(inserted)

    def _pair(self, a, msg):
        """Unclaimed bot: the first private message carrying the panel's code claims it."""
        code = a.get("pair_code")
        text = re.sub(r"\s+", "", str(msg.get("text") or ""))
        if not code or msg.get("chat_type") == "group" or text not in (code, "配对" + code):
            return False
        sender = str(msg.get("sender_id") or "")
        if not sender:
            return False
        self.update_account(a["id"], owner_user_id=sender, pair_code=None)
        try:
            self.send_text(a, {"chat_id": str(msg.get("chat_id") or sender), "chat_type": "user",
                               "sender_id": sender, "reply_context": msg.get("reply_context")},
                           "配对成功。之后直接发消息给我就行。")
        except Exception:
            LOG.warning("%s pairing reply failed", self.ID)
        return False

    def _save_attachment(self, mid, att):
        try:
            if att.get("path"):
                return str(att["path"])
            data = att.get("data")
            if data is None:
                return None
            name = re.sub(r"[/\\\x00]", "_", att.get("name") or "file")[:120]
            path = self.media_dir / re.sub(r"\W", "_", mid) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o600)
            return str(path)
        except Exception:
            LOG.exception("%s attachment save failed", self.ID)
            return None

    def _deliver_loop(self):
        while not self.stop.wait(2):
            try:
                self.deliver_once()
            except Exception:
                LOG.exception("%s delivery failed", self.ID)

    def deliver_once(self):
        accounts = {a["id"]: a for a in self.accounts()}
        with self.db() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM inbox WHERE state IN ('received','submitted','notified') ORDER BY created_at")]
        busy = {r["conv"] for r in rows if r["state"] != "received"}
        for row in rows:
            a = accounts.get(row["account"])
            if not (a and a.get("project_id")):
                continue
            if row["state"] != "received":
                self._check(row, a)
            elif row["conv"] not in busy:   # one message at a time per conversation, in order
                busy.add(row["conv"])
                self._submit(row, a)

    def _set(self, mid, **kw):
        with self.db() as db:
            db.execute("UPDATE inbox SET %s,updated_at=? WHERE message_id=?" % ",".join(k + "=?" for k in kw),
                       (*kw.values(), time.time(), mid))

    def _submit(self, row, a):
        with self.db() as db:
            conv = db.execute("SELECT * FROM conversations WHERE conv=?", (row["conv"],)).fetchone()
            if row["reset"]:
                db.execute("UPDATE conversations SET last_task=NULL WHERE conv=?", (row["conv"],))
        if row["reset"]:
            self._set(row["message_id"], state="done")
            return self._send(row, "好的，已开始新对话。", a)
        self._connection(a["id"])
        a = self.account(a["id"])
        message = (a.get("prompt") or "{{message}}").replace("{{message}}", row["text"]) + autonomy_principles() + self._rule_instruction(a, row)
        payload = {"message": message[:12000],
                   "request_id": (self.ID[:6] + "-" + re.sub(r"[^\w.:-]", "", row["message_id"]))[-100:],
                   "private": row["chat_type"] != "group"}
        if conv and conv["last_task"] and conv["connection_id"] == a.get("connection_id"):
            payload["continue_from"] = conv["last_task"]
        try:
            task = self._bridge(a["id"], "POST", "/v1/tasks", payload)
        except BridgeCallError as exc:
            if exc.code == "conversation_busy":
                return
            if payload.get("continue_from") and exc.code in ("task_not_found", "historical_task"):
                with self.db() as db:
                    db.execute("UPDATE conversations SET last_task=NULL WHERE conv=?", (row["conv"],))
                return
            self._set(row["message_id"], state="failed", error=exc.code)
            return self._send(row, "没能交给助手处理：" + exc.message, a)
        except Exception:
            return  # Bridge briefly unavailable; retry next round
        self._set(row["message_id"], state="submitted", task_id=task["task_id"])
        with self.db() as db:
            db.execute("UPDATE conversations SET connection_id=?,last_task=?,updated_at=? WHERE conv=?",
                       (a.get("connection_id"), task["task_id"], time.time(), row["conv"]))

    def _check(self, row, a):
        try:
            task = self._bridge(a["id"], "GET", "/v1/tasks/" + row["task_id"])
        except Exception:
            return
        final = task["status"] in TERMINAL
        if (final or (task["status"] == "需要你处理" and row["state"] == "submitted")) and \
                self._send(row, self.reply_text(task), a):
            self._set(row["message_id"], state="done" if final else "notified")

    def reply_text(self, task):
        text = self.format_reply(str(task.get("result") or "").strip())
        if task["status"] == "已完成":
            return text or "这次没有返回结果，请在 Bridge 面板查看。"
        return {"失败": "这次没能完成。", "已停止": "这次已停止。"}.get(task["status"], "任务需要你在 Bridge 面板处理。") + \
            ("\n" + text if text else "")

    def _send(self, row, text, a):
        with self.db() as db:
            ctx = db.execute("SELECT reply_context FROM conversations WHERE conv=?", (row["conv"],)).fetchone()
        target = {"chat_id": row.get("chat_id"), "chat_type": row.get("chat_type"), "sender_id": row["from_user"],
                  "reply_context": (ctx and ctx[0]) or row.get("reply_context")}
        try:
            for i in range(0, max(len(text), 1), self.REPLY_LIMIT):
                if not self.send_text(a, target, text[i:i + self.REPLY_LIMIT]):
                    return False
            return True
        except Exception as exc:
            LOG.warning("%s send failed: %s", self.ID, exc)
            return False


class SessionExpired(Exception):
    """Raise from receive() when the platform login/token is no longer valid."""


# ---------------------------------------------------------------- registry
def platform_dirs():
    dirs = [ROOT / "platforms"]
    dirs += [Path(p) for p in os.getenv("BRIDGE_PLATFORM_DIRS", "").split(os.pathsep) if p]
    return dirs


def load_platform_classes(dirs=None):
    """Import every platforms/<id>.py (files starting with "_" are skipped)."""
    found = {}
    for folder in dirs or platform_dirs():
        for path in sorted(Path(folder).glob("*.py")) if Path(folder).is_dir() else []:
            if path.name.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location("bridge_platform_" + path.stem, path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                cls = getattr(module, "Channel")
                if not (isinstance(cls, type) and issubclass(cls, PlatformChannel)):
                    raise TypeError("Channel 必须继承 PlatformChannel")
                if cls.ID != path.stem:
                    raise ValueError("Channel.ID (%s) 必须与文件名 %s 一致" % (cls.ID, path.stem))
                if cls.ID in found or cls.ID in ("feishu", "weixin", "tasks", "connections"):
                    raise ValueError("平台 ID %s 已被占用" % cls.ID)
                found[cls.ID] = cls
            except Exception:
                LOG.exception("platform %s failed to load; skipped", path.name)
    return found


def load_platforms(state_dir, bridge_url, store, dirs=None):
    channels = {}
    for pid, cls in load_platform_classes(dirs).items():
        try:
            channels[pid] = cls(state_dir, bridge_url, store)
        except Exception:
            LOG.exception("platform %s failed to start; skipped", pid)
    return channels


def handle_api(channels, method, path, read_json):
    """Dispatch /api/platforms[...]. Returns (status, payload, extra) or None.
    extra = {"file": Path, "mime": str} to serve a file instead of JSON."""
    if path == "/api/platforms" and method == "GET":
        return 200, {"platforms": [c.meta() for c in (channels or {}).values()]}, None
    parts = path.split("/")[3:]      # ["<id>", ...]
    channel = (channels or {}).get(parts[0]) if parts else None
    if not channel:
        return None
    rest = "/".join(parts[1:])
    if rest == "" and method == "GET":
        return 200, channel.status(), None
    if rest.startswith("accounts/") and len(parts) == 3 and method == "PATCH":
        return 200, channel.patch(parts[2], read_json()), None
    if rest.startswith("accounts/") and len(parts) == 4 and parts[3] == "delete" and method == "POST":
        return 200, channel.delete(parts[2]), None
    if rest == "bind" and method == "POST":
        return 200, channel.bind(read_json()), None
    if rest == "login" and method == "POST":
        return 200, channel.start_login(), None
    if rest == "login/verify" and method == "POST":
        return 200, channel.submit_verify_code(read_json().get("code")), None
    if rest == "self-update" and method == "POST":
        return 200, channel.self_update(read_json()), None
    if rest == "qr.png" and method == "GET" and channel.qr_path.exists():
        return 200, None, {"file": channel.qr_path, "mime": "image/png"}
    return None
