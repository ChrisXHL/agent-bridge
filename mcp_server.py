#!/usr/bin/env python3
"""Read-only tools for the prompt-driven Feishu Bridge."""
import json
import os
import sys
import urllib.request

PORT = int(os.getenv("BRIDGE_PORT", "8797"))
BASE = f"http://127.0.0.1:{PORT}"
TOOLS = [
    {"name":"bridge_environment_status","description":"Diagnose a fresh Bridge installation before dependencies or the service exist. Returns missing requirements and the next action for the agent. Does not install, log in, or create bots.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True}},
    {"name":"bridge_create_assistant_manager","description":"Create or resume the user's Feishu assistant manager after an explicit creation/setup request. Returns official QR progress; the user must complete QR authorization. Does not recreate an existing manager.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":False,"destructiveHint":False,"idempotentHint":True}},
    {"name":"bridge_panel","description":"Get the local Agent Bridge task and connection panel URL.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True}},
    {"name":"bridge_list_tasks","description":"List external tasks received by Agent Bridge, including current status.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True}},
    {"name":"bridge_list_connections","description":"List Agent Bridge source-to-project connections without revealing keys.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True}},
    {"name":"bridge_feishu_setup_status","description":"Inspect the isolated 2.0 Feishu setup and identify the next setup step without exposing credentials or pairing codes.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True}},
]
def fetch(path):
    with urllib.request.urlopen(BASE+path, timeout=5) as response:
        return json.load(response)

def feishu_setup_status():
    value = fetch("/api/feishu")
    try:
        jobs=fetch('/api/feishu/onboarding').get('assistants',[])
    except (OSError,ValueError):jobs=[]
    manager=next((j for j in jobs if j.get('kind')=='manager'),None)
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
    if manager:
        if manager['phase']=='ready':next_step='打开面板顶部的“打开助手管理员”，在飞书私聊描述想创建的助手用途。'
        elif manager['phase']=='removed':next_step='在面板顶部点击“恢复助手管理员”，继续使用原来的机器人。'
        elif manager['phase']=='needs_check':next_step='在面板顶部点击“查看恢复方式”，先核对飞书中的创建结果。'
        elif manager['phase']=='failed':next_step='在面板顶部点击“继续配置”，沿用原创建记录。'
        else:next_step='打开面板的飞书页，继续完成助手管理员的扫码和自动配置。'
    elif not value.get('has_secret'):
        next_step='在面板的飞书页顶部点击“创建助手管理员”，用自己的飞书扫码确认；连接和管理规则会自动配置。'
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
        "assistant_manager": {"status":manager['phase'],"name":manager.get('name','助手管理员')} if manager else {"status":"not_created"},
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
                respond(req_id,{"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"codex-bridge-v2","version":"2.6.0"}})
            elif method=="ping": respond(req_id,{})
            elif method=="tools/list": respond(req_id,{"tools":TOOLS})
            elif method=="tools/call":
                name=request.get("params",{}).get("name")
                if name=='bridge_environment_status':
                    from bridge_setup import doctor
                    data=doctor(port=PORT)
                elif name=='bridge_create_assistant_manager':
                    from bridge_setup import manager,doctor
                    try:data=manager(PORT)
                    except (OSError,ValueError):data=doctor(port=PORT)
                elif name=="bridge_panel": data={"url":BASE+"/panel"}
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
