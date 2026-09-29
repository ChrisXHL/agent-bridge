# Codex Feishu Bridge 2.4

把飞书消息填入 Prompt，交给这台 Mac 上的 Codex 处理，并向原消息回复结果。

## 简化后的结构

- **任务**：查看收到的任务、执行状态和结果。
- **连接**：管理消息来源与 Codex 项目的对应关系。
- **飞书**：配置应用、配对允许的用户或群，以及编辑任务 Prompt。

新飞书消息 → `{{message}}` 替换 → 同一个 Codex 任务连续处理 → 文字回复。
处理步骤、使用哪些 Skill、何时询问用户，都由 Prompt 决定。插件不再内置群流程、
审核卡片、卡片回调或分阶段工作流。付费操作的防重复与产物校验需由执行这些操作的
业务 Skill 自行负责；Bridge 仅对消息接收、任务提交和回复提供稳定请求标识。

旧版任务和数据库保留只读，既不重放也不继续旧流程。继续新工作请发送一条新的飞书消息。
新版本创建的任务支持通过回复原消息继续同一任务；前一轮完成后才提交后续消息。

## 安装运行

需要 macOS、Python 3.13、已安装并登录的 Codex CLI，以及企业自建飞书应用。
Codex 在宿主机执行，当前没有 Docker 部署脚本。

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./run.sh
```

打开 http://127.0.0.1:8797/panel 。程序自动读取本机 Codex 项目。
可选的 `projects.json` 使用 `{ "项目名": "/absolute/project/path" }`，该文件属于本机配置，不提交到 Git。

在飞书页面依次连接应用、检查机器人、配对用户或群。群消息要求同时满足：
发送者已允许、群已允许、明确 @ 当前机器人。只接收文字消息；可以在文字中附文档链接。

飞书后台选择长连接，订阅 `im.message.receive_v1`，启用机器人并发布应用。
私聊使用 `im:message.p2p_msg:readonly`，群聊使用 `im:message.group_at_msg:readonly`，
回复使用 `im:message:send_as_bot`。同一个应用的其他长连接可能分流事件。
新版不需要卡片回调订阅。

可在“任务 Prompt”中编辑规则并预览。必须恰好包含一次 `{{message}}`；保存只影响之后收到的消息。
默认 Prompt 不添加强制人工审核。示例：

```text
飞书任务：{{message}}

根据消息完成工作，按需调用已安装的 Skill。
完成后说明结果；缺少必要信息或超出授权范围时询问我。
```

## 后台服务

`./scripts/install-daemon` 安装 `dev.codex.bridge.v2`，端口 8797。
数据默认保存在 `~/Library/Application Support/CodexBridgeV2/`，日志位于
`~/Library/Logs/CodexBridgeV2/`。`./scripts/uninstall-daemon` 仅停用后台服务。
直接运行 `run.sh` 使用插件目录下的 `data/`。不要同时启动两个实例占用同一端口。

Codex 以 Full Access、无需命令审批的策略执行；飞书允许名单应只包含可信任用户。
面板仅监听本机回环地址。密钥仅保存在本机；公开仓库不含凭据和用户数据。
不要在日志或聊天中粘贴连接密钥。已由 Codex 桌面占用的任务可能无法由独立
Bridge 进程恢复，面板会显示原因；此版本不声称解决跨进程任务所有权问题。

## 测试

```sh
.venv/bin/python -m unittest -q test_service test_feishu test_feishu_scenarios test_prompt_mode
```

测试使用临时数据库、模拟飞书发送器和模拟 Codex，不会发送真实消息。
HTTP 接收成功只代表排队，实际执行应以任务状态和 Codex 读回为准。

## 版本恢复

见 [RESTORE.md](RESTORE.md)。`v2.3-before-prompt` 是改版前经过隐私清理的源码备份；
`v2.4-prompt` 是去掉群流程和卡片后的版本。
