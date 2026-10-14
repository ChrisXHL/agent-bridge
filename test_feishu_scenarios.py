"""Ten isolated first-use and message-routing scenarios for Feishu Bridge 2.0."""
from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from codex_http_bridge import BridgeError
from feishu_settings import load, save
from feishu_worker import BridgeClient, FeishuAdapter, Inbox
from service import Store, make_handler


def event(message_id, text, *, sender="ou_owner", chat="oc_private", chat_type="p2p",
          mentions=None, parent=None):
    return {"event": {
        "sender": {"sender_type": "user", "sender_id": {"open_id": sender}},
        "message": {
            "message_id": message_id, "message_type": "text", "chat_id": chat,
            "chat_type": chat_type, "content": json.dumps({"text": text}),
            "mentions": mentions or [], "parent_id": parent,
        },
    }}


class FakeSender:
    def reply(self, message_id, status, body):
        return "om_reply_" + message_id


class FeishuScenarioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bridge-v2-scenario-")
        self.root = Path(self.tmp.name)
        project = self.root / "project"
        project.mkdir()
        projects_path = self.root / "projects.json"
        projects_path.write_text(json.dumps({"project-1": str(project)}), encoding="utf-8")
        self.store = Store(self.root / "bridge.sqlite3", projects_path,
                           self.root / "missing-codex-state.sqlite")
        self.settings_path = self.root / "feishu.json"
        self.inbox_path = self.root / "inbox.sqlite3"
        self.probe_patch = patch("service.probe_feishu")
        self.probe = self.probe_patch.start()
        self.probe.return_value = {
            "credentials_ok": True, "bot_ready": True,
            "bot_open_id": "ou_bot", "bot_name": "Test Bot",
        }
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            self.store, None, "http://127.0.0.1:0",
            feishu_settings_path=self.settings_path, feishu_inbox_path=self.inbox_path))
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()
        self.probe_patch.stop()
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        try:
            client.request(method, path, payload, headers)
            response = client.getresponse()
            return response.status, json.load(response)
        finally:
            client.close()

    def setup_app(self):
        return self.request("POST", "/api/feishu/setup", {
            "app_id": "cli_test", "app_secret": "secret-for-test", "project_id": "project-1",
        })

    def adapter(self):
        config = load(self.settings_path)
        bridge = BridgeClient(f"http://127.0.0.1:{self.server.server_port}",
                              config["connection_key"])
        return FeishuAdapter(config, Inbox(self.inbox_path), bridge, FakeSender())

    def pair_private(self):
        status, result = self.request("POST", "/api/feishu/pairing", {"action": "start"})
        self.assertEqual(status, 200)
        self.assertTrue(self.adapter().handle_event(event("om_pair", result["pairing_code"])))
        status, result = self.request("POST", "/api/feishu/pairing", {"action": "confirm"})
        self.assertEqual(status, 200)
        return result

    def test_01_valid_setup_creates_one_connection_without_exposing_secret(self):
        status, result = self.setup_app()
        self.assertEqual(status, 200)
        self.assertTrue(result["bot_ready"])
        self.assertTrue(result["has_secret"])
        self.assertNotIn("app_secret", result)
        self.assertEqual(len(self.store.connections()), 1)

    def test_02_rejected_credentials_leave_no_connection(self):
        self.probe.side_effect = BridgeError(400, "feishu_credentials_rejected", "飞书未接受应用凭据")
        status, result = self.setup_app()
        self.assertEqual((status, result["error"]["code"]),
                         (400, "feishu_credentials_rejected"))
        self.assertEqual(self.store.connections(), [])
        self.assertFalse(self.settings_path.exists())

    def test_03_repeated_setup_reuses_connection(self):
        _, first = self.setup_app()
        _, second = self.setup_app()
        self.assertEqual(first["connection_id"], second["connection_id"])
        self.assertEqual(len(self.store.connections()), 1)

    def test_04_unready_bot_cannot_start_pairing(self):
        self.probe.return_value = {"credentials_ok": True, "bot_ready": False,
                                   "bot_open_id": "", "bot_name": ""}
        status, result = self.setup_app()
        self.assertEqual(status, 200)
        self.assertFalse(result["bot_ready"])
        status, result = self.request("POST", "/api/feishu/pairing", {"action": "start"})
        self.assertEqual((status, result["error"]["code"]), (409, "bot_not_ready"))
        self.assertFalse(load(self.settings_path)["enabled"])

    def test_05_expired_pairing_code_cannot_bind_user(self):
        self.setup_app()
        _, result = self.request("POST", "/api/feishu/pairing", {"action": "start"})
        self.assertTrue(self.adapter().handle_event(event("om_pair", result["pairing_code"])))
        save(self.settings_path, {}, self.store,
             internal={"pairing_expires_at": time.time() - 1})
        status, result = self.request("POST", "/api/feishu/pairing", {"action": "confirm"})
        self.assertEqual((status, result["error"]["code"]), (409, "pairing_expired"))
        self.assertEqual(load(self.settings_path).get("allowed_open_ids", []), [])

    def test_06_valid_pairing_binds_identity_without_creating_task(self):
        self.setup_app()
        result = self.pair_private()
        self.assertEqual(result["allowed_open_ids"], ["ou_owner"])
        self.assertEqual(result["pairing_code"], "")
        self.assertEqual(self.store.tasks(), [])

    def test_07_allowed_private_message_uses_prompt_template(self):
        self.setup_app()
        self.pair_private()
        save(self.settings_path, {"prompt_template": "固定规则：{{message}}"}, self.store)
        adapter = self.adapter()
        self.assertTrue(adapter.handle_event(event("om_task", "检查项目")))
        adapter.pump_once()
        task = self.store.tasks()[0]
        self.assertEqual(task["status"], "等待 Codex")
        content=self.store.task(task["id"])["messages"][0]["content"]
        self.assertTrue(content.startswith("固定规则：检查项目\n\n[Codex Bridge 来源信息"))
        self.assertIn('"source_message_id": "om_task"', content)
        self.assertNotIn("bridge_send_card",content)

    def test_08_unknown_private_sender_is_ignored(self):
        self.setup_app()
        self.pair_private()
        adapter = self.adapter()
        self.assertFalse(adapter.handle_event(event("om_unknown", "执行任务", sender="ou_unknown")))
        adapter.pump_once()
        self.assertEqual(self.store.tasks(), [])

    def test_09_group_requires_bot_mention_and_allowed_chat(self):
        self.setup_app()
        _, result = self.request("POST", "/api/feishu/pairing", {"action": "start"})
        mention = [{"id": {"open_id": "ou_bot"}, "key": "@_user_1"}]
        adapter = self.adapter()
        self.assertFalse(adapter.handle_event(event("om_no_mention", result["pairing_code"],
                                                    chat="oc_group", chat_type="group")))
        self.assertTrue(adapter.handle_event(event("om_group_pair", "@_user_1 " + result["pairing_code"],
                                                   chat="oc_group", chat_type="group", mentions=mention)))
        status, result = self.request("POST", "/api/feishu/pairing", {"action": "confirm"})
        self.assertEqual(status, 200)
        self.assertEqual(result["allowed_chat_ids"], ["oc_group"])
        adapter = self.adapter()
        self.assertFalse(adapter.handle_event(event("om_plain", "群里检查", chat="oc_group",
                                                    chat_type="group")))
        self.assertFalse(adapter.handle_event(event("om_other_group", "@_user_1 群里检查",
                                                    chat="oc_other", chat_type="group", mentions=mention)))
        self.assertTrue(adapter.handle_event(event("om_mentioned", "@_user_1 群里检查",
                                                   chat="oc_group", chat_type="group", mentions=mention)))
        adapter.pump_once()
        self.assertEqual(len(self.store.tasks()), 1)

    def test_10_duplicate_is_ignored_and_reply_continues_same_task(self):
        self.setup_app()
        self.pair_private()
        adapter = self.adapter()
        first = event("om_first", "检查项目")
        self.assertTrue(adapter.handle_event(first))
        self.assertFalse(adapter.handle_event(first))
        adapter.pump_once()
        original = self.store.tasks()[0]["id"]
        self.store.set_task(original, "已完成", "已检查")
        self.assertTrue(adapter.handle_event(event("om_followup", "补充检查日志", parent="om_first")))
        adapter.pump_once()
        self.assertEqual(len(self.store.tasks()), 1)
        self.assertEqual(self.store.tasks()[0]["id"], original)
        self.assertEqual(len(self.store.task(original)["messages"]), 2)


if __name__ == "__main__":
    unittest.main()
