"""Local patch tests: backup Claude token takes over when the browser login is unusable."""
import json
import os
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import claude_auth
import claude_runtime
from claude_runtime import ClaudeTransport

TOKEN = "sk-ant-oat01-" + "x" * 60

FAKE = textwrap.dedent('''\
    #!%s
    import json, sys, os
    args = sys.argv[1:]
    state = os.environ["FAKE_STATE"]
    if args[:2] == ["auth", "status"]:
        print(open(os.path.join(state, "status.json")).read()); sys.exit(0)
    msg = sys.stdin.read()
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    with open(os.path.join(state, "calls.log"), "a") as f:
        f.write(("token" if token else "login") + "\\n")
    if not token and os.path.exists(os.path.join(state, "login_bad")):
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                          "result": "OAuth token has expired. Please run /login"}), flush=True)
        sys.exit(1)
    if "--json-schema" in args:
        print(json.dumps({"is_error": False, "structured_output": {"via": "token" if token else "login"}}))
        sys.exit(0)
    sid = args[args.index("--resume") + 1] if "--resume" in args else args[args.index("--session-id") + 1]
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "tools": []}), flush=True)
    reply = ("token:" if token else "login:") + msg[:20]
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": reply}), flush=True)
''' % sys.executable)


class ClaudeFallbackTokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.bin = self.dir / "claude"; self.bin.write_text(FAKE); self.bin.chmod(0o755)
        self.cwd = self.dir / "work"; self.cwd.mkdir()
        env = patch.dict(os.environ, {"CLAUDE_BIN": str(self.bin), "FAKE_STATE": str(self.dir)})
        env.start(); self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        claude_auth.configure(self.dir)
        claude_auth._status_cache = (0.0, None); claude_auth._login_failed_at = 0.0
        self.addCleanup(claude_auth.configure, None)
        mcp = patch.object(ClaudeTransport, "refresh_mcp", lambda self, cwd=None: None)
        mcp.start(); self.addCleanup(mcp.stop)
        self.set_login(True, time.time() + 30 * 86400)

    def set_login(self, logged_in, expires):
        iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(expires)) if expires else None
        (self.dir / "status.json").write_text(json.dumps({"loggedIn": logged_in, "loginExpiresAt": iso}))
        claude_auth._status_cache = (0.0, None)

    def calls(self):
        try:
            return (self.dir / "calls.log").read_text().split()
        except FileNotFoundError:
            return []

    def test_token_saved_owner_only_and_never_returned(self):
        claude_auth.save_token(TOKEN)
        mode = stat.S_IMODE(os.stat(claude_auth.token_path()).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertNotIn(TOKEN, json.dumps(claude_auth.summary()))
        self.assertTrue(claude_auth.summary()["token"]["configured"])
        with self.assertRaises(ValueError):
            claude_auth.save_token("not a token")
        claude_auth.clear_token()
        self.assertFalse(claude_auth.summary()["token"]["configured"])

    def test_valid_login_does_not_use_token(self):
        claude_auth.save_token(TOKEN)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", claude_runtime.child_env())
        self.assertFalse(claude_auth.summary()["using_token"])

    def test_expired_or_missing_login_uses_token(self):
        claude_auth.save_token(TOKEN)
        self.set_login(True, time.time() - 60)
        self.assertEqual(claude_runtime.child_env()["CLAUDE_CODE_OAUTH_TOKEN"], TOKEN)
        self.set_login(False, None)
        self.assertEqual(claude_runtime.child_env()["CLAUDE_CODE_OAUTH_TOKEN"], TOKEN)
        self.assertTrue(claude_auth.summary()["using_token"])

    def test_no_token_keeps_login_even_if_expired(self):
        self.set_login(False, None)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", claude_runtime.child_env())

    def test_auth_failure_reruns_turn_once_with_token(self):
        claude_auth.save_token(TOKEN)
        (self.dir / "login_bad").write_text("1")   # status says logged in, but the run fails auth
        t = ClaudeTransport(self.dir, claude_bin=str(self.bin)); t.mcp_servers = {"x"}; t.mcp_checked_at = time.time()
        th = t.start_thread(str(self.cwd)); t.send(th, "hello")
        end = time.time() + 20
        while time.time() < end:
            turns = t.read(th)["turns"]
            if turns and turns[-1]["status"] != "inProgress":
                break
            time.sleep(0.2)
        turn = t.read(th)["turns"][-1]
        self.assertEqual(turn["status"], "completed", turn)
        self.assertEqual(turn["items"][-1]["text"], "token:hello")
        self.assertEqual(self.calls(), ["login", "token"])
        t.close()

    def test_auth_failure_without_token_fails_normally(self):
        (self.dir / "login_bad").write_text("1")
        t = ClaudeTransport(self.dir, claude_bin=str(self.bin)); t.mcp_servers = {"x"}; t.mcp_checked_at = time.time()
        th = t.start_thread(str(self.cwd)); t.send(th, "hello")
        end = time.time() + 20
        while time.time() < end:
            turns = t.read(th)["turns"]
            if turns and turns[-1]["status"] != "inProgress":
                break
            time.sleep(0.2)
        self.assertEqual(t.read(th)["turns"][-1]["status"], "failed")
        self.assertEqual(self.calls(), ["login"])
        t.close()

    def test_structured_run_retries_with_token(self):
        claude_auth.save_token(TOKEN)
        (self.dir / "login_bad").write_text("1")
        out = claude_runtime.run_structured("x", {"type": "object"}, self.cwd, self.dir / "s.log")
        self.assertEqual(out, {"via": "token"})
        self.assertEqual(self.calls(), ["login", "token"])


if __name__ == "__main__":
    unittest.main()
