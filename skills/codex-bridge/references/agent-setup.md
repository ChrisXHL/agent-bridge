# Install and connect for the user

The user should be able to hand this package to their agent and say:
“安装并接入我的飞书，创建助手管理员，再用一条非敏感测试消息验收。”
Handle environment and setup yourself. Ask about business intent when necessary,
not internal implementation choices. Do not start installation merely because
you are reviewing these files.

## Find the package and inspect first

Locate the directory containing `.codex-plugin/plugin.json` and `bridge_setup.py`.
Run `./scripts/bridge-setup doctor` from that directory. Commands return one JSON
object with `stage`, `next_action`, `user_action_required`, and diagnostic checks.
The doctor works before Python package/Node dependencies are installed. If the
host cannot register this plugin, use this CLI and the local panel; the execution
backend remains Codex, not the host agent.

Only macOS with Python 3.11–3.13 is currently supported; prefer Python 3.13.
Node.js/npm and logged-in Codex are required. Use an existing official package
manager or vendor source for missing system runtimes; follow the host's approval
rules. Do not update unrelated global tools or install another model runtime.
Official sources: https://www.python.org/downloads/macos/,
https://nodejs.org/en/download and https://github.com/larksuite/cli.

## Prepare and start

1. `./scripts/bridge-setup prepare` prepares the package-local virtual environment,
   pinned Python/Node dependencies, and a package-local official Feishu CLI if
   none exists. It does not select a Feishu profile or log in as a user. This can
   take minutes: run it in the host's background terminal and retain its session.
2. `./scripts/bridge-setup install` installs the per-user background service and
   checks that it serves this exact package and state directory. Fresh installs
   use free collaboration. Existing collaboration settings remain unchanged.
3. Repeat `doctor` to verify the result. Installation is repeatable and does not
   recreate bots. Keep the extracted package at this location after installation.

An isolated test may pass `--state-dir /absolute/empty/path --port UNUSED_PORT`
to each command. Never set HOME or CODEX_HOME to impersonate a fresh machine.
Do not run an isolated test on a port belonging to the user's real service.

| stage / next_action | Agent action |
|---|---|
| needs_agent / missing runtimes | Prepare the missing official runtime, then repeat doctor. |
| needs_login | Ask the user to complete Codex login; do not request passwords in chat. |
| port_conflict / other_installation | Inspect the existing instance. Reuse it deliberately, or pick a separate unused port; do not kill or overwrite it. |
| existing_installation | An installed launch agent points elsewhere. Inspect it before upgrading; never replace it blindly. |
| busy | Another setup is in progress. Read progress and wait rather than starting a duplicate. |
| command failed | Inspect the private setup log, fix the concrete problem, retry the same command; do not paste full logs or credentials into chat. |
| ready | The local environment and service are ready. This does not prove Feishu message delivery. |

## Create or resume the assistant manager

After the user's explicit setup/creation request, use
`bridge_create_assistant_manager` or `./scripts/bridge-setup manager`.
The tool returns the public job. Poll that same job with
`GET /api/feishu/onboarding/JOB_ID`; do not repeatedly POST creation requests.
Use `verification_url` and `qr_url` as returned. Resolve a relative QR URL against
the local service; show/download that QR through the host's supported image tools.
The user scans the official QR and confirms their own tenant and permissions.
Never manufacture a URL or handle their login password.

When `phase` is waiting_scan, hand over the QR promptly. A user handoff ends the
active setup turn; resume the stored job when they return. Failed or needs_check
means inspect the existing application/credentials before retrying. An ambiguous
create outcome is never proof that Feishu did not create the bot.
When ready, open the returned `open_url`. The creator can describe new assistants
in that Feishu chat; Bridge handles their connections, workspaces and prompts.
For the task coordinator use the existing dispatcher endpoint and ordinary
groups. Do not create a topic-mode group. Do not import the distributor's state.

## Business operations without extra installed skills

The new assistant's prompt includes its own CLI path and profile. Use that exact
prefix and `--as bot`; inspect `--help`, domain help and API schemas as needed.
Do not depend on the distributor's custom skills or another default profile.
Bridge provisions basic document/Base/upload/message scopes. Check effective
platform permissions and the actual resource's access before operating. More
specialized media or external platforms require their own available tools.

## Acceptance and recovery

Keep environment readiness, bot connection, message receipt, actual Codex
execution and final Feishu reply separate. Only mark the latter three verified
after a newly authorized non-sensitive message reaches the intended workspace,
executes, and returns to its original Feishu conversation. If the user authorized
the test, the agent may send it using the user's supported Feishu UI/session;
otherwise ask them to send one test message and report the unverified boundary.
Never forge a user event or write success into the database to make a test pass.

For an upgrade, first inspect active tasks, creation sessions and management
requests. Wait for an idle window, back up local state privately, then update.
Do not restart during QR registration, change existing application subscriptions,
replay historical tasks, or distribute profiles, credentials, databases or logs.
Register the plugin through the host's supported installation mechanism when
available; extracting a ZIP alone does not register tools in every agent product.
