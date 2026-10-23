# 恢复说明

## 公开源码备份

- `v2.3-before-prompt`：改版前的群流程及卡片版本，已清除私人资源、凭据和本机路径。
- `v2.4-prompt`：以 Prompt 为入口的简化版本。
- `snapshot-2026-10-15`：2.4 源码快照，包含多应用、多群配置与桌面 Codex 运行环境修复。
- `v2.6.2`：当前版本，新增助手管理员、自动建应用、群话题、媒体回复、Claude Code 后端与 Agent 安装流程。

建议把所需版本克隆到新目录，先查看源码和说明，再切换本机服务。

```sh
git clone --branch v2.3-before-prompt https://github.com/ChrisXHL/agent-bridge.git bridge-restore
cd bridge-restore
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

旧版需要本机 `projects.json`（例如 `{}`），以及曾使用的业务 Skill/校验工具。
公开备份里的飞书域名、音色素材、卡片模板编号和身份配置名是示例值，必须自行重新配置。
GitHub 备份不会恢复登录态、飞书凭据、任务记录或生成文件。

## 本机精确恢复

改版前另有本机私有备份，包含插件源码包、两份一致性数据库快照、飞书配置和
后台服务定义。私有备份含凭据，不能上传 GitHub。先停止 V2 服务，再备份当前数据，
最后决定是只恢复代码还是连同当时数据一起恢复；不要直接覆盖当前数据库或重放消息。
恢复旧代码并连接既有数据后，旧卡片/流程能力可能重新启用，请先检查是否有未完成记录。

历史任务文件始终保留在本机。新版只对历史记录做只读展示，并停止旧的自动阶段调度。
