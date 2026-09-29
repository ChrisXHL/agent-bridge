#!/usr/bin/env python3
"""Read-only tools for the prompt-driven Feishu Bridge."""
import json
import os
import sys
import urllib.request

PORT = int(os.getenv("BRIDGE_PORT", "8797"))
BASE = f"http://127.0.0.1:{PORT}"
TOOLS = [
    {"name":"bridge_panel","description":"Get the local Codex Bridge task and connection panel URL.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True}},
    {"name":"bridge_list_tasks","description":"List external tasks received by Codex Bridge, including current status.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True}},
    {"name":"bridge_list_connections","description":"List Codex Bridge source-to-project connections without revealing keys.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True}},
    {"name":"bridge_feishu_setup_status","description":"Inspect the isolated 2.0 Feishu setup and identify the next setup step without exposing credentials or pairing codes.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True}},
]
def fetch(path):
    with urllib.request.urlopen(BASE+path, timeout=5) as response:
        return json.load(response)

def feishu_setup_status():
    value = fetch("/api/feishu")
    if not value.get("has_secret"):
        next_step = "在本机面板的飞书向导填写 App ID、App Secret 并选择 Codex 项目。"
    elif not value.get("bot_ready"):
        next_step = "在飞书开发者后台启用机器人、配置长连接和接收消息事件、发布应用，再在面板检查状态。"
    elif value.get("pairing_candidate"):
        next_step = "面板已收到配对消息，请核对并确认绑定的用户或群。"
    elif value.get("pairing_code"):
        next_step = "打开本机面板查看配对码，向飞书机器人发送；等待面板显示收到配对消息。"
    elif not value.get("allowed_open_ids"):
        next_step = "在本机面板生成配对码并启动接收。"
    else:
        next_step = "发送一条普通飞书测试消息，再检查 Bridge 任务状态和 Codex 读回。"
    return {
        "app_id": value.get("app_id", ""),
        "connection_id": value.get("connection_id", ""),
        "bot_name": value.get("bot_name", ""),
        "bot_ready": value.get("bot_ready", False),
        "receiver_runtime": value.get("runtime", "未启动"),
        "allowed_user_count": len(value.get("allowed_open_ids", [])),
        "allowed_chat_count": len(value.get("allowed_chat_ids", [])),
        "pairing_candidate": bool(value.get("pairing_candidate")),
        "next_step": next_step,
        "panel_url": BASE + "/panel",
    }

def respond(req_id, result=None, error=None):
    payload={"jsonrpc":"2.0","id":req_id}
    if error is not None: payload["error"]=error
    else: payload["result"]=result
    sys.stdout.write(json.dumps(payload,ensure_ascii=False)+"\n")
    sys.stdout.flush()

def main():
    for line in sys.stdin:
        try:
            request=json.loads(line)
            req_id=request.get("id")
            method=request.get("method")
            if req_id is None: continue
            if method=="initialize":
                respond(req_id,{"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"codex-bridge-v2","version":"2.4.0"}})
            elif method=="ping": respond(req_id,{})
            elif method=="tools/list": respond(req_id,{"tools":TOOLS})
            elif method=="tools/call":
                name=request.get("params",{}).get("name")
                if name=="bridge_panel": data={"url":BASE+"/panel"}
                elif name=="bridge_list_tasks": data=fetch("/api/tasks")
                elif name=="bridge_list_connections": data=fetch("/api/connections")
                elif name=="bridge_feishu_setup_status": data=feishu_setup_status()
                else: raise ValueError("Unknown tool")
                respond(req_id,{"content":[{"type":"text","text":json.dumps(data,ensure_ascii=False)}],"structuredContent":data})
            else: respond(req_id,error={"code":-32601,"message":"Method not found"})
        except Exception as exc:
            if 'req_id' in locals() and req_id is not None:
                respond(req_id,{"content":[{"type":"text","text":str(exc)}],"isError":True})
if __name__=="__main__": main()
