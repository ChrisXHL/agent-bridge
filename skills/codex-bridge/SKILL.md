---
name: codex-bridge-v2
description: Manage Codex Bridge group workflows and send configured Feishu review cards.
---

# Codex Bridge 2.0

Use `bridge_panel` to find the isolated 2.0 panel, `bridge_list_tasks` for task status, and `bridge_list_connections` for source-to-project bindings. For requests to configure Feishu, call `bridge_feishu_setup_status` first and give the user its concrete next step. The panel's Feishu wizard validates credentials, creates the Bridge connection, identifies the bot, and pairs the sender by a short code; never ask the user to copy Open IDs or Chat IDs into chat. App ID and App Secret should be entered in the local panel, never ordinary chat. The user must still create and publish a self-built app and subscribe to the message event in the Feishu developer console. If another machine listens to the same app through a long connection, use a separate test app first because events are distributed across clients. Never show connection keys, Feishu secrets, or pairing codes in ordinary chat. Queued or uncertain delivery is not proof that Codex received a message; check task status and readback.

## Send a review card (2.1)

When the user requests a review card, first call `bridge_list_card_templates`. Use the source message ID and allowed card IDs explicitly provided in the Bridge task context. Never invent a source ID or borrow another task's ID.

After creating the review document, call `bridge_send_card` with:
- `source_message_id`: the original Feishu message from task context
- `card_type`: `voice_review` for the existing Example voice document review
- `variables`: `{"review_doc_link":"<actual Feishu document URL>"}`
- `request_id`: a stable unique value for this logical send (8–100 characters); reuse on retry

Then inspect `bridge_list_card_deliveries`. `queued` means waiting; `sent` with a message ID means sent. `delivery_uncertain` must be checked manually before any fresh send. End the current response and wait for the human decision; do not approve for the user or start the dependent voice task early.

The dropdown is bound to the configured voice/material pair. The original requester submits the form; Bridge validates it and resumes the same Codex task with the document URL and selected pair. A callback `submitted` means queued to Codex, not completed processing. Continue according to the configured callback instructions and report the actual result.

If the newly added MCP tools are unavailable in the current session, use the equivalent local CLI:
`/path/to/codex-bridge/scripts/send-card --list`
Then call it with `--source-message-id`, `--card-type`, `--variables-json`, and `--request-id`. Use proper shell quoting or a subprocess argument array. No credentials are needed. `--deliveries` reads status.

The panel has “群流程” for each paired group's project/Prompt/card choices and “卡片” for published CardKit templates, variables, button names and callback instructions. New templates must be published in Feishu and linked to this OpenClaw application before use. Existing cards retain their original rules and task binding. Ordinary document links alone do not trigger a card. The legacy explicit `FEISHU_REVIEW_DOC_URL:` marker remains compatible for `voice_review`; prefer the tool for new workflows.

## Configure by conversation

For natural-language requests to add/change a group workflow, use the installed `codex-bridge-workflows` skill at `~/.codex/skills/codex-bridge-workflows/SKILL.md`. Read `bridge_workflow_context`, resolve the user's group/project/card names, preview with `bridge_configure_workflow`, then save and read back. CLI fallback: `/path/to/codex-bridge/scripts/configure-workflow --context` and `--file <JSON argument file>`. The user's configuration request authorizes its save; ask only about genuinely missing choices. Avoid sending users to technical forms.


## 多阶段任务

配置和执行多阶段任务使用已安装的 `codex-bridge-workflows` Skill。读取 workflow context 后生成声明式阶段，通过 preview/save 保存；执行时读取 run_id 账本，仅完成当前阶段并提交产物。不能模拟人工审核，不能在等待审核时自行继续。未刷新 MCP 的任务用 `/path/to/codex-bridge/scripts/workflow-run`。
