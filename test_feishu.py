import json
import http.client
import io
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from codex_http_bridge import BridgeError
from feishu_settings import DEFAULT_PROMPT_TEMPLATE, complete_pairing, load, render_prompt, save, start_pairing
from feishu_probe import probe
from feishu_worker import BridgeClient, FeishuAdapter, Inbox
from service import Store, make_handler


class FakeBridge:
    def __init__(self):
        self.requests = {}
        self.tasks = {}
        self.fail_next = False
        self.submissions = []

    def submit(self, message, request_id, task_id=None):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("Bridge temporarily unavailable")
        if request_id in self.requests:
            return {"task_id": self.requests[request_id], "duplicate": True}
        self.submissions.append((message, task_id))
        new_id = task_id or "task_" + str(len(self.tasks) + 1)
        self.requests[request_id] = new_id
        self.tasks[new_id] = {"status": "等待 Codex", "result": None}
        return {"task_id": new_id, "duplicate": False}

    def task(self, task_id):
        return {"task_id": task_id, **self.tasks[task_id]}


class FakeSender:
    def __init__(self):
        self.sent = []

    def reply(self, message_id, status, body):
        self.sent.append((message_id, status, body))
        return "om_reply_" + str(len(self.sent))


def event(message_id="om_first", *, sender="ou_owner", chat="oc_private",
          chat_type="p2p", text="检查项目", parent=None, mentions=None,
          message_type="text", sender_type="user"):
    return {"event": {
        "sender": {"sender_type": sender_type, "sender_id": {"open_id": sender}},
        "message": {
            "message_id": message_id, "chat_id": chat, "chat_type": chat_type,
            "message_type": message_type, "content": json.dumps({"text": text}),
            "parent_id": parent, "mentions": mentions or [],
        },
    }}


class FeishuBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = {
            "app_id": "cli_test", "allowed_open_ids": ["ou_owner"],
            "allowed_chat_ids": ["oc_team"], "bot_open_id": "ou_bot",
        }
        self.inbox = Inbox(root / "inbox.sqlite3")
        self.bridge = FakeBridge()
        self.sender = FakeSender()
        self.adapter = FeishuAdapter(self.config, self.inbox, self.bridge, self.sender)

    def tearDown(self):
        self.tmp.cleanup()

    def group_event(self, message_id, sender="ou_member", chat="oc_team", parent=None):
        return event(message_id, sender=sender, chat=chat, chat_type="group",
                     text="@_user_1 检查项目", parent=parent,
                     mentions=[{"id": {"open_id": "ou_bot"}, "key": "@_user_1"}])

    def test_group_member_without_personal_pairing_can_create_task(self):
        self.config["allowed_open_ids"] = []
        self.assertTrue(self.adapter.handle_event(self.group_event("om_member")))
        self.adapter.pump_once()
        self.assertEqual(len(self.bridge.tasks), 1)
        self.assertEqual(self.inbox.linked_source("om_member")["sender_id"], "ou_member")
        self.assertEqual(self.config["allowed_open_ids"], [])

    def test_unlisted_group_is_rejected_even_for_personally_paired_user(self):
        for sender in ("ou_member", "ou_owner"):
            self.assertFalse(self.adapter.handle_event(
                self.group_event("om_other_" + sender, sender=sender, chat="oc_other")))
        self.assertEqual(self.inbox.pending(), [])

    def test_group_member_still_needs_bot_mention(self):
        self.assertFalse(self.adapter.handle_event(event(
            "om_missing_at", sender="ou_member", chat="oc_team", chat_type="group")))
        self.assertFalse(self.adapter.handle_event(event(
            "om_wrong_at", sender="ou_member", chat="oc_team", chat_type="group",
            text="@_user_1 hi", mentions=[{"id":{"open_id":"ou_someone"},"key":"@_user_1"}])))

    def test_group_membership_does_not_grant_private_access(self):
        self.assertTrue(self.adapter.handle_event(self.group_event("om_group_access")))
        self.assertFalse(self.adapter.handle_event(event("om_dm", sender="ou_member")))
        self.assertTrue(self.adapter.handle_event(event("om_owner_dm", sender="ou_owner")))

    def test_group_task_stays_owned_by_original_sender(self):
        self.adapter.handle_event(self.group_event("om_original"))
        self.adapter.pump_once()
        tid = self.inbox.linked_source("om_original")["task_id"]
        self.bridge.tasks[tid] = {"status":"已完成", "result":"done"}
        self.adapter.pump_once()
        reply = self.inbox.linked_source("om_original")["outbound_id"]
        for parent in ("om_original", reply):
            self.assertFalse(self.adapter.handle_event(
                self.group_event("om_hijack_" + parent, sender="ou_other", parent=parent)))
        self.assertTrue(self.adapter.handle_event(self.group_event("om_own_reply", parent=reply)))
        self.adapter.pump_once()
        self.assertEqual(self.inbox.linked_source("om_own_reply")["task_id"], tid)
        self.assertTrue(self.adapter.handle_event(self.group_event("om_new_task", sender="ou_other")))
        self.adapter.pump_once()
        self.assertNotEqual(self.inbox.linked_source("om_new_task")["task_id"], tid)

    def test_same_sender_cannot_continue_task_from_different_allowed_group(self):
        self.config["allowed_chat_ids"].append("oc_other_allowed")
        self.adapter.handle_event(self.group_event("om_original"))
        self.adapter.pump_once()
        self.assertFalse(self.adapter.handle_event(self.group_event(
            "om_cross_group", chat="oc_other_allowed", parent="om_original")))

    def test_dedup_submit_and_final_reply(self):
        self.assertTrue(self.adapter.handle_event(event()))
        self.assertFalse(self.adapter.handle_event(event()))
        self.assertEqual(len(self.inbox.pending()), 1)
        self.adapter.pump_once()
        self.assertEqual(len(self.bridge.requests), 1)
        self.assertTrue(self.bridge.submissions[0][0].startswith(
            DEFAULT_PROMPT_TEMPLATE.replace("{{message}}", "检查项目") + "\n\n[Codex Bridge 来源信息"))
        self.assertIn('"source_message_id": "om_first"', self.bridge.submissions[0][0])
        task_id = self.inbox.submitted()[0]["task_id"]
        self.bridge.tasks[task_id] = {"status": "已完成", "result": "完成核验"}
        self.adapter.pump_once()
        self.adapter.pump_once()
        self.assertEqual(len(self.sender.sent), 1)
        self.assertIn("完成核验", self.sender.sent[0][2])

    def test_sender_group_mention_and_message_type_filters(self):
        self.assertFalse(self.adapter.handle_event(event(sender="ou_stranger")))
        self.assertFalse(self.adapter.handle_event(event(message_type="file")))
        self.assertFalse(self.adapter.handle_event(event(sender_type="app")))
        self.assertFalse(self.adapter.handle_event(event(
            chat_type="group", chat="oc_team", message_id="om_group")))
        self.assertFalse(self.adapter.handle_event(event(
            chat_type="group", chat="oc_other", message_id="om_group")))
        mention = [{"id": {"open_id": "ou_bot"}, "key": "@_user_1"}]
        self.assertTrue(self.adapter.handle_event(event(
            chat_type="group", chat="oc_team", message_id="om_group",
            text="@_user_1 继续检查", mentions=mention)))
        self.assertEqual(self.inbox.pending()[0]["text"], "继续检查")

    def test_reply_to_prior_message_reuses_task(self):
        self.adapter.handle_event(event())
        self.adapter.pump_once()
        original = self.inbox.linked_source('om_first')['task_id']
        self.adapter.handle_event(event('om_followup', text='补充要求', parent='om_first'))
        self.adapter.pump_once()
        self.assertEqual(len(self.bridge.submissions), 1)
        self.bridge.tasks[original] = {'status':'已完成','result':'Original turn result'}
        self.adapter.pump_once()
        followup = self.inbox.linked_source('om_followup')
        self.assertEqual(followup['task_id'], original)
        self.assertEqual(len(self.bridge.tasks), 1)
        self.assertIn('补充要求', self.bridge.submissions[1][0])
        self.assertEqual(self.bridge.submissions[1][1], original)
        self.assertEqual(len(self.sender.sent), 1)
        self.assertIn('Original turn result', self.sender.sent[0][2])

    def test_custom_prompt_is_frozen_when_message_arrives(self):
        self.config["prompt_template"] = "项目规则：先核对事实。\n请求：{{message}}"
        self.adapter.handle_event(event())
        self.config["prompt_template"] = "修改后的规则：{{message}}"
        self.adapter.pump_once()
        self.assertTrue(self.bridge.submissions[0][0].startswith(
            "项目规则：先核对事实。\n请求：检查项目\n\n[Codex Bridge 来源信息"))

    def test_prompt_respects_bridge_request_size(self):
        with self.assertRaises(BridgeError):
            render_prompt("{{message}}", "中" * 6000)
        self.assertFalse(self.adapter.handle_event(event(text="中" * 6000)))

    def test_bridge_failure_keeps_inbox_for_retry(self):
        self.adapter.handle_event(event())
        self.bridge.fail_next = True
        self.adapter.pump_once()
        self.assertEqual(len(self.inbox.pending()), 1)
        self.adapter.pump_once()
        self.assertEqual(len(self.inbox.pending()), 0)
        self.assertEqual(len(self.bridge.requests), 1)

    def test_pairing_identifies_sender_without_creating_a_task(self):
        root = Path(self.tmp.name)
        project = root / "project"
        project.mkdir()
        projects = root / "projects.json"
        projects.write_text(json.dumps({"project-1": str(project)}))
        store = Store(root / "bridge.sqlite3", projects, root / "missing.sqlite")
        connection = store.create_connection("飞书测试", "project-1")
        settings_path = root / "private" / "feishu.json"
        save(settings_path, {
            "app_id": "cli_test", "app_secret": "secret-for-test",
            "connection_id": connection["id"], "allowed_open_ids": [],
            "allowed_chat_ids": [], "enabled": False,
        }, store, internal={"bot_ready": True, "bot_open_id": "ou_bot"})
        start_pairing(settings_path, store)
        config = load(settings_path)
        adapter = FeishuAdapter(config, self.inbox, self.bridge, self.sender)
        self.assertFalse(adapter.handle_event(event("om_wrong", sender="ou_new", text="wrong-code")))
        self.assertTrue(adapter.handle_event(event("om_pair", sender="ou_new", text=config["pairing_code"])))
        self.assertEqual(self.inbox.pending(), [])
        self.assertEqual(self.bridge.submissions, [])
        candidate = self.inbox.latest_pairing(config["pairing_code"])
        self.assertEqual(candidate["sender_id"], "ou_new")
        complete_pairing(settings_path, store, candidate)
        self.assertEqual(load(settings_path)["allowed_open_ids"], ["ou_new"])
        self.assertEqual(load(settings_path)["pairing_code"], "")
        adapter = FeishuAdapter(load(settings_path), self.inbox, self.bridge, self.sender)
        self.assertTrue(adapter.handle_event(event("om_task", sender="ou_new", text="现在检查")))
        self.assertFalse(adapter.handle_event(event("om_other", sender="ou_other", text="不应进入")))
        adapter.pump_once()
        self.assertEqual(len(self.bridge.submissions), 1)

    def test_group_pairing_requires_mention_and_adds_chat(self):
        root = Path(self.tmp.name)
        project = root / "project"
        project.mkdir()
        projects = root / "projects.json"
        projects.write_text(json.dumps({"project-1": str(project)}))
        store = Store(root / "bridge.sqlite3", projects, root / "missing.sqlite")
        connection = store.create_connection("飞书测试", "project-1")
        settings_path = root / "private" / "feishu.json"
        save(settings_path, {
            "app_id": "cli_test", "app_secret": "secret-for-test",
            "connection_id": connection["id"], "allowed_open_ids": [],
            "allowed_chat_ids": [], "enabled": False,
        }, store, internal={"bot_ready": True, "bot_open_id": "ou_bot"})
        start_pairing(settings_path, store)
        config = load(settings_path)
        adapter = FeishuAdapter(config, self.inbox, self.bridge, self.sender)
        code = config["pairing_code"]
        self.assertFalse(adapter.handle_event(event("om_no_mention", chat_type="group", chat="oc_group", text=code)))
        mention = [{"id": {"open_id": "ou_bot"}, "key": "@_user_1"}]
        self.assertTrue(adapter.handle_event(event("om_group_pair", chat_type="group", chat="oc_group",
                                                   text="@_user_1 " + code, mentions=mention)))
        complete_pairing(settings_path, store, self.inbox.latest_pairing(code))
        config = load(settings_path)
        self.assertEqual(config["allowed_chat_ids"], ["oc_group"])
        self.assertEqual(config["allowed_open_ids"], ["ou_owner"])

    def test_settings_are_isolated_and_secret_is_not_returned(self):
        root = Path(self.tmp.name)
        project = root / "project"
        project.mkdir()
        projects = root / "projects.json"
        projects.write_text(json.dumps({"project-1": str(project)}))
        store = Store(root / "bridge.sqlite3", projects, root / "missing.sqlite")
        connection = store.create_connection("飞书测试", "project-1")
        settings_path = root / "private" / "feishu.json"
        payload = {
            "app_id": "cli_test", "app_secret": "secret-for-test",
            "connection_id": connection["id"],
            "allowed_open_ids": ["ou_owner"], "allowed_chat_ids": [],
            "bot_open_id": "", "enabled": False,
        }
        public = save(settings_path, payload, store)
        self.assertNotIn("app_secret", public)
        self.assertNotIn("connection_key", public)
        self.assertTrue(public["has_secret"])
        self.assertEqual(public["prompt_template"], DEFAULT_PROMPT_TEMPLATE)
        self.assertEqual(load(settings_path)["connection_key"], connection["api_key"])
        self.assertEqual(settings_path.stat().st_mode & 0o777, 0o600)
        updated = save(settings_path, {"enabled": True}, store)
        self.assertTrue(updated["enabled"])
        updated = save(settings_path, {"prompt_template": "固定指令\n{{message}}"}, store)
        self.assertEqual(updated["prompt_template"], "固定指令\n{{message}}")
        self.assertTrue(updated["enabled"])
        with self.assertRaises(BridgeError):
            save(settings_path, {"prompt_template": "漏了变量"}, store)
        with self.assertRaises(BridgeError):
            save(settings_path, {"prompt_template": "{{message}} 和 {{message}}"}, store)
        self.assertEqual(save(settings_path, {"allowed_open_ids": []}, store)["allowed_open_ids"], [])

    def test_official_probe_finds_bot_without_extra_scopes(self):
        class Reply(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        paths = []
        def opener(req, timeout):
            paths.append((req.full_url, req.get_method()))
            if req.full_url.endswith("/tenant_access_token/internal"):
                return Reply(b'{"code":0,"tenant_access_token":"test-token"}')
            return Reply(b'{"code":0,"bot":{"open_id":"ou_bot","app_name":"Test Bot","activate_status":2}}')
        checked = probe("cli_test", "secret-for-test", opener=opener)
        self.assertTrue(checked["bot_ready"])
        self.assertEqual(checked["bot_open_id"], "ou_bot")
        self.assertEqual([method for _, method in paths], ["POST", "GET"])

    def test_quick_setup_and_pairing_http_flow(self):
        root = Path(self.tmp.name)
        project = root / "project"
        project.mkdir()
        projects = root / "projects.json"
        projects.write_text(json.dumps({"project-1": str(project)}))
        store = Store(root / "bridge.sqlite3", projects, root / "missing.sqlite")
        settings_path = root / "private" / "feishu.json"
        inbox_path = root / "private" / "inbox.sqlite3"
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
            store, None, "http://127.0.0.1:0",
            feishu_settings_path=settings_path, feishu_inbox_path=inbox_path))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def post(path, data):
            client = http.client.HTTPConnection("127.0.0.1", server.server_port)
            client.request("POST", path, json.dumps(data).encode(), {"Content-Type": "application/json"})
            response = client.getresponse()
            status, result = response.status, json.load(response)
            client.close()
            return status, result
        try:
            with patch("service.probe_feishu", return_value={
                "credentials_ok": True, "bot_ready": True,
                "bot_open_id": "ou_bot", "bot_name": "Test Bot",
            }):
                status, result = post("/api/feishu/setup", {
                    "app_id": "cli_test", "app_secret": "secret-for-test", "project_id": "project-1",
                })
            self.assertEqual(status, 200)
            self.assertTrue(result["bot_ready"])
            self.assertNotIn("app_secret", result)
            self.assertEqual(len(store.connections()), 1)
            status, result = post("/api/feishu/pairing", {"action": "start"})
            self.assertEqual(status, 200)
            code = result["pairing_code"]
            receiver = FeishuAdapter(load(settings_path), Inbox(inbox_path), self.bridge, self.sender)
            self.assertTrue(receiver.handle_event(event("om_pair_http", sender="ou_new", text=code)))
            status, result = post("/api/feishu/pairing", {"action": "confirm"})
            self.assertEqual(status, 200)
            self.assertEqual(result["allowed_open_ids"], ["ou_new"])
            self.assertEqual(result["pairing_code"], "")
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_feishu_settings_http_is_local_only_and_never_returns_secret(self):
        root = Path(self.tmp.name)
        project = root / "project"
        project.mkdir()
        projects = root / "projects.json"
        projects.write_text(json.dumps({"project-1": str(project)}))
        store = Store(root / "bridge.sqlite3", projects, root / "missing.sqlite")
        connection = store.create_connection("飞书测试", "project-1")
        settings_path = root / "private" / "feishu.json"
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(store, None, "http://127.0.0.1:0", feishu_settings_path=settings_path),
        )
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            payload = json.dumps({
                "app_id": "cli_test", "app_secret": "secret-for-test",
                "connection_id": connection["id"], "allowed_open_ids": ["ou_owner"],
                "allowed_chat_ids": [], "bot_open_id": "", "enabled": False,
            }).encode()
            client = http.client.HTTPConnection("127.0.0.1", server.server_port)
            client.request("PATCH", "/api/feishu", payload, {"Content-Type": "application/json"})
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertNotIn(b"secret-for-test", response.read())
            client.close()
            client = http.client.HTTPConnection("127.0.0.1", server.server_port)
            client.request("GET", "/api/feishu")
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertNotIn(b"secret-for-test", response.read())
            client.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join()
        lan_handler = make_handler(
            store, None, "http://127.0.0.1:0", allowed_host="127.0.0.1:0",
            feishu_settings_path=settings_path)
        lan_server = ThreadingHTTPServer(("127.0.0.1", 0), lan_handler)
        lan_server.RequestHandlerClass = make_handler(
            store, None, "http://127.0.0.1:0",
            allowed_host=f"127.0.0.1:{lan_server.server_port}",
            feishu_settings_path=settings_path)
        worker = threading.Thread(target=lan_server.serve_forever, daemon=True)
        worker.start()
        try:
            client = http.client.HTTPConnection("127.0.0.1", lan_server.server_port)
            client.request("GET", "/api/feishu")
            response = client.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            client.close()
        finally:
            lan_server.shutdown()
            lan_server.server_close()
            worker.join()

    def test_worker_uses_existing_bridge_intake_contract(self):
        root = Path(self.tmp.name)
        project = root / "project"
        project.mkdir()
        projects = root / "projects.json"
        projects.write_text(json.dumps({"project-1": str(project)}))
        store = Store(root / "bridge.sqlite3", projects, root / "missing.sqlite")
        connection = store.create_connection("飞书测试", "project-1")
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(store, None, "http://127.0.0.1:0"),
        )
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = BridgeClient(
                f"http://127.0.0.1:{server.server_port}", connection["api_key"])
            first = client.submit("测试消息", "fs_test_message_001")
            duplicate = client.submit("测试消息", "fs_test_message_001")
            self.assertEqual(first["task_id"], duplicate["task_id"])
            self.assertTrue(duplicate["duplicate"])
            self.assertEqual(client.task(first["task_id"])["status"], "等待 Codex")
            config = {**self.config, "prompt_template": "固定规则\n{{message}}"}
            adapter = FeishuAdapter(config, self.inbox, client, self.sender)
            self.assertTrue(adapter.handle_event(event("om_contract", text="执行检查")))
            adapter.pump_once()
            linked = self.inbox.submitted()[0]
            self.assertTrue(store.task(linked["task_id"])["messages"][0]["content"].startswith(
                "固定规则\n执行检查\n\n[Codex Bridge 来源信息"))
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
