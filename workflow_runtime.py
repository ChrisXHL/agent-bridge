"""Durable workflow ledger and deterministic transitions on the existing inbox database."""
import copy
import json
import time
import uuid
from pathlib import Path
from card_config import values_for
from codex_http_bridge import BridgeError
from workflow_contract import fingerprint, validate_workflow, review_fields
from workflow_evidence import capture, check_files, document_snapshot

def dumps(v):return json.dumps(v,ensure_ascii=False)
def uid():return uuid.uuid4().hex

class WorkflowRuntime:
    def __init__(self,inbox):
        self.inbox=inbox
        self.folder=Path(inbox.path).parent/'workflow-artifacts'
        with inbox.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS workflow_runs (
                id TEXT PRIMARY KEY,source_id TEXT UNIQUE NOT NULL,task_id TEXT,connection_id TEXT,
                chat_id TEXT NOT NULL,sender_id TEXT NOT NULL,app_id TEXT NOT NULL,
                definition TEXT NOT NULL,version TEXT NOT NULL,forms TEXT NOT NULL,
                step_id TEXT NOT NULL,epoch INTEGER NOT NULL,revision INTEGER NOT NULL,
                state TEXT NOT NULL,artifacts TEXT NOT NULL,decision TEXT,error TEXT,
                created_at REAL NOT NULL,updated_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS workflow_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,epoch INTEGER NOT NULL,
                event TEXT NOT NULL,data TEXT NOT NULL,created_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS workflow_artifacts (
                id TEXT PRIMARY KEY,run_id TEXT NOT NULL,epoch INTEGER NOT NULL,name TEXT NOT NULL,
                evidence TEXT NOT NULL,inputs TEXT NOT NULL,created_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS workflow_reviews (
                id TEXT PRIMARY KEY,run_id TEXT NOT NULL,epoch INTEGER NOT NULL,artifacts TEXT NOT NULL,
                fields TEXT NOT NULL,reviewers TEXT NOT NULL,content TEXT NOT NULL,state TEXT NOT NULL,
                message_id TEXT UNIQUE,decision TEXT,error TEXT,updated_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS workflow_effects (
                id TEXT PRIMARY KEY,run_id TEXT NOT NULL,epoch INTEGER NOT NULL,name TEXT NOT NULL,
                fingerprint TEXT NOT NULL,state TEXT NOT NULL,result TEXT,updated_at REAL NOT NULL,
                UNIQUE(run_id,name,fingerprint));
              CREATE TABLE IF NOT EXISTS workflow_effect_details (
                effect_id TEXT PRIMARY KEY,parameters TEXT NOT NULL,inputs TEXT NOT NULL);
            ''')

    def event(self,db,r,event,data):
        db.execute('INSERT INTO workflow_events(run_id,epoch,event,data,created_at) VALUES (?,?,?,?,?)',
                   (r['id'],r['epoch'],event,dumps(data),time.time()))

    def row(self,db,rid):
        r=db.execute('SELECT * FROM workflow_runs WHERE id=?',(rid,)).fetchone()
        if not r:raise BridgeError(404,'run_not_found','任务账本不存在')
        return dict(r)

    def stage(self,r):
        return next(s for s in json.loads(r['definition'])['steps'] if s['id']==r['step_id'])

    def evidence(self,db,r):
        return {k:json.loads(db.execute('SELECT evidence FROM workflow_artifacts WHERE id=?',(v,)).fetchone()[0])
                for k,v in json.loads(r['artifacts']).items()}

    def choices(self,db,r):
        result={}
        for row in db.execute("SELECT data FROM workflow_events WHERE run_id=? AND event='decision_verified' ORDER BY id",(r['id'],)):
            result.update(json.loads(row[0]).get('form',{}))
        return result

    def conflict_context(self,db,r,form=None):
        from workflow_audio_policy import context
        return context(json.loads(r['definition']),self.evidence(db,r),{**self.choices(db,r),**(form or {})})

    def conflicts(self,db,r,form=None):
        rows=list(db.execute("SELECT id,data FROM workflow_events WHERE run_id=? AND event='constraint_failed' ORDER BY id",(r['id'],)))
        if not rows:return []
        current=self.conflict_context(db,r,form)
        return [dict(id=x['id'],**json.loads(x['data']),applies_to_current_inputs=json.loads(x['data'])['context']==current)
                for x in rows]

    def require_feasible_inputs(self,db,r,form=None):
        if any(x['applies_to_current_inputs'] for x in self.conflicts(db,r,form)):
            raise BridgeError(409,'unresolved_time_conflict','同一文稿、音色和配音策略已有时长冲突。请选择修改，或先显式升级配音策略；重复通过不能解决冲突。')

    def record_conflict(self,db,r,reason):
        if not any(x['applies_to_current_inputs'] for x in self.conflicts(db,r)):
            self.event(db,r,'constraint_failed',{'code':'TIME_CONFLICT','reason':reason,'context':self.conflict_context(db,r)})

    def for_source(self,source):
        original=self.inbox.original_source(source)
        with self.inbox.db() as db:
            r=db.execute('SELECT id FROM workflow_runs WHERE source_id=?',(original['message_id'],)).fetchone()
        return r['id'] if r else None

    def list(self):
        with self.inbox.db() as db:
            return [{k:r[k] for k in ('id','task_id','chat_id','step_id','epoch','revision','state','error','created_at')}
                    | {'name':json.loads(r['definition'])['name'],'stage_name':self.stage(dict(r))['name']}
                    for r in db.execute('SELECT * FROM workflow_runs ORDER BY created_at DESC LIMIT 100')]

    def get(self,rid):
        with self.inbox.db() as db:
            r=self.row(db,rid);out={k:r[k] for k in ('id','source_id','task_id','chat_id','app_id','connection_id','sender_id','step_id','epoch','revision','state','error','version')}
            out.update(definition=json.loads(r['definition']),stage=self.stage(r),artifacts=self.evidence(db,r),
                       decision=json.loads(r['decision'] or 'null'))
            out['history']=[dict(x)|{'data':json.loads(x['data'])} for x in db.execute(
                'SELECT epoch,event,data,created_at FROM workflow_events WHERE run_id=? ORDER BY id',(rid,))]
            out['choices']={}
            for item in out['history']:
                if item['event']=='decision_verified':out['choices'].update(item['data'].get('form',{}))
            out['effects']=[dict(x)|{'result':json.loads(x['result'] or 'null')} for x in db.execute(
                'SELECT id,epoch,name,state,result FROM workflow_effects WHERE run_id=?',(rid,))]
            out['constraints']=self.conflicts(db,r)
            return out

    def start(self,source,definition,config):
        definition=validate_workflow(definition,config.get('cards',[]),config.get('allowed_open_ids',[]))
        rid='run_'+fingerprint([config['app_id'],source['message_id']])[:24]
        forms={c['id']:c for c in config.get('cards',[])}
        with self.inbox.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM workflow_runs WHERE id=?',(rid,)).fetchone():return rid
            t=time.time();first=definition['steps'][0]
            db.execute('''INSERT INTO workflow_runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (rid,source['message_id'],source.get('task_id'),source.get('connection_id'),source['chat_id'],source['sender_id'],config['app_id'],
                 dumps(definition),fingerprint(definition),dumps(forms),first['id'],1,1,'running','{}',None,None,t,t))
            r=self.row(db,rid);self.event(db,r,'started',{'workflow':definition['name'],'source_message_id':source['message_id']})
            db.execute('UPDATE inbound SET prompt=? WHERE message_id=?',(self.prompt(r,source),source['message_id']))
        return rid

    def prompt(self,r,source):
        stage=self.stage(r)
        detail=''
        if stage.get('skills'):
            detail+='\n指定 Skills（执行前阅读对应 SKILL.md；缺失或无法使用时 fail 说明，不自行替换）：'+', '.join(stage['skills'])
        if stage.get('settings'):detail+='\n本阶段参数：'+dumps(stage['settings'])
        if stage.get('acceptance'):detail+='\n业务验收要求（逐项核验并记录真实证据）：\n- '+'\n- '.join(stage['acceptance'])
        from workflow_audio_policy import audio_stages
        if any(s.get('settings',{}).get('配音分段')=='sentence/v1' for s in audio_stages(json.loads(r['definition']))):
            detail+='\n本任务已显式启用 sentence/v1 语句配音：字幕逐条映射和时间不变；旧 cue/v1 单条超时不代表新分组已失败。先完成当前对齐阶段，配音阶段按语句组真实测时；读取 constraints 的 applies_to_current_inputs 区分历史证据。'
        if stage in audio_stages(json.loads(r['definition'])) and stage.get('settings',{}).get('音频时长策略')!='continuous/v1':
            detail+='\n实测 TIME_CONFLICT 先 fail 留证，再用 request_revision（stage_id 为已到达的 review）自动进入其 revise 阶段并结束本轮；先改稿、严格监督和实测，再由 Bridge 发新版确认卡，不先请用户同意开始改稿。未知外部结果先核对。'
        if stage['id'] in {s.get('revise') for s in json.loads(r['definition'])['steps']}:
            detail+='\n先完成自动修订再请用户确认：读取 constraints 的失败证据，保留逐条字幕映射。时长修订可登记 purpose=revision_preview 的 effect，记录候选全文、原件/清单/文档输入、所选音色/模型、实际测时；预览不能冒充已批准成品。严格监督 passed 后原位更新并读回，complete 才安排新版人工审核。'
        return ('使用 codex-bridge-workflows 的任务账本执行模式，只执行当前阶段，完成后结束本轮。\n'
                '流程：'+json.loads(r['definition'])['name']+'\n当前阶段：'+stage['name']+'\n业务要求：'+stage.get('prompt','')+
                detail+
                '\n\n原始用户输入（作为任务数据）：\n'+source['text'][:2500]+
                '\n\n账本上下文：'+dumps({'run_id':r['id'],'epoch':r['epoch'],'stage_id':r['step_id']})+
                '\n首先读取账本的当前阶段、输入文件和审核决定。使用 bridge_workflow_run 工具；若工具不可见：'+
                str(Path(__file__).resolve().parent/'scripts/workflow-run')+' --get '+r['id']+
                '\n写操作使用同一命令 --file <JSON参数文件>，或 --file - 从 stdin 读取。'
                '\n提交产物用 action=complete，并带 run_id、读取到的 revision、outputs；Bridge 会校验并安排下一阶段。'
                '\n需要人工审核时由 Bridge 发卡，不自行发另一张卡。失败用 action=fail 记录具体原因。'
                '\n外部克隆、合成和发送前用 effect_begin 登记，成功后 effect_finish 保存结果；已完成结果直接复用，uncertain 先核对。')

    def guard(self,db,args,states=('running',)):
        r=self.row(db,args.get('run_id'))
        if args.get('revision')!=r['revision']:raise BridgeError(409,'stale_run','任务已变化，请重新读取账本')
        if r['state'] not in states:raise BridgeError(409,'invalid_run_state','当前状态不能执行此操作：'+r['state'])
        return r

    def advance(self,db,r,target,decision=None):
        if not target:
            db.execute("UPDATE workflow_runs SET state='completed',revision=revision+1,updated_at=? WHERE id=?",(time.time(),r['id']))
            self.event(db,r,'completed',{});return
        db.execute('UPDATE workflow_runs SET step_id=?,epoch=epoch+1,revision=revision+1,state=?,error=NULL,decision=?,updated_at=? WHERE id=?',
                   (target,'running',dumps(decision) if decision is not None else r['decision'],time.time(),r['id']))
        r=self.row(db,r['id']);stage=self.stage(r);self.event(db,r,'stage_entered',{'name':stage['name'],'stage_id':stage['id']})
        if stage['kind']=='review':self.create_review(db,r)
        else:
            source=dict(db.execute('SELECT * FROM inbound WHERE message_id=?',(r['source_id'],)).fetchone())
            mid='wf_'+r['id']+'_'+str(r['epoch'])
            prompt=self.prompt(r,source);t=time.time()
            db.execute('''INSERT OR IGNORE INTO inbound(message_id,chat_id,sender_id,text,prompt,parent_id,connection_id,app_id,route_json,chat_type,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (mid,r['chat_id'],r['sender_id'],stage['name'],prompt,r['source_id'],r['connection_id'],r['app_id'],source['route_json'],source['chat_type'],t,t))

    def create_review(self,db,r):
        stage=self.stage(r);current=json.loads(r['artifacts']);needed=stage['needs']
        if not set(needed)<=set(current):raise BridgeError(409,'missing_inputs','缺少审核产物')
        fields=review_fields(stage,list(json.loads(r['forms']).values()))
        # The generic renderer supports fixed choices. Other inputs require a dedicated template.
        if any(s['type']!='choice' for s in fields.values()):raise BridgeError(400,'unsupported_review_form','通用审核卡当前支持固定选项；其他字段请使用专用卡片')
        ev=self.evidence(db,r);rid='review_'+uid();elements=[]
        links=[]
        for key in needed:
            if ev[key].get('url'):links.append(f"[{key}]({ev[key]['url']})")
        description='请审核本轮结果。需要修改时选择“提交修改意见”；确认当前版本可以继续时选择“确认通过”。\n'+ '\n'.join(links)
        blocked=any(x['applies_to_current_inputs'] for x in self.conflicts(db,r))
        if blocked:
            description='当前版本有已实测的配音时长冲突，原样通过不能继续。请选择“提交修改意见”进入修订，或让 Codex 执行已授权的配音策略升级。\n'+ '\n'.join(links)
        elements.append({'tag':'markdown','content':description})
        selects=[]
        for name,spec in fields.items():
            options=[]
            for x in spec['options']:
                label=x['data'].get('label',x['data'].get('voice',x['value'])) if isinstance(x['data'],dict) else str(x['data'])
                if label=='example_voice':label='示例音色'
                options.append({'text':{'tag':'plain_text','content':str(label)},'value':x['value']})
            selects.append({'tag':'select_static','name':name,'required':spec.get('required',False),'initial_index':1,
                            'placeholder':{'tag':'plain_text','content':'请选择 '+name},'options':options})
        buttons=[] if blocked else [('approve','确认通过，继续','primary_filled')]
        if stage.get('revise'):buttons.append(('revise','提交修改意见','primary_filled' if blocked else 'default'))
        buttons.append(('cancel','取消任务','default'))
        selects.extend({'tag':'button','name':'workflow_'+action,'text':{'tag':'plain_text','content':label},
                        'type':typ,'form_action_type':'submit'} for action,label,typ in buttons)
        elements.append({'tag':'form','name':'workflow_decision','elements':selects})
        card={'schema':'2.0','header':{'title':{'tag':'plain_text','content':stage['name']},'subtitle':{'tag':'plain_text','content':'第 '+str(r['epoch'])+' 轮 · '+json.loads(r['definition'])['name']},'template':'blue'},'body':{'elements':elements}}
        reviewers=[r['sender_id'] if x=='initiator' else x for x in stage.get('reviewers',['initiator'])]
        db.execute('INSERT INTO workflow_reviews VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (rid,r['id'],r['epoch'],dumps({k:current[k] for k in needed}),dumps(fields),dumps(reviewers),dumps(card),'queued',None,None,None,time.time(),) )
        db.execute("UPDATE workflow_runs SET state='waiting_review' WHERE id=?",(r['id'],))
        self.event(db,r,'review_requested',{'review_id':rid,'artifacts':{k:current[k] for k in needed}})

    def act(self,args,*,snapshotter=document_snapshot,receipt_reader=None):
        if not isinstance(args,dict):raise BridgeError(400,'invalid_run_action','参数需要对象')
        action=args.get('action')
        if action=='use_continuous_audio':
            from workflow_continuous_audio import resume
            return resume(self,args,snapshotter)
        if action not in ('complete','fail','retry','cancel','reopen_review','request_revision','upgrade_audio_grouping','effect_begin','effect_finish'):
            raise BridgeError(400,'invalid_run_action','未知的账本操作')
        with self.inbox.db() as db:
            r=self.guard(db,args,('running','blocked','waiting_review') if action in ('retry','cancel','reopen_review','request_revision','upgrade_audio_grouping') else ('running',))
            evidence=self.evidence(db,r);stage=self.stage(r)
            latest_review=db.execute('SELECT * FROM workflow_reviews WHERE run_id=? AND epoch=?',(r['id'],r['epoch'])).fetchone()
            if action in ('complete','effect_begin') and stage['id'] not in {s.get('revise') for s in json.loads(r['definition'])['steps']}:
                self.require_feasible_inputs(db,r)
            if action=='request_revision':
                from workflow_audio_policy import audio_stages
                if r['state']!='blocked' or stage not in audio_stages(json.loads(r['definition'])) or not any(x['applies_to_current_inputs'] for x in self.conflicts(db,r)):
                    raise BridgeError(409,'revision_requires_time_conflict','自动改稿只适用于已记录实测时长冲突的阻断配音阶段')
                if not isinstance(args.get('reason'),str) or not 1<=len(args['reason'])<=1500:
                    raise BridgeError(400,'invalid_reason','记录需要自动修订的具体失败证据')
            if action in ('reopen_review','request_revision'):
                stage=next((s for s in json.loads(r['definition'])['steps'] if s['id']==args.get('stage_id') and s['kind']=='review'),None)
                seen=[json.loads(x[0]).get('stage_id') for x in db.execute("SELECT data FROM workflow_events WHERE run_id=? AND event='stage_entered'",(r['id'],))]
                if not stage or stage['id'] not in seen:raise BridgeError(409,'invalid_review_target','只能返回本任务已经到达过的审核阶段')
                if db.execute("SELECT 1 FROM workflow_effects WHERE run_id=? AND state IN ('started','uncertain')",(r['id'],)).fetchone():
                    raise BridgeError(409,'effect_unconfirmed','先核对尚未确认的外部操作，再返回审核')
                if action=='request_revision':
                    lookup={s['id']:s for s in json.loads(r['definition'])['steps']}
                    target=lookup.get(stage.get('revise'));sid=stage.get('next');downstream=[]
                    while sid:
                        downstream.append(sid);sid=lookup[sid].get('next')
                    if not target or target['kind']!='agent' or target.get('next')!=stage['id'] or r['step_id'] not in downstream:
                        raise BridgeError(409,'invalid_revision_target','需要当前配音上游审核的 revise 阶段，且修订后必须返回该审核')
                    # A revision attempt is an agent transition, never a fabricated human decision.
                    attempts=db.execute("SELECT COUNT(*) FROM workflow_events WHERE run_id=? AND event='revision_requested'",(r['id'],)).fetchone()[0]
                    if attempts>=3:raise BridgeError(409,'revision_limit','自动改稿已达三轮，保留失败证据并等待人工处理')
        refreshed={}
        if (action=='retry' and stage['kind']=='review') or action in ('reopen_review','request_revision'):
            if latest_review and not latest_review['message_id']:
                raise BridgeError(409,'delivery_uncertain','审核卡发送结果未确认，不能重发；先在飞书核对并关联真实消息')
            check_files(evidence)
            refreshed={k:capture({'kind':'feishu_document'},{'url':evidence[k]['url']},self.folder/r['id']/uid(),evidence,snapshotter=snapshotter)
                       for k in stage['needs'] if evidence[k]['kind']=='feishu_document'}
        if action=='complete':
            outputs=args.get('outputs')
            if stage['kind']!='agent' or not isinstance(outputs,dict) or set(outputs)!=set(stage.get('produces',{})):
                raise BridgeError(400,'invalid_outputs','需提交当前阶段声明的全部产物')
            check_files(evidence)
            for name,e in evidence.items():
                if name in stage.get('needs',[]) and name not in outputs and e['kind']=='feishu_document':
                    if fingerprint(snapshotter(e['url']))!=e['content_hash']:raise BridgeError(409,'approved_version_changed','已审核文档或评论变化，须返回审核')
            from workflow_contract import output_order
            captured={}
            for k in output_order(stage['produces'], stage.get('needs',[])):
                try:
                    captured[k]=capture(stage['produces'][k],outputs[k],self.folder/r['id']/uid(),
                        {**evidence,**captured},snapshotter=snapshotter,receipt_reader=receipt_reader)
                except BridgeError as exc:
                    if 'TIME_CONFLICT' in str(exc):
                        with self.inbox.db() as db:
                            db.execute('BEGIN IMMEDIATE');latest=self.guard(db,args)
                            error=str(exc)[:1500]
                            self.record_conflict(db,latest,error)
                            db.execute("UPDATE workflow_runs SET state='blocked',error=?,revision=revision+1,updated_at=? WHERE id=?",(error,time.time(),r['id']))
                            self.event(db,latest,'blocked',{'reason':error})
                    raise
            from workflow_alignment import verify_document_bindings
            verify_document_bindings(captured,evidence)
            with self.inbox.db() as db:
                db.execute('BEGIN IMMEDIATE');r=self.guard(db,args)
                if db.execute("SELECT 1 FROM workflow_effects WHERE run_id=? AND state IN ('started','uncertain')",(r['id'],)).fetchone():
                    raise BridgeError(409,'effect_unconfirmed','外部操作结果尚未确认，不能完成阶段')
                from workflow_alignment import verify_audio_effects
                verify_audio_effects(captured,evidence,stage,list(db.execute(
                    "SELECT e.*,d.parameters FROM workflow_effects e JOIN workflow_effect_details d ON d.effect_id=e.id WHERE e.run_id=? AND e.state='succeeded'",(r['id'],))))
                current=json.loads(r['artifacts'])
                for k,e in captured.items():
                    aid='artifact_'+uid();db.execute('INSERT INTO workflow_artifacts VALUES (?,?,?,?,?,?,?)',
                        (aid,r['id'],r['epoch'],k,dumps(e),r['artifacts'],time.time()));current[k]=aid
                db.execute('UPDATE workflow_runs SET artifacts=? WHERE id=?',(dumps(current),r['id']))
                self.event(db,r,'outputs_verified',{'outputs':{k:current[k] for k in captured}})
                r=self.row(db,r['id']);self.advance(db,r,stage.get('next'))
        elif action=='effect_begin':
            key=args.get('key');inputs=args.get('inputs',[]);params=args.get('parameters',{})
            if not isinstance(key,str) or not 1<=len(key)<=100 or not isinstance(inputs,list) or not set(inputs)<=set(evidence) or not isinstance(params,dict):
                raise BridgeError(400,'invalid_effect','外部操作需名称、有效输入和参数')
            check_files({k:evidence[k] for k in inputs})
            for k in stage.get('needs',[]):
                if evidence[k]['kind']=='feishu_document' and fingerprint(snapshotter(evidence[k]['url']))!=evidence[k]['content_hash']:
                    raise BridgeError(409,'approved_version_changed','文档审核版本已变化，停止外部操作')
            fp=fingerprint([{k:evidence[k]['sha256'] for k in inputs},params]);eid='effect_'+fingerprint([r['id'],key,fp])[:32]
            with self.inbox.db() as db:
                db.execute('BEGIN IMMEDIATE');r=self.guard(db,args)
                old=db.execute('SELECT * FROM workflow_effects WHERE id=?',(eid,)).fetchone()
                if old:
                    return {'effect_id':eid,'state':old['state'],'result':json.loads(old['result'] or 'null'),
                            'instruction':'复用已完成结果；started/uncertain 必须先核对，不能再次执行','run_id':r['id'],'revision':r['revision']}
                db.execute('INSERT INTO workflow_effects VALUES (?,?,?,?,?,?,?,?)',(eid,r['id'],r['epoch'],key,fp,'started',None,time.time(),))
                db.execute('INSERT INTO workflow_effect_details VALUES (?,?,?)',
                           (eid,dumps(params),dumps({k:evidence[k]['sha256'] for k in inputs})))
                self.event(db,r,'effect_started',{'effect_id':eid,'name':key})
            return {'effect_id':eid,'state':'claimed','revision':r['revision'],'instruction':'仅本次 claimed 可执行外部操作；完成后 effect_finish 登记结果'}
        else:
            with self.inbox.db() as db:
                db.execute('BEGIN IMMEDIATE');r=self.guard(db,args,('running','blocked','waiting_review') if action in ('retry','cancel','reopen_review','request_revision','upgrade_audio_grouping') else ('running',))
                if action=='effect_finish':
                    effect=db.execute('SELECT * FROM workflow_effects WHERE id=? AND run_id=?',(args.get('effect_id'),r['id'])).fetchone()
                    if not effect or effect['state']=='succeeded':raise BridgeError(409,'effect_state','外部操作不存在或已完成')
                    state=args.get('state');result=args.get('result')
                    if state not in ('succeeded','uncertain') or not isinstance(result,dict) or not result:raise BridgeError(400,'invalid_effect_result','需记录真实外部回执；未确认时标为 uncertain')
                    db.execute('UPDATE workflow_effects SET state=?,result=?,updated_at=? WHERE id=?',(state,dumps(result),time.time(),effect['id']))
                    self.event(db,r,'effect_'+state,{'effect_id':effect['id'],'result':result})
                elif action=='cancel':
                    db.execute("UPDATE workflow_runs SET state='cancelled',revision=revision+1,updated_at=? WHERE id=?",(time.time(),r['id']))
                    self.event(db,r,'cancelled',{'note':'停止后续阶段；已经发生的外部操作不会自动撤销'})
                elif action=='fail':
                    error=args.get('reason')
                    if not isinstance(error,str) or not 1<=len(error)<=1500:raise BridgeError(400,'invalid_reason','请说明具体失败原因')
                    db.execute("UPDATE workflow_runs SET state='blocked',error=?,revision=revision+1,updated_at=? WHERE id=?",(error,time.time(),r['id']))
                    self.event(db,r,'blocked',{'reason':error})
                    if 'TIME_CONFLICT' in error:self.record_conflict(db,r,error)
                elif action=='upgrade_audio_grouping':
                    if r['state']!='blocked' or stage['kind']!='agent':
                        raise BridgeError(409,'upgrade_requires_blocked','先停止当前执行阶段，再升级配音契约')
                    reason=args.get('reason')
                    if not isinstance(reason,str) or not 1<=len(reason)<=1500:
                        raise BridgeError(400,'invalid_reason','记录用户明确授权的配音分组变更')
                    if db.execute("SELECT 1 FROM workflow_effects WHERE run_id=? AND state IN ('started','uncertain')",(r['id'],)).fetchone():
                        raise BridgeError(409,'effect_unconfirmed','先核对尚未确认的外部操作')
                    from workflow_audio_policy import upgrade,audio_stages
                    definition=json.loads(r['definition']);speech=audio_stages(definition)
                    # Only before synthesis/delivery: do not turn completed delivery into a new job.
                    allowed={s['id'] for s in definition['steps'] if any(p['kind']=='subtitle_alignment' for p in s.get('produces',{}).values())}|{s['id'] for s in speech}
                    if stage['id'] not in allowed:raise BridgeError(409,'invalid_upgrade_stage','只能在对齐或配音阶段升级')
                    if 'TIME_CONFLICT' in (r['error'] or ''):self.record_conflict(db,r,r['error'])
                    updated=upgrade(definition)
                    validate_workflow(updated,list(json.loads(r['forms']).values()),[r['sender_id']])
                    current=json.loads(r['artifacts']);lookup={s['id']:s for s in updated['steps']};sid=speech[0]['id']
                    while sid:
                        for key in lookup[sid].get('produces',{}):current.pop(key,None)
                        sid=lookup[sid].get('next')
                    version=fingerprint(updated)
                    db.execute('UPDATE workflow_runs SET definition=?,version=?,artifacts=?,revision=revision+1,updated_at=? WHERE id=?',
                               (dumps(updated),version,dumps(current),time.time(),r['id']))
                    self.event(db,r,'audio_policy_upgraded',{'from_version':r['version'],'to_version':version,
                        'from_grouping':'cue/v1','to_grouping':'sentence/v1','reason':reason,'previous_definition':definition})
                elif action in ('retry','reopen_review','request_revision'):
                    if action=='retry' and r['state']!='blocked':raise BridgeError(409,'not_blocked','仅阻断的阶段可以恢复')
                    if action=='request_revision' and db.execute("SELECT 1 FROM workflow_effects WHERE run_id=? AND state IN ('started','uncertain')",(r['id'],)).fetchone():
                        raise BridgeError(409,'effect_unconfirmed','文档读取期间出现未确认的外部操作，停止自动修订')
                    current=json.loads(r['artifacts'])
                    if action in ('reopen_review','request_revision'):
                        if 'TIME_CONFLICT' in (r['error'] or ''):self.record_conflict(db,r,r['error'])
                        lookup={s['id']:s for s in json.loads(r['definition'])['steps']};sid=stage.get('next')
                        while sid:
                            for key in lookup[sid].get('produces',{}):current.pop(key,None)
                            sid=lookup[sid].get('next')
                    for k,e in refreshed.items():
                        aid='artifact_'+uid();db.execute('INSERT INTO workflow_artifacts VALUES (?,?,?,?,?,?,?)',(aid,r['id'],r['epoch'],k,dumps(e),r['artifacts'],time.time()));current[k]=aid
                    db.execute('UPDATE workflow_runs SET artifacts=? WHERE id=?',(dumps(current),r['id']))
                    # An uncertain external effect remains uncertain across retries and is never replayed.
                    if action=='request_revision':
                        self.event(db,r,'revision_requested',{'review_stage_id':stage['id'],'stage_id':stage['revise'],
                            'reason':args['reason'],'trigger':'TIME_CONFLICT','attempt':attempts+1,'requires_review_after_revision':True})
                        self.advance(db,self.row(db,r['id']),stage['revise'])
                    else:
                        self.event(db,r,'retry_requested' if action=='retry' else 'review_reopened',{'stage_id':stage['id']})
                        self.advance(db,self.row(db,r['id']),stage['id'])
        return self.get(r['id'])

    def accept_callback(self,config,payload):
        event=payload.get('event') or {};ctx=event.get('context') or {};action=event.get('action') or {}
        operator=(event.get('operator') or {}).get('open_id')
        with self.inbox.db() as db:
            db.execute('BEGIN IMMEDIATE')
            review=db.execute('SELECT * FROM workflow_reviews WHERE message_id=?',(ctx.get('open_message_id'),)).fetchone()
            if not review:return None
            r=self.row(db,review['run_id'])
            if (r['app_id']!=config['app_id'] or r['chat_id']!=ctx.get('open_chat_id') or operator not in config.get('allowed_open_ids',[])
                or operator not in json.loads(review['reviewers'])):return 'ignored'
            source=db.execute('SELECT chat_type FROM inbound WHERE message_id=?',(r['source_id'],)).fetchone()
            if source['chat_type']=='group' and r['chat_id'] not in config.get('allowed_chat_ids',[]):return 'ignored'
            if review['state'] in ('submitted','verifying'):return 'already_submitted'
            if review['state']!='sent' or r['state']!='waiting_review' or r['epoch']!=review['epoch']:return 'stale_review'
            decision={'workflow_approve':'approve','workflow_revise':'revise','workflow_cancel':'cancel'}.get(action.get('name'))
            if action.get('tag')!='button' or not decision:return 'invalid_form'
            if decision=='revise' and not self.stage(r).get('revise'):return 'invalid_form'
            try:form={} if decision=='cancel' else values_for(json.loads(review['fields']),action.get('form_value') or {})
            except BridgeError:return 'invalid_form'
            if decision=='approve':
                try:self.require_feasible_inputs(db,r,form)
                except BridgeError:return 'unresolved_time_conflict'
            data={'action':decision,'operator':operator,'form':form,'artifacts':json.loads(review['artifacts']),
                  'review_id':review['id'],'at':time.time()}
            db.execute("UPDATE workflow_reviews SET state='verifying',decision=?,updated_at=? WHERE id=?",(dumps(data),time.time(),review['id']))
            db.execute("UPDATE workflow_runs SET state='checking_review',revision=revision+1 WHERE id=?",(r['id'],))
            self.event(db,r,'decision_received',data)
        return 'accepted'

    def pump(self,config,sender,*,snapshotter=document_snapshot):
        # Callback ack is immediate; network verification is performed by the worker afterwards.
        with self.inbox.db() as db:
            rows=[dict(x) for x in db.execute("SELECT * FROM workflow_reviews WHERE state IN ('queued','verifying','sending','blocked')")]
        for review in rows:
            with self.inbox.db() as db:r=self.row(db,review['run_id']);ev=self.evidence(db,r)
            if r['app_id']!=config['app_id'] or r['sender_id'] not in config.get('allowed_open_ids',[]):continue
            source=self.inbox.cards.source(r['source_id'])
            if source.get('chat_type')=='group' and r['chat_id'] not in config.get('allowed_chat_ids',[]):continue
            if review['state']=='blocked':
                try:
                    sender.reply(r['source_id'],'review_blocked:'+review['id'],'审核暂未继续：'+review['error']+'。请让 Codex 查看当前任务账本并处理。')
                    with self.inbox.db() as db:db.execute("UPDATE workflow_reviews SET state='blocked_notified' WHERE id=? AND state='blocked'",(review['id'],))
                except Exception:pass
                continue
            if review['state']=='sending':
                if time.time()-review['updated_at']>120:self.block_review(review,'卡片发送中断，先在飞书核对，不能自动重发')
                continue
            if r['epoch']!=review['epoch'] or r['state']=='cancelled':continue
            if review['state']=='queued':
                with self.inbox.db() as db:
                    if not db.execute("UPDATE workflow_reviews SET state='sending',updated_at=? WHERE id=? AND state='queued'",(time.time(),review['id'])).rowcount:continue
                try:
                    mid=sender.reply_card(r['source_id'],review['content'],review['id'])
                    if not mid:raise ValueError('missing message id')
                    with self.inbox.db() as db:db.execute("UPDATE workflow_reviews SET state='sent',message_id=?,updated_at=? WHERE id=?",(mid,time.time(),review['id']))
                except Exception:self.block_review(review,'卡片发送结果未确认，请在飞书核对')
                continue
            decision=json.loads(review['decision']);stage=self.stage(r)
            try:
                check_files(ev)
                captured={}
                for key in json.loads(review['artifacts']):
                    a=ev[key]
                    if a['kind']=='feishu_document' and decision['action']!='cancel':
                        fresh=snapshotter(a['url'])
                        if decision['action']=='approve':
                            if fingerprint(fresh)!=a['content_hash']:raise BridgeError(409,'review_changed','文档或评论已变化，请重新发起审核或提交修改')
                        else:
                            captured[key]=capture({'kind':'feishu_document'},{'url':a['url']},self.folder/r['id']/uid(),ev,snapshotter=lambda _:fresh)
                with self.inbox.db() as db:
                    db.execute('BEGIN IMMEDIATE');latest=self.row(db,r['id'])
                    if latest['epoch']!=r['epoch'] or latest['state']!='checking_review':continue
                    if decision['action']=='approve':self.require_feasible_inputs(db,latest,decision.get('form'))
                    db.execute("UPDATE workflow_reviews SET state='submitted',updated_at=? WHERE id=?",(time.time(),review['id']))
                    self.event(db,r,'decision_verified',decision)
                    if decision['action']=='cancel':
                        db.execute("UPDATE workflow_runs SET state='cancelled',revision=revision+1 WHERE id=?",(r['id'],));continue
                    current=json.loads(r['artifacts'])
                    for key,a in captured.items():
                        aid='artifact_'+uid();db.execute('INSERT INTO workflow_artifacts VALUES (?,?,?,?,?,?,?)',(aid,r['id'],r['epoch'],key,dumps(a),r['artifacts'],time.time()));current[key]=aid
                    if decision['action']=='revise':
                        # Remove downstream outputs; retain only inputs needed to perform the revision.
                        steps=json.loads(r['definition'])['steps'];lookup={s['id']:s for s in steps};target=stage['revise'];sid=lookup[target].get('next');keep=set(lookup[target].get('needs',[]))
                        seen=set()
                        while sid and sid not in seen:
                            seen.add(sid)
                            for key in lookup[sid].get('produces',{}):
                                if key not in keep:current.pop(key,None)
                            sid=lookup[sid].get('next')
                    db.execute('UPDATE workflow_runs SET artifacts=? WHERE id=?',(dumps(current),r['id']))
                    self.advance(db,self.row(db,r['id']),stage['revise'] if decision['action']=='revise' else stage.get('next'),decision)
            except Exception as exc:
                reason=str(exc) if isinstance(exc,BridgeError) else '审核版本读取失败，请核对权限与网络后恢复'
                self.block_review(review,reason)

    def block_review(self,review,reason):
        with self.inbox.db() as db:
            r=self.row(db,review['run_id'])
            db.execute("UPDATE workflow_reviews SET state='blocked',error=?,updated_at=? WHERE id=?",(reason,time.time(),review['id']))
            if r['epoch']==review['epoch'] and r['state']!='cancelled':
                db.execute("UPDATE workflow_runs SET state='blocked',error=?,revision=revision+1 WHERE id=?",(reason,r['id']))
                self.event(db,r,'review_blocked',{'reason':reason})

    def bind_task(self,db,message_id,task_id):
        db.execute('UPDATE workflow_runs SET task_id=? WHERE source_id=? AND task_id IS NULL',(task_id,message_id))

    def dispatch_allowed(self,item):
        if not item['message_id'].startswith('wf_run_'):return True
        rid=self.for_source(item)
        if not rid:return False
        with self.inbox.db() as db:r=self.row(db,rid)
        return item['message_id']=='wf_'+rid+'_'+str(r['epoch']) and r['state']=='running'
