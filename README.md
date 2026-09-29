# Codex Bridge 2.0 · 本机飞书接入开发版

此目录从 1.0 源码复制而来，运行状态完全独立：默认面板
`http://127.0.0.1:8797/panel`，数据库 `data/bridge.sqlite3`，
飞书配置 `data/feishu.json`，飞书收件记录 `data/feishu-inbox.sqlite3`。
原有 1.0 服务、连接密钥、任务历史和飞书中转电脑均未被这个开发版修改。
`README-1.0-reference.md` 是复制时的旧版说明，不适用于 2.0 的端口和后台服务。

## 当前能力

- 本机飞书长连接接收 `im.message.receive_v1`，无需公网回调地址。
- 飞书文字消息先持久化到独立收件记录，再以稳定 `request_id` 交给 Bridge 原有任务队列；重复推送不会重复建任务。
- 内置可编辑 Prompt 模板，使用 `{{message}}` 插入飞书原文。收到消息时固定生成的完整 Prompt，再作为新 Codex 会话的第一条消息；回复旧消息则沿用原会话。
- 私聊需发送者 Open ID 在允许列表；群聊还需群 Chat ID 在允许列表、发送者在允许列表、消息明确 @ 指定机器人。
- 回复已有消息时，能沿用对应的 Codex 任务；完成、失败或需人工处理后，向原飞书消息回复状态。
- 飞书凭据只保存在本机权限为 `0600` 的配置文件中，面板只显示“已保存密钥”，不回传明文。
- 2.0 面板仅监听本机回环地址；`scripts/enable-lan` 在开发版中禁用。
- 接入向导只需应用凭据和 Codex 项目：验证凭据后自动建连接，从飞书接口读取机器人 Open ID；向机器人发送短期配对码后，面板自动识别用户和群 ID，确认后加入允许名单。

文字任务已支持文档审核卡片：Codex 的最终回复若单独包含
`FEISHU_REVIEW_DOC_URL: <HTTPS 飞书文档链接>`，Bridge 会向原消息回复
CardKit 模板 `EXAMPLE_TEMPLATE_ID`（版本 `1.2.0`）。OpenClaw 应用需订阅
`card.action.trigger`。审核人点击通过后，Bridge 校验身份、原会话和示例音色音色与素材链接，
只向原 Codex 任务提交一次后续消息。代码见 `feishu_review.py`、`feishu_worker.py`，
测试见 `test_feishu_review.py`。审核卡片只识别显式标记；普通完成消息仍回复文字。

图片、附件、富文本和旧中转机上的自定义转换流程尚未迁入，
不能据此停用承担这些功能的旧连接器。Bridge 当前仍以 Full Access、无需审批的
Codex 策略运行，飞书允许列表应仅包含可信任的本人账号。

## 本机启动

```sh
cd /path/to/codex-bridge
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./run.sh
```

打开 `http://127.0.0.1:8797/panel`，进入“飞书”页：

1. 在飞书开发者后台创建企业自建测试应用，复制 App ID 和 App Secret；在向导中选择 Codex 项目并点击“验证并保存”。Bridge 自动验证凭据、识别机器人并创建 2.0 独立连接。
2. 在飞书开发者后台启用机器人，选择长连接，订阅 `im.message.receive_v1` 并发布应用。回到向导点击“检查机器人状态”。
3. 点击“生成配对码并启动接收”，向机器人发送配对码。面板收到后显示用户或群 ID；确认绑定后，再发送普通任务消息。配对码有效 10 分钟，且不会创建 Codex 任务。

飞书进程在生成配对码时才会启动。配置变化时它自动重启；Bridge 任务执行进程不重启。
高级设置仍可手动调整允许名单、Prompt 模板和连接。若旧电脑也在监听同一个
飞书应用，两个长连接会分流事件，应先使用独立测试应用。
2.0 插件还提供只读的 `bridge_feishu_setup_status` 工具；安装插件后，Codex 可在
对话中查看当前配置阶段并告诉你下一步，工具不会返回 App Secret 或配对码。
“飞书”页可修改 Prompt 模板并用示例消息预览。模板必须恰好包含一次
`{{message}}`；最终合成的消息须符合 Bridge 的 12000 字和 16 KiB 请求限制。修改模板只影响之后收到的
飞书消息，不改动已入库消息或既有任务。

飞书开放平台仍需启用机器人，选“使用长连接接收事件”，订阅
`im.message.receive_v1`。私聊可使用 `im:message.p2p_msg:readonly`，群聊 @ 机器人
可使用 `im:message.group_at_msg:readonly`，回复使用 `im:message:send_as_bot`，
并发布应用。飞书 SDK 的[长连接说明](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/event-subscription-guide/long-connection-mode)
及[接收消息事件](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)
是实施依据。要先确认旧电脑究竟使用长连接还是 HTTP 回调；切换同一应用的
事件订阅方式会影响旧连接器。

## 常驻运行与切换

`./scripts/install-daemon` 安装独立的 `dev.codex.bridge.v2` LaunchAgent，
使用 `~/Library/Application Support/CodexBridgeV2/`，不覆盖 1.0 的
`dev.codex.bridge`。运行前先停止手动启动的 8797 服务。

正式切换前须核对旧中转机的消息类型、附件处理、回复方式和路由规则。
建议使用独立飞书测试应用和测试群完成真实收发。验证通过后，确认旧机
没有在途事件，再切换生产飞书应用并停用旧连接器。1.0 Bridge 可继续
保留回滚；历史任务不迁移、不重投。

这版为满足“只用这台 Mac”先使用原生常驻进程。Docker 容器化飞书接收器
尚未完成安全的容器到本机 Bridge 通道，因此当前没有 Docker 部署承诺。

## 验证

```sh
.venv/bin/python -m unittest -q test_service.py test_feishu.py
node --check static/app.js
```

测试只使用临时数据库和替身飞书客户端，不发送真实飞书消息。

## 2.1 升级：多卡片与群流程

面板新增“群流程”和“卡片”。同一个 OpenClaw 应用可以接入多个已配对群；每个群设置自己的 Codex 项目连接、Prompt 和可用卡片。未单独设置的会话沿用默认配置。修改配置只影响新任务；旧任务的回复与卡片回传保留原项目和规则。

新增卡片：先在飞书 CardKit 发布模板并关联 OpenClaw，再在“卡片”中填写模板 ID、固定版本、变量及按钮 name。按钮回传可引用 `{{variables.变量名}}` 和 `{{form.字段名}}`。表单支持 text/url/number/boolean/object/choice；choice 可将选择值绑定到固定音色及素材链接。每张卡片接受原任务发起人的一次决定。

内置 `voice_review` 使用已发布的 1.2.0 模板，默认示例音色，变量为 `review_doc_link`。新工作流第 4 步应写为：

> 文档创建完成后，调用 Bridge 的 bridge_send_card，卡片类型 voice_review，将真实飞书文档链接传入 review_doc_link。确认卡片已发送后结束本轮，等待人工审核通过；收到回传后按选定音色及素材继续处理。

工具参数：
```json
{"source_message_id":"<Bridge 上下文里的原消息 ID>","card_type":"voice_review","variables":{"review_doc_link":"<真实飞书文档链接>"},"request_id":"review-document-001"}
```

`bridge_list_card_templates` 查看模板；`bridge_list_card_deliveries` 查看状态。同一逻辑发送重试须使用相同 request_id。仅在普通回复中放链接不会触发卡片；旧的 `FEISHU_REVIEW_DOC_URL:` 显式标记仍兼容。

当前 Codex 会话尚未刷新 MCP 工具时，可用 `/path/to/codex-bridge/scripts/send-card`：`--list` 查看模板，`--deliveries` 查看状态，发卡用 `--source-message-id`、`--card-type`、`--variables-json`、`--request-id`。Bridge 自动把上下文和调用方法提供给新任务。

发送与回传持久化到 SQLite。queued 是排队，sent 才代表拿到飞书消息 ID，pending 表示已接收决定，submitted 表示交给 Codex，均不等于处理完成。网络错误导致 delivery_uncertain 时不自动重发，需人工核对。审核时原任务尚在执行，会等待原轮完成再继续；重复点击不会创建重复续跑。

旧版已发出的审核卡片继续兼容。本版支持同一飞书应用下多个群及配置式按钮/表单卡片；多个应用凭据、动态卡片联动、任意插件式执行器不在本次升级范围内。没有真实 CardKit 模板和目标群配对时，仅添加配置不能证明线上可用。

升级验证：
```sh
.venv/bin/python -m unittest -q test_service.py test_feishu.py test_feishu_scenarios.py test_feishu_review.py test_multi_cards.py
node --check static/app.js
node --check static/flows.js
```
47 项自动化测试包括两个群对应不同项目、两类卡片、固定规则回传、防重复、断线恢复和旧卡兼容；使用隔离数据库及替身飞书客户端。新增卡片与群流程的面板保存另外通过浏览器验证。真实飞书审核仍由用户点击完成。

## 2.2：对话配置

直接对 Codex 描述“哪个群、处理什么、用什么卡片、通过后做什么”。已安装 `codex-bridge-workflows` Skill，Codex 会读取现有群、项目、卡片、音色与素材绑定，自行匹配、预览校验、保存并读回。用户无需输入模板字段或回调名称。未明确要求修改的配置会保留；群专属的按钮指令会使用独立卡片规则，已发送卡片继续使用发出时的快照。

工具为 `bridge_workflow_context`、`bridge_configure_workflow`。会话尚未刷新 MCP 时，Skill 使用 `scripts/configure-workflow --context` 和 `--file <JSON 参数文件>`。参数支持群名、项目名、卡片名、自然语言处理步骤和通过后指令。相同名称对应多个对象时明确返回歧义；revision 防止过期配置覆盖。dry_run 只校验，不写配置，也不发消息。

新增飞书模板依旧要由 Codex 利用飞书工具或浏览器实际创建、发布并核对契约；不能通过编造模板 ID 完成。新群仍须配对。当前能力在 Codex 对话中使用，面板保留读回及高级手动配置，没有另加一套面板聊天模型。

## 两阶段字幕流程

群流程现有两个独立 Prompt：第一阶段 `prompt_template` 负责翻译和发起审核；第二阶段 `review_prompt_template` 负责卡片提交后的修订、配音与交付。自然语言配置工具的 `review_instructions` 可单独修改第二阶段。发送卡片的技术参数由 `feishu-subtitle-dubbing` Skill 和 Bridge 上下文承担。

审核人把修改意见写在飞书文档评论中，回到卡片选择音色并提交。文档评论不是卡片回调的一部分：提交后 Codex 在原任务里重新读取正文、未解决评论及回复，按业务步骤处理。当前没有订阅评论实时事件，单独写一条文档评论不会自动触发配音。

Bridge 回传携带原 task_id、最初来源消息及输入摘录、文档链接和音色素材。初始 FCPXML 的实际文件/hash/逐单元映射由 Skill 保存到项目任务清单。时间轴结构保留、逐段语音时长和飞书文件回执要在实际业务执行时校验；本次代码测试不代表已合成音频或验证 Final Cut 导入。


## Public backup and privacy

This snapshot contains source code only. Credentials, local project mappings, message history, runtime databases, generated artifacts and machine-specific service definitions are excluded. Example Feishu resources and template identifiers are placeholders. Reconfigure these resources locally before restoring the older workflow functionality. The pre-prompt tag preserves this version; main contains the simplified prompt-driven version.
