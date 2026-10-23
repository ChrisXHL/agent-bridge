"""Task-scoped Feishu consent. Identity is verified against each application's creator."""
from __future__ import annotations
import json
import logging
import secrets
import threading
import time
import uuid
from codex_http_bridge import BridgeError
from feishu_groups import application_paths
from feishu_settings import CONFIG_LOCK,load
from feishu_worker import Inbox,FeishuSender
from assistant_consent import parse as parse_consent

LOG=logging.getLogger('codex_bridge.assistant_authorizations')

class AssistantAuthorizations:
    def __init__(self,collaboration,sender_factory=FeishuSender):
        self.c=collaboration;self.store=collaboration.store;self.sender_factory=sender_factory
        self.stop=threading.Event();self.thread=None

    def owner(self,ident):
        j,cfg,_=self.c.ready(ident)
        credentials=load(self.c.onboarding.directory(ident)/'credentials.json')
        if not credentials.get('owner_open_id') or credentials.get('app_id')!=cfg['app_id']:
            raise BridgeError(409,'owner_unverified','无法核实这位助手的创建者，请在本机设置协作授权。')
        return {'mode':'owner','job_id':ident,'app_ref':j['app_ref'],'app_id':cfg['app_id'],
                'owner_open_id':credentials['owner_open_id'],'name':j['name'],'approved':False}

    def approvers(self,source,targets):
        ids=[source,*targets];jobs=[self.c.ready(i)[0] for i in ids];manager=jobs[0].get('managed_by')
        if manager and all(j.get('managed_by')==manager for j in jobs):
            _,path,_=self.c.groups.resolve(manager['app_ref']);cfg=load(path)
            if cfg.get('enabled') and not cfg.get('assistant_archived') and (cfg.get('assistant_manager') or {}).get('admin_open_id')==manager['admin_open_id']:
                return [{'mode':'manager','jobs':ids,'app_ref':manager['app_ref'],'app_id':cfg['app_id'],
                         'owner_open_id':manager['admin_open_id'],'name':cfg.get('bot_name') or '助手管理员','approved':False}]
        return [self.owner(i) for i in dict.fromkeys(ids)]

    def validate_approver(self,a):
        _,path,_=self.c.groups.resolve(a['app_ref']);cfg=load(path)
        if not cfg.get('enabled') or cfg.get('assistant_archived') or cfg.get('app_id')!=a['app_id']:
            raise BridgeError(409,'owner_changed','确认所用的机器人已停用或身份发生变化。')
        if self.store.connection(cfg['connection_id'])['status']=='已停用':raise BridgeError(409,'owner_changed','确认所用的连接已停用。')
        if a['mode']=='owner':
            now=self.owner(a['job_id'])
            if any(now[k]!=a[k] for k in ('app_ref','app_id','owner_open_id')):raise BridgeError(409,'owner_changed','创建者身份已变化。')
        else:
            if (cfg.get('assistant_manager') or {}).get('admin_open_id')!=a['owner_open_id']:raise BridgeError(403,'owner_changed','管理员身份已变化。')
            for ident in a['jobs']:
                j,_,_=self.c.ready(ident)
                if j.get('managed_by')!={'app_ref':a['app_ref'],'admin_open_id':a['owner_open_id']}:raise BridgeError(403,'owner_changed','助手不再由同一管理员管理。')
        return cfg

    def create(self,db,session,root,batch,targets):
        approvers=self.approvers(session['assistant_id'],targets)
        code=secrets.token_hex(4).upper()
        while db.execute('SELECT 1 FROM assistant_authorizations WHERE code=?',(code,)).fetchone():code=secrets.token_hex(4).upper()
        db.execute('INSERT INTO assistant_authorizations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            ('auth_'+uuid.uuid4().hex,code,root,session['task_id'],batch,session['assistant_id'],json.dumps(targets),json.dumps(approvers,ensure_ascii=False),'pending',time.time()+900,time.time()))

    def finish(self,db,row,status,reason=''):
        if status=='approved':
            for target in json.loads(row['targets']):db.execute('INSERT OR IGNORE INTO assistant_task_grants VALUES (?,?,?)',(row['root_task'],row['source_id'],target))
            db.execute("UPDATE messages SET delivery='queued',updated_at=? WHERE delivery='authorization_pending' AND task_id IN (SELECT child_task FROM assistant_calls WHERE batch_id=?)",(time.time(),row['batch_id']))
            db.execute("UPDATE tasks SET result=NULL,error_code=NULL WHERE error_code='assistant_authorization_required' AND id IN (SELECT child_task FROM assistant_calls WHERE batch_id=?)",(row['batch_id'],))
        else:
            db.execute("UPDATE messages SET delivery='failed',updated_at=? WHERE delivery='authorization_pending' AND task_id IN (SELECT child_task FROM assistant_calls WHERE batch_id=?)",(time.time(),row['batch_id']))
            db.execute("UPDATE tasks SET status='失败',result=?,error_code=?,updated_at=? WHERE id IN (SELECT child_task FROM assistant_calls WHERE batch_id=?) AND status='等待 Codex'",(reason,'assistant_authorization_'+status,time.time(),row['batch_id']))
        db.execute('UPDATE assistant_authorizations SET status=? WHERE id=?',(status,row['id']))
        self.c.queue_authorization_resume(db,row['parent_task'])

    def reconcile(self):
        with CONFIG_LOCK,self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute("SELECT * FROM assistant_authorizations WHERE status='pending'").fetchall()
            for row in rows:
                try:
                    self.c.ensure_active_chain(row['parent_task'])
                    for target in json.loads(row['targets']):self.c.ready(target)
                    for a in json.loads(row['approvers']):self.validate_approver(a)
                except BridgeError as exc:
                    if exc.code=='parent_resuming':continue
                    self.finish(db,row,'cancelled','调用链或助手配置已变化，本次协作授权已取消。');continue
                if row['expires_at']<=time.time():self.finish(db,row,'expired','协作授权已过期，未执行委托。');continue
                if set(json.loads(row['targets']))<=self.c.allowed(row['source_id'],row['root_task']):self.finish(db,row,'approved')

    def confirm(self,ref,item):
        command=parse_consent(item.get('text',''))
        if not command or item.get('chat_type')!='p2p':return '请在机器人私聊中回复“允许本次协作”或“拒绝本次协作”。'
        decision,code=command
        with CONFIG_LOCK,self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if code:
                row=db.execute('SELECT * FROM assistant_authorizations WHERE code=?',(code,)).fetchone()
            else:
                # Bind only to a notice actually sent to this app's verified owner
                # before the original Feishu event. A delayed reply must never
                # approve a request created after the user sent it.
                quoted=item.get('parent_id') or item.get('root_id')
                event_time=json.loads(item.get('route_json') or '{}').get('event_time',item.get('created_at',time.time()))
                rows=db.execute('SELECT * FROM assistant_authorizations'+('' if quoted else " WHERE status='pending'")).fetchall()
                candidates=[]
                for candidate in rows:
                    for a in json.loads(candidate['approvers']):
                        n=a.get('notice',{})
                        if (a['app_ref']!=ref or a['app_id']!=item.get('app_id') or a['owner_open_id']!=item.get('sender_id')
                            or not n.get('sent') or not n.get('outbound_id') or n.get('first_attempt',float('inf'))>event_time):continue
                        if quoted:
                            if quoted==n['outbound_id']:candidates.append(candidate)
                        else:candidates.append(candidate)
                if len(candidates)>1:return '有几件事在等你确认。请回复你要处理的那条协作请求，写“允许本次协作”或“拒绝本次协作”，不用复制编号。'
                row=candidates[0] if candidates else None
            if not row:return '没有找到对应的待确认协作。请回复机器人发来的那条协作请求，不用填写编号。'
            approvers=json.loads(row['approvers'])
            selected=[a for a in approvers if a['app_ref']==ref and a['app_id']==item.get('app_id') and a['owner_open_id']==item.get('sender_id')]
            if not selected:return '只有本次请求指定的创建者可以确认协作。'
            cfg=self.validate_approver(selected[0])
            if item.get('connection_id')!=cfg.get('connection_id'):return '确认来源已变化，请重新检查。'
            if row['status']!='pending':return '本次协作授权已处理：'+{'approved':'已授权','denied':'已拒绝','expired':'已过期','cancelled':'已取消'}.get(row['status'],row['status'])+'。'
            if row['expires_at']<=time.time():self.finish(db,row,'expired','协作授权已过期，未执行委托。');return '本次确认已过期，没有执行委托。'
            try:
                self.c.ensure_active_chain(row['parent_task'])
                for a in approvers:self.validate_approver(a)
            except BridgeError:
                self.finish(db,row,'cancelled','调用链或创建者身份已变化，本次协作授权已取消。');return '任务或创建者身份已变化，本次没有授权。'
            if decision=='deny':self.finish(db,row,'denied','创建者拒绝了本次协作，未执行委托。');return '这次不协作，我会把结果告诉发起的助手。'
            selected[0]['approved']=True
            db.execute('UPDATE assistant_authorizations SET approvers=? WHERE id=?',(json.dumps(approvers,ensure_ascii=False),row['id']))
            if all(a['approved'] for a in approvers):
                self.finish(db,row,'approved');return '可以了，原来的任务会接着做。只允许这次协作。'
            return '收到，还在等其他助手的创建者确认。都确认后，原来的任务会接着做。'

    def notice(self,ident,index):
        # Save UUID/body/attempt before remote send; ambiguous sends are not retried after 10 minutes.
        with CONFIG_LOCK,self.store.db() as db:
            row=db.execute('SELECT * FROM assistant_authorizations WHERE id=?',(ident,)).fetchone()
            if not row or row['status']!='pending':return
            approvers=json.loads(row['approvers']);a=approvers[index];notice=a.get('notice',{})
            if a['approved'] or notice.get('sent') or time.time()-notice.get('last_attempt',0)<15:return
            if notice.get('first_attempt') and time.time()-notice['first_attempt']>600:return
            cfg=self.validate_approver(a)
            source=self.c.onboarding.read(row['source_id'])['name'];targets='、'.join(self.c.onboarding.read(i)['name'] for i in json.loads(row['targets']))
            body=(f'“{source}”想请{targets}一起做这件事。\n会把这次分工需要的内容交给它们，并使用它们已有的能力。只限当前任务，不增加长期授权。\n\n'
                  '允许的话，直接回复“允许本次协作”；不需要就回复“拒绝本次协作”。\n'
                  '同时有几件事时，请回复对应的这条消息。15 分钟内确认后，原任务自动继续。')
            notice.update(first_attempt=notice.get('first_attempt',time.time()),last_attempt=time.time(),body=notice.get('body',body));a['notice']=notice
            db.execute('UPDATE assistant_authorizations SET approvers=? WHERE id=?',(json.dumps(approvers,ensure_ascii=False),ident))
        outbound=self.sender_factory(cfg['app_id'],cfg['app_secret']).send_text(a['owner_open_id'],'authorization:'+ident+':'+str(index),notice['body'])
        if not outbound:raise RuntimeError('missing_message_id')
        with CONFIG_LOCK,self.store.db() as db:
            current=db.execute('SELECT approvers FROM assistant_authorizations WHERE id=?',(ident,)).fetchone()
            fresh=json.loads(current[0]);fresh[index].setdefault('notice',{}).update(sent=True,outbound_id=outbound)
            db.execute('UPDATE assistant_authorizations SET approvers=? WHERE id=?',(json.dumps(fresh,ensure_ascii=False),ident))

    def receive(self,ref,item):
        if json.loads(item.get('route_json') or '{}').get('mode')!='assistant_authorization':return
        with self.store.db() as db:old=db.execute('SELECT * FROM assistant_authorization_receipts WHERE app_ref=? AND message_id=?',(ref,item['message_id'])).fetchone()
        if not old:
            try:result=self.confirm(ref,item)
            except BridgeError:result='当前身份或配置无法确认这次协作，原任务没有获得新增权限。'
            with self.store.db() as db:db.execute("INSERT OR IGNORE INTO assistant_authorization_receipts VALUES (?,?,?,'pending',NULL,NULL)",(ref,item['message_id'],result))
        with self.store.db() as db:
            row=db.execute('SELECT * FROM assistant_authorization_receipts WHERE app_ref=? AND message_id=?',(ref,item['message_id'])).fetchone()
            if row['delivery']=='sent' or time.time()-(row['last_attempt'] or 0)<15 or (row['first_attempt'] and time.time()-row['first_attempt']>600):return
            db.execute("UPDATE assistant_authorization_receipts SET delivery='sending',first_attempt=COALESCE(first_attempt,?),last_attempt=? WHERE app_ref=? AND message_id=?",(time.time(),time.time(),ref,item['message_id']))
        _,path,_=self.c.groups.resolve(ref);cfg=load(path)
        if not cfg.get('enabled') or cfg.get('app_id')!=item.get('app_id'):return
        result=self.sender_factory(cfg['app_id'],cfg['app_secret']).reply(item['message_id'],'assistant-authorization',row['result'])
        if not result:raise RuntimeError('missing_message_id')
        with self.store.db() as db:db.execute("UPDATE assistant_authorization_receipts SET delivery='sent' WHERE app_ref=? AND message_id=?",(ref,item['message_id']))

    def tick(self):
        self.reconcile()
        for ref,path,inbox in application_paths(self.c.groups.config_path,self.c.groups.inbox_path):
            if not load(path).get('enabled') or not inbox.exists():continue
            for item in Inbox(inbox).pending():
                if json.loads(item.get('route_json') or '{}').get('mode')!='assistant_authorization':continue
                try:self.receive(ref,item)
                except Exception:LOG.warning('Collaboration confirmation reply pending')
        with self.store.db() as db:rows=db.execute("SELECT id,approvers FROM assistant_authorizations WHERE status='pending'").fetchall()
        for row in rows:
            for index in range(len(json.loads(row['approvers']))):
                try:self.notice(row['id'],index)
                except Exception:LOG.warning('Collaboration authorization notice pending')

    def start(self):
        def run():
            while not self.stop.is_set():
                try:self.tick()
                except Exception:LOG.warning('Collaboration authorization sync pending')
                self.stop.wait(3)
        self.thread=threading.Thread(target=run,daemon=True);self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=4)
