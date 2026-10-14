"""Local-only configuration for the optional Feishu receiver."""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
import threading
from pathlib import Path

from codex_http_bridge import BridgeError

FIELDS = {
    "app_id", "app_secret", "connection_id", "allowed_open_ids",
    "allowed_chat_ids", "bot_open_id", "enabled", "prompt_template",
}
MESSAGE_VARIABLE = "{{message}}"
CONFIG_LOCK = threading.RLock()
DEFAULT_PROMPT_TEMPLATE = (
    "飞书任务：{{message}}\n\n"
    "请按上面的飞书消息执行，并在回复中清楚说明结果。"
)


def validate_prompt_template(template):
    if (not isinstance(template, str) or not template.strip()
            or len(template) > 8000 or template.count(MESSAGE_VARIABLE) != 1):
        raise BridgeError(
            400, "invalid_prompt_template",
            "Prompt 模板需为 1 到 8000 字，并且恰好包含一次 {{message}}",
        )
    return template


def render_prompt(template, message, source=None):
    result = validate_prompt_template(template).replace(MESSAGE_VARIABLE, message.strip())
    if source:
        fields = ('source_message_id', 'chat_id', 'app_id', 'parent_id', 'root_id')
        context = {key: source.get(key) for key in fields if source.get(key)}
        result += ('\n\n[Codex Bridge 来源信息；由接收器提供，供附件交付定位]\n'
                   + json.dumps(context, ensure_ascii=False, sort_keys=True))
    # Match the Bridge intake body's 16 KiB limit, including JSON escaping and a follow-up task ID.
    body = json.dumps({
        "message": result, "request_id": "fs_" + "x" * 48, "task_id": "x" * 100,
    }, ensure_ascii=False).encode("utf-8")
    if not 1 <= len(result.strip()) <= 12000 or len(body) > 16 * 1024:
        raise BridgeError(400, "invalid_message", "模板与飞书消息合成后超过 Bridge 长度限制")
    return result


def load(path):
    path = Path(path)
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Feishu configuration must be a JSON object")
    return value


def public(value):
    return {
        "app_id": value.get("app_id", ""),
        "connection_id": value.get("connection_id", ""),
        "allowed_open_ids": value.get("allowed_open_ids", []),
        "allowed_chat_ids": value.get("allowed_chat_ids", []),
        "bot_open_id": value.get("bot_open_id", ""),
        "enabled": value.get("enabled", False),
        "has_secret": bool(value.get("app_secret")),
        "prompt_template": value.get("prompt_template", DEFAULT_PROMPT_TEMPLATE),
        "pairing_code": value.get("pairing_code", ""),
        "pairing_expires_at": value.get("pairing_expires_at", 0),
        "bot_name": value.get("bot_name", ""),
        "bot_ready": value.get("bot_ready", False),

    }


def save(path, payload, store, *, internal=None, validate_only=False):
    with CONFIG_LOCK:
        return _save(path, payload, store, internal=internal, validate_only=validate_only)


def _save(path, payload, store, *, internal=None, validate_only=False):
    if not isinstance(payload, dict) or set(payload) - FIELDS:
        raise BridgeError(400, "invalid_feishu_settings", "飞书设置包含无效字段")
    current = load(path)
    value = {**current, **payload, **(internal or {})}
    if current.get("app_id") and "app_id" in payload and payload["app_id"] != current["app_id"] and not internal:
        value.update({
            "allowed_open_ids": [], "allowed_chat_ids": [], "bot_open_id": "",
            "bot_name": "", "bot_ready": False, "enabled": False,
            "pairing_code": "", "pairing_expires_at": 0, "group_configs": {},
        })
    value["prompt_template"] = validate_prompt_template(
        value.get("prompt_template", DEFAULT_PROMPT_TEMPLATE))
    if not payload.get("app_secret"):
        value["app_secret"] = current.get("app_secret", "")
    if not isinstance(value.get("enabled", False), bool):
        raise BridgeError(400, "invalid_feishu_settings", "启用状态无效")
    if payload.get("enabled") is False:
        value["pairing_code"] = ""
        value["pairing_expires_at"] = 0
    app_id = value.get("app_id", "")
    if app_id and (not isinstance(app_id, str) or not app_id.startswith("cli_")):
        raise BridgeError(400, "invalid_feishu_settings", "App ID 应以 cli_ 开头")
    secret = value.get("app_secret", "")
    if secret and (not isinstance(secret, str) or len(secret) < 8):
        raise BridgeError(400, "invalid_feishu_settings", "App Secret 无效")
    for field, prefix in (("allowed_open_ids", "ou_"), ("allowed_chat_ids", "oc_")):
        items = value.get(field, [])
        if not isinstance(items, list) or len(items) > 100 or any(
            not isinstance(item, str) or not item.startswith(prefix) or len(item) > 128
            for item in items
        ):
            raise BridgeError(400, "invalid_feishu_settings", field + " 无效")
        value[field] = list(dict.fromkeys(items))
    bot_id = value.get("bot_open_id", "")
    if bot_id and (not isinstance(bot_id, str) or not bot_id.startswith("ou_")):
        raise BridgeError(400, "invalid_feishu_settings", "机器人 Open ID 无效")
    cid = value.get("connection_id", "")
    if cid:
        connection = store.connection(cid, reveal=True)
        value["connection_key"] = connection["api_key"]
        if value["enabled"] and connection["status"] == "已停用":
            raise BridgeError(400, "connection_disabled", "选中的 Bridge 连接已停用")
    if value["enabled"] and not all((app_id, secret, cid)):
        raise BridgeError(400, "incomplete_feishu_settings", "启用前请填写应用凭据和连接")
    if value["enabled"] and value["allowed_chat_ids"] and not bot_id:
        raise BridgeError(400, "incomplete_feishu_settings", "启用群聊前请填写机器人 Open ID")
    if validate_only:
        return public(value)
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".feishu-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return public(value)


def start_pairing(path, store):
    current = load(path)
    if not all((current.get("app_id"), current.get("app_secret"), current.get("connection_id"))):
        raise BridgeError(400, "incomplete_feishu_settings", "先连接飞书应用并选择 Codex 项目")
    if not current.get("bot_ready") or not current.get("bot_open_id"):
        raise BridgeError(409, "bot_not_ready", "请先在飞书后台启用并发布机器人，再点击检查状态")
    return save(path, {"enabled": True}, store, internal={
        "pairing_code": "CB-" + secrets.token_hex(5).upper(),
        "pairing_expires_at": time.time() + 600,
    })


def complete_pairing(path, store, candidate):
    current = load(path)
    if (not current.get("pairing_code") or time.time() >= current.get("pairing_expires_at", 0)
            or candidate.get("code") != current["pairing_code"]):
        raise BridgeError(409, "pairing_expired", "配对码已过期，请重新生成")
    sender = candidate["sender_id"]
    users = list(dict.fromkeys([*current.get("allowed_open_ids", []), sender]))
    chats = current.get("allowed_chat_ids", [])
    if candidate["chat_type"] == "group":
        chats = list(dict.fromkeys([*chats, candidate["chat_id"]]))
    return save(path, {"allowed_open_ids": users, "allowed_chat_ids": chats}, store,
                internal={"pairing_code": "", "pairing_expires_at": 0})
