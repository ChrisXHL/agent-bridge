---
name: bridge-platform-adapter
description: Add a new chat platform (Telegram, Slack, Discord, DingTalk, WeCom, LINE, ...) to Agent Bridge so its messages become agent tasks and results go back to the same chat. Use when the user asks to 接入/新增/适配 a platform other than 飞书 or 微信. Covers product rules, panel UI rules, the adapter contract, scaffolding and acceptance.
---

# 新增聊天平台接入

Agent Bridge 把聊天消息交给本机 Agent 执行，再把结果回到原聊天。飞书是完整的
企业接入；微信（`weixin_bridge.py`）是一个手写的轻量平台。其他平台一律走
**平台框架**：一个文件 `platforms/<id>.py` 继承 `PlatformChannel`，只写平台
特有的四件事，其余（账号、文件夹、Prompt、执行工具、续聊、新对话、记住、
顺序投递、去重、面板页面、API 路由）由框架统一提供，保证所有平台行为一致。

## 你要做的步骤

1. **确认平台和接入方式**。问清（或从用户话里判断）是哪个平台、用哪种官方
   机器人能力。查该平台**官方文档**：机器人身份怎么来（扫码登录 / Bot Token /
   应用凭证）、怎么收消息（长轮询优先；只有 Webhook 的平台见 contract）、怎么
   发文字、附件怎么下载。不要猜接口名、字段或错误码。
2. **生成骨架**：`./scripts/new-platform <id> <显示名> --mode form|qr`。
   `qr` 用于官方扫码登录（像微信），`form` 用于粘贴 Token/凭证（像 Telegram）。
3. **只实现钩子**（见 [adapter-contract.md](references/adapter-contract.md)）：
   `bind_form` 或 `qr_begin`+`qr_poll`、`receive`、`send_text`；必要时
   `format_reply`。只用标准库或 `requirements.txt` 已有依赖；要加依赖先说明理由。
4. **离线核验**：`./scripts/verify-platform <id>`，必须“通过”。它用真实
   Store 和你的类跑完整流程，并检查是否放开了陌生人、泄露凭证、留着 TODO。
5. **真实验收**（见 [acceptance.md](references/acceptance.md)）：重启 Bridge
   让新平台加载（先确认没有扫码或执行中的任务），在面板接入一个真实机器人，
   完成配对，用一条无敏感内容的消息验证 收到 → 执行 → 回到原聊天，再测追问、
   新对话、附件、群聊隔离。
6. **汇报**：说清哪个平台、已验证到哪一步、还差用户做什么（例如在平台后台
   创建机器人、扫码、授权）。不要把离线核验说成真实可用。

## 必守规则（细节见 references）

- **产品**：一个平台账号 = 一个独立机器人，自带工作文件夹、业务 Prompt、
  执行工具和 Bridge 连接。只服务绑定它的人（扫码者或配对者），不能对所有人
  开放——任务在这台电脑上执行。完整规则：[product-rules.md](references/product-rules.md)。
- **界面**：不自己设计页面。框架的 `static/platforms.js` 已按微信页生成导航、
  接入卡、机器人卡片、确认框和设置页；你只提供 `LABEL`、`ICON`、`BIND_*`
  文案。规则：[ui-rules.md](references/ui-rules.md)。
- **不改核心文件**：正常情况下不需要改 `service.py`、`index.html`、`app.js`、
  `platform_channel.py`。确实缺能力时，改框架（让所有平台受益）并补测试，
  不要在单个平台里绕过。
- **凭证**：只存在账号的 `secrets` 里（文件权限 600），不返回给面板，不打日志，
  不贴进对话或提交进仓库。用户自己去平台后台创建机器人、扫码或授权；不要
  索取用户密码。
- **不重放**：`receive` 要保存游标（offset/cursor），重启不重复、不漏消息；
  首次接入不要拉取历史消息当作新任务。

## 参考实现

- `platform_channel.py`：框架本体（读一遍再写）。
- `platforms/_template.py`：骨架模板，注释里有典型写法。
- `weixin_bridge.py` + `static/weixin.js`：扫码登录、长轮询、AES 附件下载的真实例子。
- `test_platform_channel.py`：框架测试，含一个最小的 demo 平台。
