"""Natural-language card requests use the existing Codex task engine and saved group."""
import json
import time
from codex_http_bridge import BridgeError
from workflow_admin import context, resolve

def ensure(store):
    with store.db() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS card_design_requests (
            request_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, connection_id TEXT NOT NULL,
            task_id TEXT NOT NULL, message TEXT NOT NULL, created_at REAL NOT NULL)''')

def list_requests(store):
    ensure(store)
    with store.db() as db:
        rows=db.execute('''SELECT d.*,t.status,t.result,t.thread_id FROM card_design_requests d
            JOIN tasks t ON t.id=d.task_id WHERE d.rowid IN
            (SELECT MAX(rowid) FROM card_design_requests GROUP BY task_id)
            ORDER BY d.created_at DESC LIMIT 30''').fetchall()
    return [dict(r) for r in rows]

def submit(path,store,args):
    if not isinstance(args,dict) or set(args)-{'chat_id','message','request_id','task_id'} or not {'chat_id','message','request_id'}<=set(args):
        raise BridgeError(400,'invalid_card_request','请提供群和卡片需求')
    message=args['message'];key=args['request_id']
    if not isinstance(message,str) or not 1<=len(message.strip())<=4000:raise BridgeError(400,'invalid_card_request','请用 1 到 4000 字描述卡片需求')
    if not isinstance(key,str) or not 8<=len(key)<=100 or not all(c.isalnum() or c in '-_' for c in key):raise BridgeError(400,'invalid_request_id','请求标识无效')
    if not isinstance(args['chat_id'],str):raise BridgeError(400,'invalid_group','请先选择群')
    if args.get('task_id') is not None and (not isinstance(args['task_id'],str) or not 1<=len(args['task_id'])<=100):
        raise BridgeError(400,'invalid_task_id','配置对话标识无效')
    ctx=context(path,store);group=resolve(ctx['groups'],args['chat_id'],('chat_id',),'群')
    conn=store.connection(group['workflow']['connection_id'])
    if conn['status']=='已停用':raise BridgeError(409,'connection_disabled','这个群的 Codex 连接已停用')
    ensure(store)
    with store.db() as db:
        old=db.execute('SELECT * FROM card_design_requests WHERE request_id=?',(key,)).fetchone()
        if old:
            if old['chat_id']!=args['chat_id'] or old['message']!=message:
                raise BridgeError(409,'idempotency_conflict','本次请求已提交，请查看原结果')
            return {'task_id':old['task_id'],'duplicate':True}
        if args.get('task_id'):
            linked=db.execute('SELECT 1 FROM card_design_requests WHERE task_id=? AND chat_id=? AND connection_id=?',
                              (args['task_id'],args['chat_id'],conn['id'])).fetchone()
            if not linked:raise BridgeError(403,'wrong_design_task','只能继续这个群的卡片配置对话')
            task=store.task(args['task_id'],conn['id'])
            if task['status'] in ('执行中','等待 Codex'):raise BridgeError(409,'design_busy','Codex 正在处理，请等本轮回复后补充')
    prompt=('卡片配置需求：'+message+'\n\n'
        '[面板配置上下文]\n使用 codex-bridge-workflows Skill，在当前项目处理这个本机面板请求。'
        '用户已授权按上述需求创建或修改卡片配置。仅限群 '+(group['name'] or group['chat_id'])+'（'+group['chat_id']+'）。'
        '这是配置工作，不执行字幕翻译、TTS 或生产流程，不发送飞书消息，不模拟人工审核。'
        '先读取实时 workflow context，优先修改 workflow 的 review 阶段；固定选择项直接配置该阶段 fields，'
        '默认第一项，选项 data 绑定真实音色/素材。不需要用户填写模板 ID、字段 JSON 或按钮回调。'
        '已有素材链接和流程尽量复用；缺少真实素材或有业务歧义时仅问必要问题。'
        '保持未要求改的阶段、群、项目、权限和旧任务规则。用 configure-workflow 先 dry_run 再保存并读回，'
        '不能编造 ID、素材链接或声称已经发送卡片。仅旧版 CardKit 布局变更需要真实发布。'
        '最后用简短中文说明改了什么、默认选择、通过/退回后做什么，以及是否实际保存。'
        '\nCLI：./scripts/configure-workflow --context；写入用 --file JSON文件。')
    payload={'message':prompt}
    if args.get('task_id'):payload['task_id']=args['task_id']
    _,result=store.submit(conn,payload,'card_design_'+key)
    with store.db() as db:
        db.execute('INSERT OR IGNORE INTO card_design_requests VALUES (?,?,?,?,?,?)',
                   (key,args['chat_id'],conn['id'],result['task_id'],message,time.time()))
    return result
