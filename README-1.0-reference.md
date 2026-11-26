# Codex Bridge · 外部任务连接器 MVP

可运行的本地插件：**任务 / 连接** 两页沿用 V3.2 原型的视觉。外部来源固定绑定一个 Codex 项目。首条任务创建持久 Codex 会话；同一 `task_id` 的后续消息进入原会话。SQLite 保存连接、任务、消息和设置。

## 已验证的链路

2026-09-24 在本机 Codex CLI 0.156.1 上实测：创建连接 → 发首条真实任务 → 新建 Codex 会话 → 面板显示任务 → Codex 完成并读回结果 → 同一 `task_id` 续发 → 原会话收到第二条消息 → 重复 `request_id` 返回原任务 → 连接变为“可用”。自动化测试覆盖授权范围、幂等、等待与恢复、项目暂时不可用、HTTP 页面，以及命令和文件修改审批回传。

## 安装与打开

此目录是完整插件包。需要 macOS、Python 3.10+、已登录的 Codex CLI，且 `codex` 可在终端运行。项目列表实时读取本机 Codex 的 `state_*.sqlite` 项目记录；`projects.json` 保留已有连接的项目标识及手动修复的位置，即使目录暂时不可访问也会显示，以便修复。新建 Codex 项目后，再打开“创建连接”即可看到。首次运行可直接执行 `./run.sh`，再在 Codex 内浏览 `http://127.0.0.1:8787/panel`。

本机常驻安装采用个人插件市场与 macOS LaunchAgent。开发时先把整个目录复制到 `~/plugins/codex-bridge-mvp/`，安装 `codex-bridge-mvp@personal`，再在复制后的目录执行 `./scripts/install-daemon`。后台接收服务随登录启动，Codex 桌面端关闭时保持任务排队；桌面端重新打开后，队列会自动继续。当前机器已完成这两步，面板地址是 `http://127.0.0.1:8787/panel`。

Bridge 同时最多运行 2 条 Codex 任务；超出的任务保留在“等待 Codex”，有任务结束后按提交顺序派发。服务启动时会尝试将文件句柄软上限提高到 4096，供其启动的 Codex CLI 进程继承；实际结果写入服务日志。失败任务不会自动重投，以免重复执行已经产生的修改。

当前 Codex 插件包提供 MCP 工具和技能入口；V3.2 页面由本地服务提供，在 Codex 的浏览器面板打开。官方插件清单目前没有把任意本地 HTML 注册为常驻原生侧栏页面的字段，因此这不是原生侧栏扩展。

## 使用

1. 打开“连接”，创建连接：填任务来源、选择 Codex 项目。
2. 创建后点“复制连接信息”，交给任务来源。连接先显示“待验证”。
3. 第一条真实任务到达并经 Codex 消息读回后，显示“可用”。
4. 在“任务”查看需要处理、最近任务和历史任务；点开查看原始内容、后续要求与结果。
5. 连接设置可停用连接、修复被移动的项目位置，并展开查看接口与密钥。

普通页面隐藏技术细节。本机面板保留回环地址；可另行启用局域网 HTTP 入口。连接密钥只在创建、复制和技术信息里出现，保存在权限为 `0600` 的本地 SQLite 文件中。

Bridge 投递的 Codex CLI 会话使用 Full Access（`danger-full-access`）和 `never` 审批策略。新任务与现有任务的后续消息都按此设置运行。macOS 的“完全磁盘访问权限”是另一层授权；安装脚本在 npm 包含原生 Codex 程序时直接启动它，避免经过 Node 启动器。若要让任务访问桌面或受保护目录，请在系统设置中为实际运行的 Codex 原生程序授权。CLI 或 npm 包更新后，应重新核对程序路径和授权。`strict-supervisor` 还需要在项目目录写入监督快照；要求禁止一切本地文件写入的任务无法完成它的正式流程。

## 局域网访问

在 Bridge 所在的 Mac 上运行 `./scripts/enable-lan`。脚本会使用当前 `en0` 的私有 IPv4 地址，配置随登录启动的服务，并验证局域网健康检查。也可以传入指定地址，例如 `./scripts/enable-lan 192.168.1.16`。局域网面板地址为 `http://<Mac 的局域网 IP>:8788/panel`，无需证书或面板密码。Mac 的局域网 IP 改变后，重新运行 `enable-lan`。

局域网入口仅绑定该私有 IP。管理页面及 `/api/` 不要求登录，页面可查看连接密钥；`/v1/tasks` 仍要求各连接自己的 Bearer 密钥。从局域网面板复制连接信息时会得到 HTTP 局域网接口地址。请只在你信任的局域网内使用，不要在路由器上转发 8788 端口。

## 发送任务

以下是开发接口；复制连接信息会自动包含实际地址和密钥。

```sh
curl -sS -X POST http://127.0.0.1:8787/v1/tasks \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $CONNECTION_KEY" \
  -d '{"message":"请检查项目 README","request_id":"demo-request-001"}'
```

返回 HTTP 202：`{"task_id":"task_...","status":"等待 Codex","duplicate":false}`。服务接收后异步投递；202 只证明入队。重复 `request_id` 和同内容返回 HTTP 200、同一任务；同编号不同内容返回 409。

项目目录或 Codex 项目记录暂时读不到时，任务保持“等待 Codex”，原消息留在队列中自动重试。任务详情会提示检查项目位置；恢复后会自动派发，其他可用项目的任务仍可继续。已在旧版本中标为“失败”的任务不会自动重跑。

继续任务：

```json
{"task_id":"task_...","message":"刚才的要求再调整一下","request_id":"demo-request-002"}
```

`GET /v1/tasks/<task_id>` 用同一密钥查询状态与结果。调用方不能指定项目路径、权限或其他连接的任务。内部管理接口仅供本机面板使用。

## 启动、测试和数据

```sh
./run.sh
python3 -m unittest -v test_service.py
```

`.env.example` 给出可覆盖的端口、数据库、项目清单和 Codex 可执行文件。手动运行默认写 `bridge.sqlite3` 到当前目录。常驻安装的数据库位于 `~/Library/Application Support/CodexBridge/bridge.sqlite3`；日志在 `~/Library/Logs/CodexBridge/`。`./scripts/uninstall-daemon` 停止后台服务并移除启动项，保留本地数据。

`codex_http_bridge.py` 是之前验证过的 HTTP Bridge 源文件，复用了其中的 App Server RPC 客户端和消息读回规则。`LocalCodexTransport` 隔离了 Codex 通道；以后增加云端转发只需换传输入口和排队方式，产品数据模型不需要重写。

Bridge 会话由这台 Mac 上独立运行的 Codex CLI app-server 创建，记录保存在同一台 Mac 的 `~/.codex/sessions`。在这台 Mac 的终端运行 `codex resume --all` 可从列表查看；也可以用面板任务对应的 `thread_id` 运行 `codex resume <thread_id>`。其他电脑的本地 CLI 不会自动同步这些会话。任务完成后 Bridge 会关闭空闲的 app-server，释放会话写入权；CLI 接管会话期间，Bridge 对该会话的后续消息会等待它退出。

## 当前边界

- App Server 以工作目录绑定项目；本机 0.156.1 不能设置桌面端 `projectId`。会话真实存在且能读回，但可能显示在普通任务列表，而非项目分组。
- 局域网 HTTP 入口不加密且不要求面板登录；它不提供互联网或云端 Bridge。
- 命令和文件修改审批会显示为“需要你处理”，面板可明确允许或拒绝，决定会送回同一个 Codex App Server 进程；这一分支有协议替身测试，尚未触发真实高影响审批做现场验证。其他类型的交互请求还未接入。服务重启时未决审批不会自动重放，请打开对应任务继续处理。
- 任务被标为 `uncertain` 时不会盲目重放。先查看 Codex 中的任务，再决定如何继续。
