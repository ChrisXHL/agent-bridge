"""The explicit document-review handoff used by the Example voice CardKit template."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

TEMPLATE_ID = "EXAMPLE_TEMPLATE_ID"
TEMPLATE_VERSION = "1.2.0"
MARKER = "FEISHU_REVIEW_DOC_URL:"
EXAMPLE_VOICE_MATERIAL = "https://example.feishu.cn/file/EXAMPLE_VOICE_FILE"
VOICE_MATERIALS = {"example_voice": EXAMPLE_VOICE_MATERIAL}
_LINE = re.compile(r"^FEISHU_REVIEW_DOC_URL:\s*(\S+)\s*$", re.MULTILINE)
_FEISHU_HOST = re.compile(r"(?:^|\.)feishu\.cn$", re.IGNORECASE)


def valid_doc_url(url):
    if not isinstance(url, str) or len(url) > 2000:
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (parsed.scheme == "https" and bool(parsed.hostname)
            and bool(_FEISHU_HOST.search(parsed.hostname))
            and not parsed.username and not parsed.password
            and parsed.path.startswith(("/docx/", "/docs/", "/wiki/", "/file/")))


def review_doc_url(result):
    """Only an intentional standalone marker can trigger a review card."""
    matches = _LINE.findall(result or "")
    if len(matches) != 1 or not valid_doc_url(matches[0]):
        return None
    return matches[0]


def template_content(doc_url):
    if not valid_doc_url(doc_url):
        raise ValueError("Expected an HTTPS Feishu document URL")
    return json.dumps({
        "type": "template",
        "data": {
            "template_id": TEMPLATE_ID,
            "template_version_name": TEMPLATE_VERSION,
            "template_variable": {"review_doc_link": {"url": doc_url}},
        },
    }, ensure_ascii=False)


def selected_voice(form_value):
    """Treat the card form as untrusted; accept only the published Example voice pair."""
    if isinstance(form_value, str):
        try:
            form_value = json.loads(form_value)
        except ValueError:
            return None
    if not isinstance(form_value, dict):
        return None
    choice = form_value.get("voice")
    if isinstance(choice, str):
        try:
            choice = json.loads(choice)
        except ValueError:
            return None
    if not isinstance(choice, dict) or set(choice) != {"voice", "material_url"}:
        return None
    voice = choice.get("voice")
    return choice if VOICE_MATERIALS.get(voice) == choice.get("material_url") else None


def approval_prompt(doc_url, voice):
    return (
        "飞书文档已由授权审核人确认通过，请继续原任务。\n"
        f"审核文档：{doc_url}\n"
        f"选定音色：{voice['voice']}（示例音色）\n"
        f"音色素材链接：{voice['material_url']}\n"
        "请以这份已审核文档和上述音色素材为输入执行后续处理，并在回复中说明结果。"
    )
