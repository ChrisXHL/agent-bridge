import http.client
import importlib.machinery
import importlib.util
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from platform_channel import handle_api, load_platform_classes, load_platforms
from service import Store, make_handler

ROOT = Path(__file__).resolve().parent
DEMO = '''
from platform_channel import PlatformChannel

class Channel(PlatformChannel):
    ID = "demo"
    LABEL = "Demo"
    BIND_MODE = "form"
    BIND_FIELDS = ({"key": "token", "label": "Token", "secret": True},)

    def bind_form(self, fields):
        if fields["token"] != "good":
            raise ValueError("Token 无效")
        return {"bot_id": "demo-bot", "name": "Demo 小助手", "secrets": {"token": fields["token"]}}

    def receive(self, account):
        self.stop.wait(0.2)
        return [], {}

    def send_text(self, account, target, text):
        return True
'''


def load_verifier():
    path = str(ROOT / "scripts" / "verify-platform")
    loader = importlib.machinery.SourceFileLoader("verify_platform", path)
    spec = importlib.util.spec_from_loader("verify_platform", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class PlatformChannelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "plugins").mkdir()
        (self.dir / "plugins" / "demo.py").write_text(DEMO, encoding="utf-8")
        (self.dir / "plugins" / "_skip.py").write_text("raise SystemExit('must not load')", encoding="utf-8")
        (self.dir / "plugins" / "weixin.py").write_text(DEMO.replace('"demo"', '"weixin"'), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_loader_skips_private_and_reserved(self):
        classes = load_platform_classes([self.dir / "plugins"])
        self.assertEqual(list(classes), ["demo"])

    def test_simulated_flow_passes_rules(self):
        verifier = load_verifier()
        cls = load_platform_classes([self.dir / "plugins"])["demo"]
        verifier.simulate(cls)
        self.assertEqual(verifier.FAIL, [])

    def test_loose_allow_list_is_caught(self):
        verifier = load_verifier()
        cls = load_platform_classes([self.dir / "plugins"])["demo"]
        loose = type("Loose", (cls,), {"is_allowed": lambda self, a, m: True})
        verifier.simulate(loose)
        self.assertTrue(any("陌生人" in f for f in verifier.FAIL))

    def test_rule_changes_use_context_for_bound_owner(self):
        cls = load_platform_classes([self.dir / "plugins"])["demo"]
        channel = cls(self.dir / "state", "http://127.0.0.1:1", None)
        account = {"id": "acct", "owner_user_id": "owner", "connection_key": "fixture-key",
                   "enabled": True, "prompt": "{{message}}"}
        channel._write({"accounts": [account]})
        self.assertTrue(channel.accept(account, {"sender_id": "owner", "message_id": "follow",
                                                  "text": "把它去掉"}))
        with channel.db() as db:
            row = db.execute("SELECT * FROM inbox").fetchone()
        self.assertIn("同一会话的上下文", channel._rule_instruction(account, row))
        data = {"source_id": row["message_id"], "token": channel._rule_token(account, row["message_id"]),
                "rule": "已移除该信源。{{message}}"}
        self.assertEqual(channel.self_update(data)["prompt"], data["rule"])
        channel.update_account("acct", owner_user_id="new-owner")
        with self.assertRaises(ValueError):
            channel.self_update(data)

    def test_template_is_complete_python(self):
        source = (ROOT / "platforms" / "_template.py").read_text(encoding="utf-8")
        compile(source.replace("__ID__", "x").replace("__LABEL__", "X").replace("__MODE__", "form"), "t", "exec")

    def test_http_routes(self):
        (self.dir / "work").mkdir()
        store = Store(str(self.dir / "bridge.sqlite3"), str(self.dir / "projects.json"))
        import os
        os.environ["BRIDGE_WORKSPACE_ROOT"] = str(self.dir / "work")
        channels = load_platforms(self.dir / "state", "http://127.0.0.1:1", store, [self.dir / "plugins"])
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(store, None, "http://127.0.0.1:1",
                                                                    platforms=channels))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        host = "127.0.0.1:%d" % server.server_port

        def call(method, path, body=None):
            conn = http.client.HTTPConnection(host, timeout=5)
            conn.request(method, path, json.dumps(body) if body is not None else None,
                         {"Content-Type": "application/json", "Host": host})
            r = conn.getresponse()
            return r.status, json.loads(r.read() or b"{}")

        try:
            status, data = call("GET", "/api/platforms")
            self.assertEqual([p["id"] for p in data["platforms"]], ["demo"])
            self.assertEqual(call("POST", "/api/platforms/demo/bind", {"token": "bad"})[0], 400)
            status, data = call("POST", "/api/platforms/demo/bind", {"token": "good"})
            self.assertEqual(status, 200)
            self.assertNotIn("good", json.dumps(data["accounts"]))
            aid = data["accounts"][0]["id"]
            self.assertEqual(data["accounts"][0]["name"], "Demo 小助手")
            status, data = call("PATCH", "/api/platforms/demo/accounts/" + aid, {"prompt": "没有占位符"})
            self.assertEqual(status, 400)
            status, data = call("PATCH", "/api/platforms/demo/accounts/" + aid, {"name": "改名", "prompt": "做：{{message}}"})
            self.assertEqual(data["accounts"][0]["name"], "改名")
            call("POST", "/api/platforms/demo/bind", {"token": "good"})       # re-bind keeps the custom name
            self.assertEqual(call("GET", "/api/platforms/demo")[1]["accounts"][0]["name"], "改名")
            self.assertEqual(call("GET", "/api/platforms/nope")[0], 404)
            self.assertEqual(call("POST", "/api/platforms/demo/accounts/" + aid + "/delete", {})[1]["accounts"], [])
        finally:
            server.shutdown()
            for c in channels.values():
                c.close()

    def test_handle_api_without_platforms(self):
        self.assertEqual(handle_api(None, "GET", "/api/platforms", dict)[1], {"platforms": []})
        self.assertIsNone(handle_api({}, "GET", "/api/platforms/demo", dict))


if __name__ == "__main__":
    unittest.main()
