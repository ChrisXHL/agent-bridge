"""WeChat ClawBot channel for Bridge.

Speaks the iLink bot protocol of the official @tencent-weixin/openclaw-weixin
plugin: QR login, getupdates long-poll, sendmessage. Every scanned WeChat is
one independent bot with its own folder, prompt and Bridge connection. Each
message from the bound user becomes a Bridge task (continuing that user's
conversation) and the task result is sent back to the same chat.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
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
from urllib.parse import quote

LOG = logging.getLogger("codex_bridge.weixin")
ROOT = Path(__file__).resolve().parent
ILINK_BASE = "https://ilinkai.weixin.qq.com"
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
HEADERS = {"iLink-App-Id": "bot", "iLink-App-ClientVersion": str(2 << 16 | 4 << 8 | 10)}  # plugin 2.4.10
BASE_INFO = {"channel_version": "2.4.10", "bot_agent": "CodexBridge/2.6"}
TERMINAL = {"已完成", "失败", "已停止"}
DEFAULT_NAME = "微信 ClawBot"
LOGIN_ACTIVE = ("wait", "scaned", "need_verifycode")


def ilink(base, endpoint, body=None, token=None, timeout=15):
    """POST (body given) or GET an iLink endpoint; uint64 ids are kept as strings."""
    headers = dict(HEADERS)
    data = None
    if body is not None:
        data = json.dumps({**body, "base_info": BASE_INFO}).encode()
        headers.update({"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
                        "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode()})
        if token:
            headers["Authorization"] = "Bearer " + token
    req = request.Request((base or ILINK_BASE).rstrip("/") + "/" + endpoint, data=data, headers=headers)
    with request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
    return json.loads(re.sub(r'("(?:message_id|msg_id|svr_id)"\s*:\s*)(-?\d+)', r'\1"\2"', raw))


def reply_text(task):
    text = str(task.get("result") or "").strip()
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", re.sub(r"(?m)^#{1,6}\s+|```\w*", "", text))  # WeChat shows raw text
    if task["status"] == "已完成":
        return text or "这次没有返回结果，请在 Bridge 面板查看。"
    return {"失败": "这次没能完成。", "已停止": "这次已停止。"}.get(task["status"], "任务需要你在 Bridge 面板处理。") + \
        ("\n" + text if text else "")


class WeixinChannel:
    def __init__(self, state_dir, bridge_url, store):
        self.dir = Path(state_dir)
        self.config_path = self.dir / "weixin.json"
        self.db_path = self.dir / "weixin.sqlite3"
        self.qr_path = self.dir / "weixin-qr.png"
        self.bridge_url, self.store = bridge_url, store
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.login = None
        self.poll = {}      # account id -> {"ok": bool, "error": str|None}
        self.threads = {}
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS inbox (message_id TEXT PRIMARY KEY, conv TEXT NOT NULL,
                from_user TEXT NOT NULL, text TEXT NOT NULL, context_token TEXT, state TEXT NOT NULL, task_id TEXT,
                reset INTEGER DEFAULT 0, error TEXT, created_at REAL, updated_at REAL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS conversations (conv TEXT PRIMARY KEY, connection_id TEXT,
                last_task TEXT, context_token TEXT, updated_at REAL)""")
            if "account" not in [r[1] for r in db.execute("PRAGMA table_info(inbox)")]:
                db.execute("ALTER TABLE inbox ADD COLUMN account TEXT")
        os.chmod(self.db_path, 0o600)
        self._migrate_single_account()
        for a in self.accounts():       # rules once kept beside the prompt now live in the prompt itself
            if a.get("self_update_rule"):
                base = a.get("prompt") or "{{message}}"
                self.update_account(a["id"], self_update_rule=None,
                                    prompt=a["self_update_rule"].strip() + "\n\n" + base if base == "{{message}}"
                                    else base + "\n\n" + a["self_update_rule"].strip())

    def db(self):
        db = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    # ------------------------------------------------------------ config
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

    def _migrate_single_account(self):
        """Earlier versions kept one binding at the top level of weixin.json."""
        old = self._read()
        if "accounts" in old:
            return
        accounts = []
        if old.get("bot_token"):
            aid = "wx_" + uuid.uuid4().hex[:12]
            keys = ("bot_token", "bot_id", "base_url", "owner_user_id", "bound_at", "enabled", "project_id",
                    "connection_id", "connection_key", "get_updates_buf", "session_expired")
            accounts.append({"id": aid, "name": DEFAULT_NAME, "prompt": "", **{k: old.get(k) for k in keys}})
            with self.db() as db:
                db.execute("UPDATE inbox SET account=?,conv=?||':'||conv WHERE account IS NULL", (aid, aid))
                db.execute("UPDATE conversations SET conv=?||':'||conv WHERE conv NOT LIKE 'wx\\_%' ESCAPE '\\'", (aid,))
        self._write({"accounts": accounts})

    # ------------------------------------------------------------ panel API
    def _public(self, a):
        project = conn = None
        try:
            project = self.store.project(a["project_id"]) if a.get("project_id") else None
            conn = self.store.connection(a["connection_id"]) if a.get("connection_id") else None
        except Exception:
            pass
        poll = self.poll.get(a["id"]) or {"ok": False, "error": None}
        if a.get("session_expired"):
            runtime, tone = "需要重新扫码", "fail"
        elif not a.get("enabled"):
            runtime, tone = "已停用", "wait"
        elif poll["error"]:
            runtime, tone = "连接异常", "fail"
        else:
            runtime, tone = ("已连接", "done") if poll["ok"] else ("正在连接", "run")
        return {"id": a["id"], "name": a.get("name") or DEFAULT_NAME, "enabled": bool(a.get("enabled")),
                "runtime": runtime, "tone": tone, "error": poll["error"],
                "owner": (a.get("owner_user_id") or "")[:8], "bound_at": a.get("bound_at"),
                "project_id": a.get("project_id"), "project_name": project and project["name"],
                "project_path": project and project["path"], "prompt": a.get("prompt") or "",
                "execution_backend": conn["execution_backend"] if conn else None}

    def status(self):
        login = None
        if self.login:
            login = {k: self.login.get(k) for k in ("status", "message")}
            if self.login["status"] in LOGIN_ACTIVE:
                login["qr_url"] = "/api/weixin/qr.png?v=%d" % self.login["started_at"]
        return {"accounts": [self._public(a) for a in self.accounts()], "login": login}

    def patch(self, aid, data):
        a = self.account(aid)
        if not a:
            raise ValueError("这个微信机器人不存在")
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
            if prompt and "{{message}}" not in prompt:
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
            raise ValueError("这个微信机器人不存在")
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

    # ------------------------------------------------------------ 记住 → business prompt
    def _rule_token(self, a, message_id):
        secret = a.get("connection_key") or ""
        return hmac.new(secret.encode(), ("weixin-self-update-v1:%s:%s" % (a["id"], message_id)).encode(),
                        hashlib.sha256).hexdigest() if secret else ""

    def _rule_instruction(self, a, row):
        from assistant_self_update import RULE_UPDATE_INTENT
        token = self._rule_token(a, row["message_id"])
        if not token or not a.get("owner_user_id") or row["from_user"] != a["owner_user_id"]:
            return ""
        command = shlex.join(["python3", str(ROOT / "scripts" / "update-weixin-rule.py"), "--source-id", row["message_id"],
                              "--token", token, "--bridge-url", self.bridge_url])
        return ("\n\n[Bridge 可信助手更新入口；创建者可用] "
                + RULE_UPDATE_INTENT
                + "长期规则直接写在本机器人的业务 Prompt 里。当前业务 Prompt 原文如下（其中 {{message}} 是微信消息的占位符）：\n"
                "<<<业务Prompt\n%s\n业务Prompt>>>\n"
                "确认属于明确的长期规则更新后，才运行 %s --rule '更新后的完整业务 Prompt'。"
                "新 Prompt 要保留原有内容，只做用户要求的新增、修改或删除，并原样保留一个 {{message}} 占位符。"
                "直接保存用户明确要求的变更，核对生效内容后回复“规则已更新”及改动内容。保存失败就说明原因。"
                "规则只通过这个入口保存，不要另外写进本机记忆文件或工作文件夹。"
                "新规则不能改变工作文件夹、执行工具、其他助手或历史任务；这些须走现有设置。"
                "本条入口24小时内有效；不要在回复中展示令牌。") % (a.get("prompt") or "{{message}}", command)

    def self_update(self, data):
        if not isinstance(data, dict) or set(data) != {"source_id", "token", "rule", "clear"}:
            raise ValueError("更新助手的请求无效")
        source_id, token, rule, clear = (data[k] for k in ("source_id", "token", "rule", "clear"))
        if clear:
            raise ValueError("请用 --rule 提交修改后的完整业务 Prompt")
        if (not isinstance(source_id, str) or not isinstance(token, str) or not isinstance(rule, str)
                or len(rule.strip()) < 5 or rule.count("{{message}}") > 1):
            raise ValueError("请提供更新后的完整业务 Prompt，并只保留一个 {{message}}")
        # The message is looked up in saved WeChat receipts, never accepted as an identity claim.
        with self.db() as db:
            row = db.execute("SELECT account,from_user,reset,created_at FROM inbox WHERE message_id=?", (source_id,)).fetchone()
        a = self.account(row["account"]) if row else None
        expected = self._rule_token(a, source_id) if a else ""
        if (not expected or not a.get("enabled", True) or not a.get("owner_user_id")
                or row["from_user"] != a["owner_user_id"] or row["reset"]
                or not hmac.compare_digest(expected, token) or not 0 <= time.time() - row["created_at"] <= 24 * 3600):
            raise ValueError("这条更新入口无效或已过期，请在微信里重新说一次")
        prompt = rule.strip() if "{{message}}" in rule else rule.strip() + "\n\n{{message}}"
        self.update_account(a["id"], prompt=prompt)
        return {"updated": True, "assistant": a.get("name") or DEFAULT_NAME, "prompt": self.account(a["id"])["prompt"]}

    # ------------------------------------------------------------ QR login
    def start_login(self):
        with self.lock:
            if self.login and self.login["status"] in LOGIN_ACTIVE and time.time() - self.login["started_at"] < 300:
                return self.status()
            qr = ilink(None, "ilink/bot/get_bot_qrcode?bot_type=3", {"local_token_list": []})
            if not qr.get("qrcode_img_content"):
                raise RuntimeError("微信没有返回二维码，请稍后重试")
            env = dict(os.environ, PATH=os.path.expanduser("~/.local/bin") + ":" + os.environ.get("PATH", ""))
            subprocess.run(["node", "-e", "require('qrcode').toFile(process.argv[1],process.argv[2],{width:480,margin:2})",
                            str(self.qr_path), qr["qrcode_img_content"]], cwd=ROOT, env=env, check=True,
                           capture_output=True, timeout=30)
            self.login = {"qrcode": qr["qrcode"], "base": ILINK_BASE, "code": None, "started_at": int(time.time()),
                          "status": "wait", "message": "用手机微信扫一扫"}
            threading.Thread(target=self._poll_login, args=(self.login,), daemon=True).start()
        return self.status()

    def submit_verify_code(self, code):
        code = str(code or "").strip()
        if not (self.login and self.login["status"] == "need_verifycode" and code.isdigit()):
            raise ValueError("请输入手机微信上显示的数字")
        self.login.update(code=code, status="scaned", message="正在核对数字…")
        return self.status()

    def _poll_login(self, lg):
        while self.login is lg and time.time() - lg["started_at"] < 300 and not self.stop.is_set():
            if lg["status"] == "need_verifycode":
                time.sleep(1)
                continue
            ep = "ilink/bot/get_qrcode_status?qrcode=" + quote(lg["qrcode"]) + \
                ("&verify_code=" + quote(lg["code"]) if lg["code"] else "")
            try:
                st = ilink(lg["base"], ep, timeout=38)
            except Exception:
                time.sleep(1)
                continue
            s = st.get("status")
            if s == "scaned":
                lg.update(status="scaned", message="已扫码，请在手机上确认", code=None)
            elif s == "scaned_but_redirect" and st.get("redirect_host"):
                lg["base"] = "https://" + st["redirect_host"]
            elif s == "need_verifycode":
                lg.update(status=s, message="数字不对，请重新输入" if lg["code"] else "请输入手机微信上显示的数字", code=None)
            elif s == "confirmed" and st.get("bot_token"):
                try:
                    name = self._bind(st)
                    lg.update(status="confirmed", message="「%s」已接入" % name)
                except Exception as exc:
                    LOG.exception("weixin bind failed")
                    lg.update(status="expired", message="接入没有完成：%s" % exc)
                return
            elif s and s not in ("wait", "scaned_but_redirect"):
                lg.update(status="expired", message="二维码已失效，请重新扫码接入")
                return
            time.sleep(1)
        if lg["status"] in LOGIN_ACTIVE:
            lg.update(status="expired", message="二维码已过期，请重新扫码接入")

    def _bind(self, st):
        """Scanning a WeChat that is already here refreshes it; a new one becomes a new bot."""
        fresh = dict(bot_token=st["bot_token"], bot_id=st.get("ilink_bot_id"), base_url=st.get("baseurl") or ILINK_BASE,
                     owner_user_id=st.get("ilink_user_id"), bound_at=time.time(), session_expired=False,
                     get_updates_buf="")
        with self.lock:
            accounts = self.accounts()
            same = next((a for a in accounts if (fresh["owner_user_id"] and a.get("owner_user_id") == fresh["owner_user_id"])
                         or (fresh["bot_id"] and a.get("bot_id") == fresh["bot_id"])), None)
            if same:
                self.update_account(same["id"], **fresh, enabled=True)
                aid, name = same["id"], same.get("name") or DEFAULT_NAME
            else:
                names = {a.get("name") for a in accounts}
                name = next(n for n in [DEFAULT_NAME] + ["%s %d" % (DEFAULT_NAME, i) for i in range(2, 99)] if n not in names)
                import claude_runtime
                folder = claude_runtime.assistant_workspace_root() / claude_runtime.assistant_folder_name(name)
                folder.mkdir(parents=True, exist_ok=True)
                aid = "wx_" + uuid.uuid4().hex[:12]
                accounts.append({"id": aid, "name": name, "prompt": "", "enabled": True,
                                 "project_id": self.store.register_project_path(str(folder))["id"], **fresh})
                self._write({"accounts": accounts})
        self._ensure_receiver(aid)
        return name

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
            c = self.store.create_connection(a.get("name") or DEFAULT_NAME, a["project_id"])
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
        threading.Thread(target=self._deliver_loop, name="weixin-deliver", daemon=True).start()

    def close(self):
        self.stop.set()

    def _ensure_receiver(self, aid):
        with self.lock:
            if not (self.threads.get(aid) and self.threads[aid].is_alive()):
                self.threads[aid] = threading.Thread(target=self._receive_loop, args=(aid,), daemon=True)
                self.threads[aid].start()

    def _receive_loop(self, aid):
        poll = self.poll.setdefault(aid, {"ok": False, "error": None})
        while not self.stop.is_set():
            a = self.account(aid)
            if not a:
                return      # deleted
            if not (a.get("enabled") and a.get("bot_token") and a.get("project_id")) or a.get("session_expired"):
                poll.update(ok=False, error=None)
                self.stop.wait(2)
                continue
            try:
                resp = ilink(a.get("base_url"), "ilink/bot/getupdates",
                             {"get_updates_buf": a.get("get_updates_buf") or ""}, a["bot_token"], timeout=40)
            except Exception as exc:
                if "timed out" not in str(exc):
                    poll.update(ok=False, error="网络错误")
                    self.stop.wait(5)
                continue
            code = resp.get("errcode") or resp.get("ret")
            if code:
                if code == -14:
                    self.update_account(aid, session_expired=True)
                poll.update(ok=False, error="微信返回错误 %s" % code)
                self.stop.wait(5)
                continue
            poll.update(ok=True, error=None)
            for msg in resp.get("msgs") or []:
                try:
                    self._accept(msg, a)
                except Exception:
                    LOG.exception("weixin inbound message failed")
            if resp.get("get_updates_buf") is not None:
                self.update_account(aid, get_updates_buf=resp["get_updates_buf"])

    def _accept(self, msg, a):
        sender = msg.get("from_user_id") or ""
        if msg.get("message_type") != 1 or (a.get("owner_user_id") and sender != a["owner_user_id"]):
            return  # only the bound user's own messages
        mid = a["id"] + ":" + str(msg.get("message_id") or msg.get("client_id") or uuid.uuid4().hex)
        conv = a["id"] + ":" + ("group:" + msg["group_id"] if msg.get("group_id") else "user:" + sender)
        parts = []
        for item in msg.get("item_list") or []:
            if item.get("type") == 1:
                parts.append((item.get("text_item") or {}).get("text") or "")
            elif item.get("type") == 3:
                parts.append((item.get("voice_item") or {}).get("text") or "[语音未能转成文字]")
            elif item.get("type") in (2, 4, 5):
                path = self._download(mid, item)
                parts.append("附件已保存在本机：" + path if path else "[附件下载失败]")
        text = "\n".join(p for p in parts if p).strip()
        if not text:
            return
        t = time.time()
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO inbox (message_id,account,conv,from_user,text,context_token,state,reset,created_at,updated_at)"
                       " VALUES (?,?,?,?,?,?,'received',?,?,?)",
                       (mid, a["id"], conv, sender, text, msg.get("context_token"), int(text in ("新对话", "/new")), t, t))
            db.execute("INSERT INTO conversations (conv,context_token,updated_at) VALUES (?,?,?) ON CONFLICT(conv) DO "
                       "UPDATE SET context_token=COALESCE(excluded.context_token,context_token),updated_at=excluded.updated_at",
                       (conv, msg.get("context_token"), t))

    def _download(self, mid, item):
        body = item.get({2: "image_item", 4: "file_item", 5: "video_item"}[item["type"]]) or {}
        media = body.get("media") or {}
        if not (media.get("full_url") or media.get("encrypt_query_param")):
            return None
        try:
            key = bytes.fromhex(body["aeskey"]) if body.get("aeskey") else base64.b64decode(media.get("aes_key") or "")
            if len(key) == 32:
                key = bytes.fromhex(key.decode())
            url = media.get("full_url") or CDN_BASE + "/download?encrypted_query_param=" + quote(media["encrypt_query_param"])
            with request.urlopen(url, timeout=60) as r:
                data = r.read()
            if key:
                data = subprocess.run(["openssl", "enc", "-d", "-aes-128-ecb", "-K", key.hex()], input=data,
                                      capture_output=True, check=True, timeout=60).stdout
            name = re.sub(r"[/\\\x00]", "_", body.get("file_name") or {2: "image.jpg", 5: "video.mp4"}.get(item["type"], "file"))
            path = self.dir / "weixin-media" / re.sub(r"\W", "_", mid) / name[:120]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o600)
            return str(path)
        except Exception:
            LOG.exception("weixin media download failed")
            return None

    def _deliver_loop(self):
        while not self.stop.wait(2):
            try:
                self._deliver_once()
            except Exception:
                LOG.exception("weixin delivery failed")

    def _deliver_once(self):
        accounts = {a["id"]: a for a in self.accounts()}
        with self.db() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT * FROM inbox WHERE state IN ('received','submitted','notified') ORDER BY created_at")]
        busy = {r["conv"] for r in rows if r["state"] != "received"}
        for row in rows:
            a = accounts.get(row["account"])
            if not (a and a.get("bot_token") and a.get("project_id")):
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
        message = (a.get("prompt") or "{{message}}").replace("{{message}}", row["text"]) + self._rule_instruction(a, row)
        payload = {"message": message[:12000], "request_id": "wx-" + re.sub(r"[^\w.:-]", "", row["message_id"])[-100:],
                   "private": True}
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
        if (final or (task["status"] == "需要你处理" and row["state"] == "submitted")) and self._send(row, reply_text(task), a):
            self._set(row["message_id"], state="done" if final else "notified")

    def _send(self, row, text, a):
        with self.db() as db:
            ctx = db.execute("SELECT context_token FROM conversations WHERE conv=?", (row["conv"],)).fetchone()
        try:
            for i in range(0, max(len(text), 1), 1800):
                resp = ilink(a.get("base_url"), "ilink/bot/sendmessage", {"msg": {
                    "from_user_id": "", "to_user_id": row["from_user"], "client_id": "bridge-" + uuid.uuid4().hex,
                    "message_type": 2, "message_state": 2, "context_token": ctx and ctx[0],
                    "item_list": [{"type": 1, "text_item": {"text": text[i:i + 1800]}}]}}, a["bot_token"])
                if resp.get("ret") or resp.get("errcode"):
                    LOG.warning("weixin send failed ret=%s errcode=%s", resp.get("ret"), resp.get("errcode"))
                    return False
            return True
        except Exception as exc:
            LOG.warning("weixin send failed: %s", exc)
            return False


class BridgeCallError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message
