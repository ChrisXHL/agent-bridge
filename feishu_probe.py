"""Read-only checks against the official Feishu credential and bot APIs."""
from __future__ import annotations

import json
from urllib import error, request

from codex_http_bridge import BridgeError

BASE = "https://open.feishu.cn/open-apis"


def _call(path, *, body=None, token=None, opener=request.urlopen):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers=headers,
        method="POST" if body is not None else "GET",
    )
    try:
        with opener(req, timeout=7) as response:
            result = json.load(response)
    except error.HTTPError as exc:
        try:
            result = json.load(exc)
        except (ValueError, OSError):
            result = {"code": exc.code}
    except (error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise BridgeError(503, "feishu_unreachable", "无法连接飞书开放平台，请检查网络后重试") from exc
    if not isinstance(result, dict):
        raise BridgeError(502, "feishu_invalid_response", "飞书开放平台返回了无效响应")
    return result


def probe(app_id, app_secret, *, opener=request.urlopen):
    if not isinstance(app_id, str) or not app_id.startswith("cli_") or not isinstance(app_secret, str) or len(app_secret) < 8:
        raise BridgeError(400, "invalid_feishu_credentials", "请填写有效的飞书 App ID 和 App Secret")
    auth = _call("/auth/v3/tenant_access_token/internal",
                 body={"app_id": app_id, "app_secret": app_secret}, opener=opener)
    token = auth.get("tenant_access_token")
    if auth.get("code") != 0 or not isinstance(token, str) or not token:
        raise BridgeError(400, "feishu_credentials_rejected",
                          "飞书未接受应用凭据，请检查 App ID 和 App Secret")
    bot = _call("/bot/v3/info", token=token, opener=opener)
    info = bot.get("bot") if bot.get("code") == 0 else None
    if not isinstance(info, dict):
        return {"credentials_ok": True, "bot_ready": False,
                "next_step": "在飞书开发者后台启用机器人、配置长连接和接收消息事件，然后发布应用。"}
    return {"credentials_ok": True, "bot_ready": info.get("activate_status") == 2,
            "bot_open_id": info.get("open_id", ""), "bot_name": info.get("app_name", ""),
            "next_step": "" if info.get("activate_status") == 2 else "请发布并启用飞书应用。"}
