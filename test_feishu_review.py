"""Review-card handoff checks without calling Feishu or Codex."""
import json
import tempfile
import unittest
from pathlib import Path

from feishu_review import (EXAMPLE_VOICE_MATERIAL, approval_prompt, review_doc_url,
                           selected_voice, template_content)
from feishu_worker import FeishuAdapter, Inbox

DOC = "https://example.feishu.cn/docx/EXAMPLE_RESOURCE"
VOICE = {"voice": "example_voice", "material_url": EXAMPLE_VOICE_MATERIAL}


class Bridge:
    def __init__(self):
        self.status = "等待 Codex"
        self.result = None
        self.submissions = []

    def submit(self, prompt, request_id, task_id=None):
        if not any(x[1] == request_id for x in self.submissions):
            self.submissions.append((prompt, request_id, task_id))
        self.status = "等待 Codex"
        return {"task_id": task_id or "task_one"}

    def task(self, task_id):
        return {"task_id": task_id, "status": self.status, "result": self.result}


class Sender:
    def __init__(self):
        self.cards = []
        self.texts = []

    def reply_review_card(self, message_id, doc_url):
        self.cards.append((message_id, doc_url))
        return "om_card"

    def reply(self, message_id, status, body):
        self.texts.append((message_id, status, body))
        return "om_text"


def action(*, user="ou_owner", card="om_card", chat="oc_private", voice=VOICE):
    return {"event": {
        "operator": {"open_id": user},
        "context": {"open_message_id": card, "open_chat_id": chat},
        "action": {"tag": "button", "name": "approve_voice_material",
                   "form_value": {"voice": json.dumps(voice)}},
    }}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Inbox(Path(self.tmp.name) / "inbox.sqlite3")
        self.bridge = Bridge()
        self.sender = Sender()
        self.adapter = FeishuAdapter(
            {"app_id": "cli_test", "allowed_open_ids": ["ou_owner"]},
            self.inbox, self.bridge, self.sender)
        self.inbox.accept({"message_id": "om_source", "chat_id": "oc_private",
                           "sender_id": "ou_owner", "text": "生成文档", "prompt": "生成文档"})
        self.adapter.pump_once()

    def tearDown(self):
        self.tmp.cleanup()

    def test_marker_and_template(self):
        self.assertEqual(review_doc_url("Done\nFEISHU_REVIEW_DOC_URL: " + DOC), DOC)
        self.assertIsNone(review_doc_url("URL: " + DOC))
        self.assertIsNone(review_doc_url("FEISHU_REVIEW_DOC_URL: https://evil.example/docx/a"))
        self.assertIsNone(review_doc_url("FEISHU_REVIEW_DOC_URL: https://feishu.cn.evil.example/docx/a"))
        self.assertEqual(json.loads(template_content(DOC))["data"]["template_variable"],
                         {"review_doc_link": {"url": DOC}})

    def test_approval_continues_original_task_once(self):
        self.bridge.status = "已完成"
        self.bridge.result = "待审阅\nFEISHU_REVIEW_DOC_URL: " + DOC
        self.adapter.pump_once()
        self.adapter.pump_once()
        self.assertEqual(self.sender.cards, [("om_source", DOC)])
        self.assertEqual(self.adapter.handle_card_action(action(user="ou_other")), "ignored")
        self.assertEqual(self.adapter.handle_card_action(action(chat="oc_other")), "not_found")
        self.assertEqual(self.adapter.handle_card_action(action(voice={"voice": "example_voice", "material_url": "https://evil.example"})), "invalid_voice")
        self.assertEqual(self.adapter.handle_card_action(action()), "accepted")
        self.assertEqual(self.adapter.handle_card_action(action()), "already_submitted")
        self.adapter.pump_once()
        self.adapter.pump_once()
        self.assertEqual(len(self.bridge.submissions), 2)
        self.assertEqual(self.bridge.submissions[1][2], "task_one")
        self.assertEqual(self.bridge.submissions[1][0], approval_prompt(DOC, VOICE))
        self.bridge.status = "已完成"
        self.bridge.result = "后续处理完成"
        self.adapter.pump_once()
        self.assertEqual(len(self.sender.cards), 1)
        self.assertEqual(len(self.sender.texts), 1)
        self.assertIn("后续处理完成", self.sender.texts[0][2])

    def test_voice_payload_must_match_published_material(self):
        self.assertEqual(selected_voice({"voice": json.dumps(VOICE)}), VOICE)
        self.assertIsNone(selected_voice({"voice": json.dumps({"voice": "yiyi", "material_url": EXAMPLE_VOICE_MATERIAL})}))
        self.assertIsNone(selected_voice({"voice": "not-json"}))

    def test_legacy_pending_card_can_continue_with_stage_two(self):
        self.bridge.status='已完成';self.bridge.result='FEISHU_REVIEW_DOC_URL: '+DOC
        self.adapter.pump_once()
        self.adapter.config['review_prompt_template']='读取文档评论并继续配音。\n{{message}}'
        self.assertEqual(self.adapter.handle_card_action(action()),'accepted')
        self.adapter.pump_once()
        prompt,_,task=self.bridge.submissions[-1]
        self.assertEqual(task,'task_one');self.assertIn('读取文档评论',prompt)
        self.assertIn('om_source',prompt);self.assertIn(DOC,prompt)


if __name__ == "__main__":
    unittest.main()
