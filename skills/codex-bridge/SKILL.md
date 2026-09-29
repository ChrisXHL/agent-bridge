---
name: codex-bridge
description: Inspect local Feishu-to-Codex tasks, connections, and setup. Edit the message prompt from the local panel.
---

# Codex Bridge 2.4

Use bridge_panel for the panel URL, bridge_list_tasks for external tasks,
bridge_list_connections for source-to-project bindings and bridge_feishu_setup_status
for setup progress. Never show connection keys or App Secret in ordinary chat.
Queued means accepted by Bridge, not confirmed execution or delivery.

The panel has Tasks, Connections, and Feishu. Incoming allowed text messages are
inserted into the saved Prompt at {{message}}. New messages create a task; replies
to messages created in this version continue the same task. Codex follows the
Prompt and the business Skills it calls. Configure steps, outputs and when to ask
for clarification in that Prompt. This plugin does not create review cards or
run a multistage workflow engine. Do not call the retired workflow/card tools.

Historical records are read-only. Do not replay, migrate, or resume them through
the Bridge. A new top-level Feishu message starts a new prompt-driven task.

The Feishu Prompt can be changed on the panel or through the loopback-only
PATCH /api/feishu endpoint with {"prompt_template":"... {{message}} ..."}.
Read the current value first, preserve the user's requested business steps,
validate exactly one placeholder, save only the requested field, then read back.
Editing a prompt never authorizes sending a test message or pretending it ran.
