#!/usr/bin/env python3
"""Local Bridge tools, including explicitly requested interactive cards."""
import json
import os
import sys
import urllib.request

PORT = int(os.getenv("BRIDGE_PORT", "8797"))
BASE = f"http://127.0.0.1:{PORT}"
TOOLS = [
    {"name":"bridge_get_workflow_run","description":"Read a durable workflow run with current stage, immutable inputs, review decision, history and external-operation receipts. Omit run_id to list runs.","inputSchema":{"type":"object","properties":{"run_id":{"type":"string"}},"additionalProperties":False},"annotations":{"readOnlyHint":True}},
    {"name":"bridge_workflow_run","description":"Execute only the current agent stage. complete verifies declared outputs before advancing. fail records a blocker; use_continuous_audio explicitly opts this run into unaligned normal-speed synthesis only at user request, preserving approved text and receipts; request_revision routes a blocked measured TIME_CONFLICT directly to the visited review stage's revise agent, with human confirmation only after revision; upgrade_audio_grouping explicitly upgrades a blocked alignment/TTS run after user authorization; retry resumes a blocked stage; cancel stops future stages. effect_begin claims an external operation, effect_finish records a real receipt. Never use these tools to simulate human approval. Read the run and use its current revision. See codex-bridge-workflows Skill for exact contract.","inputSchema":{"type":"object","properties":{"run_id":{"type":"string"},"revision":{"type":"integer"},"action":{"enum":["complete","fail","retry","cancel","reopen_review","request_revision","use_continuous_audio","upgrade_audio_grouping","effect_begin","effect_finish"]},"stage_id":{"type":"string"},"outputs":{"type":"object"},"reason":{"type":"string"},"key":{"type":"string"},"inputs":{"type":"array","items":{"type":"string"}},"parameters":{"type":"object"},"effect_id":{"type":"string"},"state":{"enum":["succeeded","uncertain"]},"result":{"type":"object"}},"required":["run_id","revision","action"],"additionalProperties":False},"annotations":{"readOnlyHint":False}},
    {"name":"bridge_workflow_context","description":"Read paired groups, project names, existing cards, bound voice/material choices, workflow instructions and revision for conversational configuration. No credentials. Match the user's names against this context before changing anything.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True}},
    {"name":"bridge_configure_workflow","description":"Configure a paired group's workflow from user intent: resolve names, preserve omitted settings, reuse a card and optionally change its button continuation only for this group. Call with dry_run=true to validate, inspect the result, then dry_run=false with the same revision to save when the user's request authorizes it. No additional approval needed for an already requested configuration. Does not send messages or publish CardKit templates.","inputSchema":{"type":"object","properties":{"revision":{"type":"string"},"group":{"type":"string","description":"Exact known group name or ID"},"name":{"type":"string","description":"Only when naming/renaming the group workflow"},"project":{"type":"string","description":"Optional exact project name or connection ID; omit to preserve"},"instructions":{"type":"string","description":"Full intended processing steps in natural language; incoming message placeholder is added automatically if absent"},"card":{"type":"string","description":"Known card name or ID; omit to preserve existing cards"},"review_instructions":{"type":"string","description":"Independent stage-two prompt for callbacks and linked feedback. Natural language; the callback placeholder is added automatically. Keep stage one short."},"on_approve":{"type":"string","description":"User-requested instructions after the selected button. Document and form values are appended automatically; voice/material binding is preserved"},"action":{"type":"string","description":"Button name or label if the card has multiple buttons"},"new_card":{"type":"object","description":"Only a verified published CardKit contract with id,name,template_id,version,variables,actions. Never invent IDs or overwrite an existing shared card"},"dry_run":{"type":"boolean","default":True}},"required":["revision","group"],"additionalProperties":False},"annotations":{"readOnlyHint":False,"destructiveHint":False}},
    {"name":"bridge_list_card_templates","description":"List configured Feishu card templates and required variables. Sending a card requires user authorization to communicate in its original Feishu conversation.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True}},
    {"name":"bridge_send_card","description":"Queue a configured Feishu card in the original conversation, bound to the original Codex task. Use source_message_id from Bridge task context. Reuse request_id when retrying the same send. Queued does not mean delivered.","inputSchema":{"type":"object","properties":{"source_message_id":{"type":"string"},"card_type":{"type":"string"},"variables":{"type":"object"},"request_id":{"type":"string","minLength":8,"maxLength":100}},"required":["source_message_id","card_type","variables","request_id"],"additionalProperties":False},"annotations":{"readOnlyHint":False,"destructiveHint":False,"idempotentHint":True}},
    {"name":"bridge_list_card_deliveries","description":"Inspect card delivery and callback state. sent confirms a Feishu message ID; submitted confirms the callback was queued to Codex, not that work completed.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True}},
    {"name":"bridge_panel","description":"Get the local Codex Bridge task and connection panel URL.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True}},
    {"name":"bridge_list_tasks","description":"List external tasks received by Codex Bridge, including current status.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True}},
    {"name":"bridge_list_connections","description":"List Codex Bridge source-to-project connections without revealing keys.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True}},
    {"name":"bridge_feishu_setup_status","description":"Inspect the isolated 2.0 Feishu setup and identify the next setup step without exposing credentials or pairing codes.","inputSchema":{"type":"object","properties":{},"additionalProperties":False},"annotations":{"readOnlyHint":True}},
]
next(t for t in TOOLS if t['name']=='bridge_configure_workflow')['inputSchema']['properties']['workflow']={
    'type':'object','description':'Declarative multistage workflow: name, steps. Generate from user intent using codex-bridge-workflows Skill. Reuse stable stage IDs, preserve unrequested steps. Applied only to new runs.'}

def fetch(path):
    with urllib.request.urlopen(BASE+path, timeout=5) as response:
        return json.load(response)

def send_card(arguments):
    return post('/api/feishu/cards/send',arguments)

def post(path,arguments):
    req=urllib.request.Request(BASE+path,data=json.dumps(arguments,ensure_ascii=False).encode(),
                               method='POST',headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=300 if path=='/api/feishu/workflow-run' else 10) as response:return json.load(response)
    except urllib.error.HTTPError as exc:
        try: detail=json.load(exc).get('error',{}).get('message','请求失败')
        except Exception: detail='请求失败'
        raise ValueError(detail) from None

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
                respond(req_id,{"protocolVersion":"2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"codex-bridge-v2","version":"2.3.0"}})
            elif method=="ping": respond(req_id,{})
            elif method=="tools/list": respond(req_id,{"tools":TOOLS})
            elif method=="tools/call":
                name=request.get("params",{}).get("name")
                if name=="bridge_panel": data={"url":BASE+"/panel"}
                elif name=="bridge_list_tasks": data=fetch("/api/tasks")
                elif name=="bridge_list_connections": data=fetch("/api/connections")
                elif name=="bridge_feishu_setup_status": data=feishu_setup_status()
                elif name=="bridge_list_card_templates": data={'cards':fetch('/api/feishu').get('cards',[])}
                elif name=="bridge_list_card_deliveries": data={'cards':fetch('/api/feishu/card-deliveries')}
                elif name=="bridge_send_card": data=send_card(request.get('params',{}).get('arguments',{}))
                elif name=="bridge_workflow_context": data=fetch('/api/feishu/workflow-context')
                elif name=="bridge_configure_workflow": data=post('/api/feishu/configure-workflow',request.get('params',{}).get('arguments',{}))
                elif name=='bridge_get_workflow_run':
                    rid=request.get('params',{}).get('arguments',{}).get('run_id')
                    data=fetch('/api/feishu/workflow-runs'+('/'+urllib.parse.quote(rid,safe='') if rid else ''))
                    if isinstance(data,list):data={'runs':data}
                elif name=='bridge_workflow_run':data=post('/api/feishu/workflow-run',request.get('params',{}).get('arguments',{}))
                else: raise ValueError("Unknown tool")
                respond(req_id,{"content":[{"type":"text","text":json.dumps(data,ensure_ascii=False)}],"structuredContent":data})
            else: respond(req_id,error={"code":-32601,"message":"Method not found"})
        except Exception as exc:
            if 'req_id' in locals() and req_id is not None:
                respond(req_id,{"content":[{"type":"text","text":str(exc)}],"isError":True})
if __name__=="__main__": main()
