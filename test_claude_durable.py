"""Local patch tests: Claude turns survive a Bridge restart and continue after a lost process."""
import os
import signal
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import claude_runtime
from claude_runtime import ClaudeTransport

FAKE = textwrap.dedent('''\
    #!%s
    import json, sys, time, os
    args = sys.argv[1:]
    msg = sys.stdin.read()
    sid = args[args.index("--resume") + 1] if "--resume" in args else args[args.index("--session-id") + 1]
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid, "tools": []}), flush=True)
    time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
    reply = ("continued:" if "系统说明" in msg else "done:") + msg[:20]
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": reply}), flush=True)
''' % sys.executable)


class DurableClaudeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.bin = self.dir / "claude"
        self.bin.write_text(FAKE); self.bin.chmod(0o755)
        self.cwd = self.dir / "work"; self.cwd.mkdir()
        patcher = patch.object(ClaudeTransport, "refresh_mcp", lambda self, cwd=None: None)
        patcher.start(); self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def transport(self):
        t = ClaudeTransport(self.dir, claude_bin=str(self.bin))
        t.mcp_servers = {"x"}; t.mcp_checked_at = time.time()
        return t

    def wait(self, t, thread, status="completed", timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            turns = t.read(thread)["turns"]
            if turns and turns[-1]["status"] == status:
                return turns[-1]
            time.sleep(0.2)
        self.fail(f"turn not {status}: {t.read(thread)['turns']}")

    def test_normal_turn_completes(self):
        t = self.transport(); th = t.start_thread(str(self.cwd))
        t.send(th, "hello")
        turn = self.wait(t, th)
        self.assertEqual(turn["items"][-1]["text"], "done:hello")

    def test_restart_reattaches_to_running_turn(self):
        os.environ["FAKE_SLEEP"] = "3"; self.addCleanup(os.environ.pop, "FAKE_SLEEP", None)
        old = self.transport(); th = old.start_thread(str(self.cwd))
        old.send(th, "long job"); time.sleep(1)
        old.close()  # Bridge stops: the turn must keep running.
        new = self.transport()  # New Bridge process reattaches.
        turn = self.wait(new, th)
        self.assertEqual(turn["items"][-1]["text"], "done:long job")
        with new._db() as db:
            self.assertEqual(db.execute("SELECT resumes FROM turns").fetchone()[0], 0)

    def test_lost_process_continues_same_session(self):
        os.environ["FAKE_SLEEP"] = "30"; self.addCleanup(os.environ.pop, "FAKE_SLEEP", None)
        old = self.transport()
        with patch.object(ClaudeTransport, "_watch", lambda *a, **k: None):  # Bridge died right after launch.
            th = old.start_thread(str(self.cwd)); old.send(th, "lost job"); time.sleep(1.5)
        with old._db() as db:
            pid = db.execute("SELECT pid FROM turns").fetchone()[0]
        os.killpg(pid, signal.SIGKILL); time.sleep(0.5)
        os.environ["FAKE_SLEEP"] = "0"
        new = self.transport()
        turn = self.wait(new, th)
        self.assertTrue(turn["items"][-1]["text"].startswith("continued:"))
        with new._db() as db:
            self.assertEqual(db.execute("SELECT resumes FROM turns").fetchone()[0], 1)

    def test_manual_interrupt_after_restart(self):
        os.environ["FAKE_SLEEP"] = "30"; self.addCleanup(os.environ.pop, "FAKE_SLEEP", None)
        old = self.transport(); th = old.start_thread(str(self.cwd))
        old.send(th, "stop me"); time.sleep(1); old.close()
        new = self.transport(); time.sleep(0.5)
        self.assertTrue(new.interrupt(th))
        self.wait(new, th, status="interrupted")


if __name__ == "__main__":
    unittest.main()
