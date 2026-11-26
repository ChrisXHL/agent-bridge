# 适配器约定

文件：`platforms/<id>.py`，定义 `class Channel(PlatformChannel)`，`Channel.ID`
等于文件名。以 `_` 开头的文件不加载。`feishu`、`weixin`、`tasks`、`connections`
是保留 ID。服务启动时自动加载；加载失败只跳过该平台并记日志，不影响其他功能。

## 需要实现的钩子

### 表单接入（`BIND_MODE = "form"`）

```python
def bind_form(self, fields) -> dict
```
`fields` 是 `BIND_FIELDS` 各 key 的字符串值。调用平台官方接口**验证凭证**，失败
`raise ValueError("用户能看懂的原因")`。返回身份：

```python
{"bot_id": "平台上机器人的唯一 ID",      # 必填，用来去重
 "name": "平台上的机器人名",             # 可选，作为默认名称
 "owner_user_id": None,                  # 表单模式通常为空 → 走配对
 "secrets": {"token": "..."},            # 凭证只放这里
 "cursor": None}                         # 其他自定义字段都会存进账号
```

### 扫码接入（`BIND_MODE = "qr"`）

```python
def qr_begin(self) -> dict      # {"qr_text": 链接} 或 {"qr_png": bytes}，可带 "state"、"message"
def qr_poll(self, login) -> (status, identity, message)
```
`qr_poll` 每秒调用一次，`login["state"]` 是 `qr_begin` 返回的状态，
`login["code"]` 是用户在面板输入的数字验证码。`status` 取
`wait / scaned / need_verifycode / confirmed / expired`；`confirmed` 时返回身份
（同上，扫码模式应带 `owner_user_id` = 扫码人）。

### 收消息

```python
def receive(self, account) -> (messages, account_updates)
```
- 每次调用**阻塞等待一轮**（长轮询 30–40 秒，或在已建立的 WebSocket 上读到超时），
  由框架在独立线程循环调用。不要在里面自己写死循环。
- `account` 是保存的账号（含 `secrets` 和你返回的自定义字段）。
- 每条消息标准化为：

```python
{"message_id": "平台消息 ID（去重用）",
 "chat_id": "回复要发到的会话 ID",
 "chat_type": "user" | "group",
 "sender_id": "发送者 ID（和 owner_user_id 比较）",
 "text": "文字；语音可用平台转写结果",
 "attachments": [{"name": "a.pdf", "data": b"..."}],   # 或 {"name", "path"} 已下载的路径
 "reply_context": "平台回复需要的上下文（可选，如 context_token）"}
```
- `account_updates` 保存游标等（如 `{"cursor": 123}`），保证重启不重放、不漏。
- 登录失效：`raise SessionExpired()`。网络错误直接抛，框架会退避重试并在面板显示。
- 只返回用户发来的消息；机器人自己的、系统事件、编辑/撤回通知都过滤掉。
- 首次接入从“现在”开始，不要把历史消息当新消息。

### 发消息

```python
def send_text(self, account, target, text) -> bool
```
`target = {"chat_id", "chat_type", "sender_id", "reply_context"}`。发送一段纯文字，
平台确认成功才返回 True。分段由框架按 `REPLY_LIMIT` 完成。

### 可选覆盖

- `format_reply(text)`：平台支持 Markdown 时可原样返回或转换成平台格式。
- `is_allowed(account, msg)`：允许名单；不能放开给所有人。
- `REPLY_LIMIT`、`QR_TTL`。

## 收消息方式怎么选

Bridge 只监听本机 `127.0.0.1`，公网回调打不进来。优先选平台的**主动拉取**能力：

| 平台能力 | 做法 |
|---|---|
| 长轮询（Telegram `getUpdates`、微信 iLink `getupdates`） | `receive` 里请求一次 |
| 长连接 / Stream / Socket 模式（Slack Socket Mode、Discord Gateway、钉钉 Stream、飞书长连接） | 在 `self` 上保存连接，`receive` 读到超时返回；断开时重连。venv 已有 `websockets`、`requests` |
| 只有 Webhook | 默认不支持。需要公网入口（隧道/反向代理）时先向用户说明风险并取得同意，不要擅自开放端口 |

## 依赖与安全

- 优先标准库；可用 `requirements.txt` 和 venv 里已有的包。新增依赖要写进
  `requirements.txt` 并说明原因。
- HTTP 一律设置超时；所有平台调用用该机器人自己的凭证。
- 不 `print`、不在日志里输出凭证或消息全文。
- 附件先写到框架给的目录（返回 `data` 即可，框架负责写入并设 600 权限）。

## 框架提供的东西（不用自己写）

账号存储 `platform-<id>.json`（600）、收件箱 `platform-<id>.sqlite3`、附件目录
`platform-<id>-media/`（都在 Bridge 状态目录）；面板 API：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/platforms` | 已加载平台列表（导航用） |
| GET | `/api/platforms/<id>` | 平台信息、机器人、接入状态 |
| POST | `/api/platforms/<id>/bind` | 表单接入 |
| POST | `/api/platforms/<id>/login`、`/login/verify` | 扫码接入、提交数字 |
| GET | `/api/platforms/<id>/qr.png` | 二维码 |
| PATCH | `/api/platforms/<id>/accounts/<aid>` | 改名称、文件夹、Prompt、启停、执行工具 |
| POST | `/api/platforms/<id>/accounts/<aid>/delete` | 删除 |
| POST | `/api/platforms/<id>/self-update` | “记住”规则入口（脚本调用） |

以上只在本机可用。任务通过 Bridge 自己的 `/v1/tasks` 提交，使用该机器人的连接密钥。
