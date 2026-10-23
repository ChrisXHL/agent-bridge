---
name: codex-bridge
description: Install, diagnose, recover and connect the local Feishu-to-Codex Bridge; create an assistant manager via official QR and inspect real task delivery.
---

# Agent Bridge (based on Codex Bridge 2.6)

Use bridge_panel for the panel URL, bridge_list_tasks for external tasks,
bridge_list_connections for source-to-project bindings and bridge_feishu_setup_status
for setup progress. Never show connection keys or App Secret in ordinary chat.
Queued means accepted by Bridge, not confirmed execution or delivery.

For installation, first-time setup, sharing or recovery, read
[Agent setup](references/agent-setup.md). Run the packaged bridge-setup commands
instead of handing shell commands or credential fields to the user. The user
completes official login/QR/tenant approval. Do not assume the service exists
because the MCP server registered. Inspect a running installation before changes.
After setup, reuse the existing manager or resume its saved job; do not recreate it.

The panel has Tasks, Connections, and Feishu. Incoming allowed text and rich-text
messages are inserted into the saved Prompt at {{message}}. Images, files, audio
and video are downloaded with that application's own credentials before task
submission. Rich-text messages can carry documents in a separate files list;
the receiver downloads those alongside the text and inline images. The prompt includes a private local manifest; actually open/read the
attachments, never infer contents from their names. Receiving audio/video does
not itself prove transcription or semantic analysis. Private messages from the
same app, connection, chat and sender continue the same Codex conversation by
default, including separately sent attachments and text. Each message keeps its
own task record and collaboration budget; completed records are never rewritten.
The exact commands “新对话”, “重新开始对话” or /new start a fresh private conversation.
On the first follow-up to a formerly fragmented chat, Bridge supplies a private
history file; read it before answering, use it as context and never replay old
actions. Older context is bounded to the latest 100 receipt records. Codex follows the
Prompt and the business Skills it calls. Configure steps, outputs and when to ask
for clarification in that Prompt. This plugin does not create review cards or
run a multistage workflow engine. Do not call the retired workflow/card tools.

For a group task coordinator, use “创建任务管家” or POST /api/feishu/dispatcher
with an empty JSON body. Reuse its existing creation record. The owner completes
the official creation QR, adds it to an ordinary group and sends the first @ request.
Only that application's verified creator may activate a new group this way;
the same message starts the task without a pairing-code step. The coordinator
discovers and calls only business assistants already bound to that group.
Existing consent rules still apply; group membership does not grant access to
other assistants' private data or authorize unrelated assistants.

Historical records are read-only. Do not replay, migrate, or resume archived
tasks through the Bridge. Ordinary group root messages remain independent;
private conversation history must never be imported into a group task.

The Feishu Prompt can be changed on the panel or through the loopback-only
PATCH /api/feishu endpoint with {"prompt_template":"... {{message}} ..."}.
Read the current value first, preserve the user's requested business steps,
validate exactly one placeholder, save only the requested field, then read back.
Editing a prompt never authorizes sending a test message or pretending it ran.

Message-resource downloads accept im:message, im:message:readonly, or
im:message.history:readonly; im:resource alone is insufficient. If a read-only
scope is not selectable, search the console for 获取与发送单聊、群组消息
(im:message); do not claim the read-only scope was renamed or retired without
evidence. Existing applications may need their
owner to enable and publish the permission. Do not substitute another bot/user
identity. The receiver reports download failures in the original conversation.
Each resource is limited to 100 MB; large media can use an accessible cloud link.
In groups, @ in a rich-text post or reply with an attachment to the same sender's
existing addressed task. Unrelated group attachments are never ambient input.

Business assistants receive a task-scoped collaboration helper automatically.
Use its injected context to call known assistants by ID or an exact unique name.
They may delegate further (three edges deep, eight total children per root),
but may not call ancestors or themselves. Collect every batch before finishing.
Only the root assistant sends the final Feishu reply or publishes deliverables.

Opt-in group collaboration posts ordinary messages in the main Feishu group chat.
Do not use thread replies, create a topic-mode group, or require the topic sidebar.
Bridge internally associates related replies with the same task. The first
targeted bot owns it; other bot receivers do not
create duplicate tasks. New root messages and other groups remain separate.
Ordinary group messages and bot-generated messages never create tasks. Existing
groups keep their previous behavior until explicitly enabled. Enabling checks
actual granted im:message.group_msg plus a message-reading scope; do not equate
receiving @ events with receiving unmentioned thread replies.

For these tasks the injected helper supports say, ask, question, questions and
answer. Use them for real progress and clarification with the delegating
assistant; wait returns pending questions early so the delegator can answer.
An unanswered question remains pending even if a model turn ends. Bridge resumes
the same child task and chat after the answer arrives; read it using question
before completing the original assignment. Do not treat a wait timeout as success.
Do not role-play colleagues or send their messages through an unrelated bot.
Bridge posts under each helper's identity only when it is explicitly bound to
that group; otherwise the owner posts a labeled relay. The owner alone publishes
the final deliverable. Each execution task has a 24-message discussion budget;
normal depth, call count and authorization limits still apply. Group use never
grants access to private conversations or another member's personal credentials.

The local panel can enable free collaboration using POST
/api/feishu/collaboration-policy with {"mode":"open"}. Ready, enabled business
assistants can then delegate without repeated consent. Dispatchers still call
only helpers bound to their current group. Archived assistants, inactive tasks,
cycles, depth and call-count limits remain enforced. Use {"mode":"confirm"}
to restore task-only consent for calls without a pre-existing grant.

In confirmation mode, unconfigured collaboration pauses for task-only consent in Feishu. The verified
creators receive a plain-language request; reply “允许本次协作” or
“拒绝本次协作”. With multiple requests, reply to the intended notice.
No visible code is required. Legacy coded replies still work. Generic assent
such as “好的” or “对的” is not consent.
Do not impersonate creators, edit grants, or create a replacement batch while
waiting. Poll the original batch. Common-manager ownership may share a single
confirmation; Open IDs from separate apps never prove common ownership.
