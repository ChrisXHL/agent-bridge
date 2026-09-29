# Codex Bridge 2.3：对话维护的多阶段流程

## 用户入口

使用 `codex-bridge-workflows`，例如：“给翻译 2.0 的配音后面加一个试听审核，不通过就重新生成，通过后再交付。”Codex 读取真实群、项目和音色资源，保留其他阶段，生成简短配置，预检、保存并读回。

流程配置用自然语言发起，查看状态用面板“群流程”。业务步骤放在可复用 Skill；Bridge 处理状态和可靠性。现有任务使用创建时冻结的配置，新配置用于新任务。

## 运行方式

- 每个阶段只包含短业务指令、输入、可校验产物和下一步。
- 顺序执行、多个审核点和退回修改。当前没有并行调度、任意代码条件或自动业务分支。
- 同一原始 Feishu 消息、Bridge task_id 和 Codex task 延续；阶段通过合成入站消息排队。
- 人工审核卡由 Bridge 原生 JSON 自动生成。表单借用已有固定选项配置，包含音色和素材绑定。标准审核不需要重复发布 CardKit 模板。
- 不可变文件副本和 SHA-256、正文评论快照、审核人、轮次和回执持久化到 inbox SQLite 同库事务。
- completion 必须提交精确产物字段；当前阶段结束但未提交会阻断。旧 revision、旧卡片和重复提交不能再次推进。
- 点击卡片先确认收到，由 worker 异步读回版本。出现新增评论/正文变动暂停，不批准另一个版本。修改分支重新捕获文档，完成修订再发卡。
- 付费和发送操作用 effect 账本，已成功复用、不确定暂停。不能保证不支持幂等的云服务绝对只执行一次，不能在未知结果时自动重试。

## API / CLI

GET `/api/feishu/workflow-context`
POST `/api/feishu/configure-workflow`（先 dry_run=true 再 false；相同 revision）
GET `/api/feishu/workflow-runs`
GET `/api/feishu/workflow-runs/<run_id>`
POST `/api/feishu/workflow-run`

写操作 action：complete / fail / retry / reopen_review / cancel / effect_begin / effect_finish。没有人工 approve 接口。所有接口仅本机，写请求拒绝跨源。

CLI：`scripts/configure-workflow --context`、`scripts/workflow-run --get <run_id>`、`scripts/workflow-run --file <JSON文件>`。

字幕例子见 `examples/subtitle-workflow.json`。具体操作契约见已安装 `codex-bridge-workflows` Skill。

## 验证范围与恢复

- 隔离测试使用真实 SQLite 和本机 HTTP 服务；外部卡片发送、云 TTS 和文档内容由夹具模拟。测试通过不代表真实卡片已送达或音频已生成。
- 真实 docx 读取器固定使用 default profile、user 身份，支持 42HOW 文档正文与未解决评论完整分页。没有权限会阻断，不当成零评论。
- FCPXML 验证结构、时间、资源和样式相等。新增 subtitle_manifest、subtitle_alignment、audio_alignment 校验器，分别重算源字幕映射/文档段落绑定、批准文字回填、逐段音频时长与成品。音频段复制留档并与本任务成功合成回执核对；翻译语义与音频自然度仍由业务 Skill/监督完成。未做实际 Final Cut 导入时不得宣称通过。
- 交付核验实际消息所在群和 file_key；上传者仍须检查文件字节身份。
- 文件产物保存副本，没有自动清理。任务配置冻结，进行中的任务不自动迁移。
- 未知卡片发送结果会阻断并通知，不能盲目发新卡；先查飞书记录，必要时维护者核对来源及轮次后修复映射。该少见恢复路径目前没有一键界面。
- 遇到超长音频、已获批文档变化等，用 reopen_review 返回已到达的审核，后续产物失效并等待新批准。已发生的云操作不会撤销。
- 旧版待审核卡保留原回传逻辑，不被本次配置自动接管。


## 卡片页对话入口

卡片页可直接选择群并用一句话提交创建/修改要求，复用该群的 Codex 项目连接。配置任务的处理状态与回复显示回页面，后续补充在同一配置任务继续。没有独立聊天模型或额外 API key。输入区下方只展示当前已保存的卡片、默认选择、素材绑定和审核后的动作。

GET/POST `/api/feishu/card-assistant` 仅本机可用。请求幂等，续聊校验任务与群/连接归属，配置对话与生产业务任务分开；卡片需求不会作为普通飞书消息进入生产流程。配置进度中的“Codex 已回复”不等于业务卡片已发送。新建配置对话使用原有任务引擎；真实生产卡片仍在业务阶段完成后发送。

review 阶段新增可选 fields，支持独立的固定选项，不依赖模板 ID；字段与 form_card 合并，阶段内配置优先。默认第一项，data.label 可指定显示名，data 保留真实音色和素材绑定。现有进行中任务仍冻结原配置。

## 对齐执行工具

- `fcpxml-subtitle-aligner`：extract / bind / apply / verify。保留源文件哈希、精确有理数时间与稳定字幕/文档块 ID。删行、拆行、重排会拒绝，不隐式重新绑定。
- `dashscope-voice-clone-tts/scripts/audio_timeline.py`：prepare / assemble / verify。将完整解码的语音按 48 kHz 样本位置排布；超时拒绝，空隙补静音，不能截词或变速。
- 三个新的产物类型通过固定本机工具重算，不执行 AI 指定的验证代码，不信任报告的 passed。声明可引用同轮输出，按依赖顺序验证且检测循环。
- 合成 `effect_begin.parameters` 包含 segment_id/text/voice_id/model，成功 result 包含 request_id/audio_sha256。新 effect_details 表保存核对参数；历史 effect 保持原有防重复逻辑，不凭空补回执。
- 校验工具并非语音识别或语义监督。421 条真实原件的本地结构测试与模拟音频测试都不能代替真实云 TTS、真人审核和 Final Cut 导入验收。
