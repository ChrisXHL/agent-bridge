"""Shared group topics and durable, explicit assistant discussion delivery.

Only new opt-in topics are indexed. Feishu bot messages never trigger tasks.
"""
from __future__ import annotations
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from codex_http_bridge import BridgeError
from feishu_settings import CONFIG_LOCK, load

LOG=logging.getLogger('codex_bridge.group_topics')
LIMIT=24


def install(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS group_topics (
      id TEXT PRIMARY KEY,chat_id TEXT NOT NULL,root_message_id TEXT NOT NULL,
      app_ref TEXT NOT NULL,app_id TEXT NOT NULL,connection_id TEXT NOT NULL,
      route TEXT NOT NULL,task_id TEXT,created_at REAL NOT NULL,
      UNIQUE(chat_id,root_message_id));
    CREATE TABLE IF NOT EXISTS group_topic_aliases (
      chat_id TEXT NOT NULL,message_id TEXT NOT NULL,topic_id TEXT NOT NULL,
      PRIMARY KEY(chat_id,message_id));
    CREATE TABLE IF NOT EXISTS group_discussions (
      id TEXT PRIMARY KEY,topic_id TEXT NOT NULL,task_id TEXT NOT NULL,
      actor_id TEXT NOT NULL,event_key TEXT NOT NULL,body TEXT NOT NULL,
      state TEXT NOT NULL DEFAULT 'pending',outbound_id TEXT,attempts INTEGER NOT NULL DEFAULT 0,
      next_attempt REAL NOT NULL DEFAULT 0,created_at REAL NOT NULL,
      UNIQUE(task_id,event_key));
    CREATE TABLE IF NOT EXISTS group_questions (
      id TEXT PRIMARY KEY,topic_id TEXT NOT NULL,task_id TEXT NOT NULL,
      recipient_task TEXT NOT NULL,request_id TEXT NOT NULL,question TEXT NOT NULL,
      answer TEXT,created_at REAL NOT NULL,UNIQUE(task_id,request_id));
    CREATE TABLE IF NOT EXISTS group_question_reads (question_id TEXT PRIMARY KEY);
    ''')


class GroupTopics:
    def __init__(self,collaboration,sender_factory=None):
        self.c=collaboration;self.store=collaboration.store;self.groups=collaboration.groups
        if sender_factory is None:
            from feishu_worker import FeishuSender
            sender_factory=FeishuSender
        self.sender_factory=sender_factory;self.stop=threading.Event();self.thread=None

    def routes(self,chat,receiving=True):
        from feishu_groups import application_paths,group_routes
        for ref,path,_ in application_paths(self.groups.config_path,self.groups.inbox_path):
            cfg=load(path)
            if not cfg.get('enabled') or cfg.get('assistant_archived') or cfg.get('assistant_manager'):continue
            for route in group_routes(cfg):
                if route['chat_id']==chat and route.get('enabled') and (route.get('topic_collaboration') or not receiving):
                    try:
                        if self.store.connection(route['connection_id'])['status']=='已停用':continue
                    except BridgeError:continue
                    yield ref,cfg,route

    def route_for(self,connection,chat):
        match=next((x for x in self.routes(chat) if x[2]['connection_id']==connection),None)
        if not match:raise BridgeError(403,'topic_forbidden','此群尚未启用话题协作，或连接已停用。')
        return match

    def owner_route(self,topic):
        # A topic keeps its original project/rules when the group's defaults change.
        _,path,_=self.groups.resolve(topic['app_ref']);cfg=load(path)
        from feishu_groups import group_routes
        route=next((r for r in group_routes(cfg) if r['chat_id']==topic['chat_id'] and r.get('enabled')),None)
        if not route or not cfg.get('enabled') or cfg.get('assistant_archived') or cfg.get('app_id')!=topic['app_id'] or self.store.connection(topic['connection_id'])['status']=='已停用':
            raise BridgeError(403,'topic_forbidden','话题所属群或应用已停用。')
        return topic['app_ref'],cfg,route

    def activate_dispatcher(self,connection,data):
        """Only the registered creator's explicit @ may connect a new group."""
        from feishu_groups import application_paths,atomic_write,group_routes
        from feishu_probe import check_topic_permissions,chat_name
        from assistant_onboarding import build_prompt
        for ref,path,_ in application_paths(self.groups.config_path,self.groups.inbox_path):
            cfg=load(path)
            if cfg.get('connection_id')!=connection:continue
            role=cfg.get('task_dispatcher') or {};chat=data['chat_id']
            if chat in cfg.get('allowed_chat_ids',[]):return
            if not role:return self.activate_assistant(ref,path,cfg,connection,data)
            if (not cfg.get('enabled') or cfg.get('assistant_archived') or not cfg.get('bot_open_id')
                or data.get('sender_id')!=role.get('owner_open_id') or cfg['bot_open_id'] not in data.get('mentions',[])):
                raise BridgeError(403,'dispatcher_owner_required','请创建者在这个群里 @任务管家发第一条需求。')
            j=self.c.onboarding.read(role['assistant_id']);credentials=load(self.c.onboarding.directory(j['id'])/'credentials.json')
            if (j.get('kind')!='dispatcher' or j.get('phase') not in ('ready','waiting_group') or j.get('app_ref')!=ref
                or j.get('app_id')!=cfg['app_id'] or credentials.get('app_id')!=cfg['app_id']
                or credentials.get('owner_open_id')!=data.get('sender_id')):
                raise BridgeError(403,'dispatcher_owner_required','任务管家的创建者信息还没有核实。')
            check_topic_permissions(cfg['app_id'],cfg['app_secret'])
            routes={r['chat_id']:r for r in group_routes(cfg)}
            title=chat_name(cfg['app_id'],cfg['app_secret'],chat)
            routes[chat]={'chat_id':chat,'name':title or '任务管家 · '+chat[-6:],'chat_title':title,'connection_id':connection,
                          'prompt_template':build_prompt(j,credentials,group=True),'enabled':True,'topic_collaboration':True}
            cfg.update(group_configs=routes,allowed_chat_ids=list(routes));atomic_write(path,cfg)
            self.c.onboarding.update(j['id'],phase='ready',message='已接入群聊，群里 @我说要做什么就行。')
            return

    def activate_assistant(self,ref,path,cfg,connection,data):
        """A business assistant joins a new group when its verified creator @s it there; no pairing code."""
        from feishu_groups import atomic_write,group_routes
        from feishu_probe import check_topic_permissions,chat_name
        from assistant_onboarding import build_prompt
        if (cfg.get('assistant_manager') or not cfg.get('enabled') or cfg.get('assistant_archived')
                or not cfg.get('bot_open_id') or cfg['bot_open_id'] not in data.get('mentions',[])):return
        for job_path in sorted((self.groups.config_path.parent/'assistant-onboarding').glob('asst_*/job.json')):
            j=load(job_path)
            if j.get('app_ref')!=ref or j.get('kind') in ('manager','dispatcher') or not j.get('plan'):continue
            credentials=load(job_path.parent/'credentials.json')
            if credentials.get('app_id')!=cfg['app_id'] or credentials.get('owner_open_id')!=data.get('sender_id'):return
            try:
                check_topic_permissions(cfg['app_id'],cfg['app_secret']);topics=True
            except BridgeError:
                topics=False  # Without group_msg the group still works with an @ on every message.
            chat=data['chat_id'];routes={r['chat_id']:r for r in group_routes(cfg)}
            title=chat_name(cfg['app_id'],cfg['app_secret'],chat)
            routes[chat]={'chat_id':chat,'name':title or j['name']+' · 群 '+chat[-6:],'chat_title':title,'connection_id':connection,
                          'prompt_template':build_prompt(j,credentials,group=True),'enabled':True,'topic_collaboration':topics}
            cfg.update(group_configs=routes,allowed_chat_ids=list(routes));atomic_write(path,cfg)
            if j.get('phase')=='waiting_group':
                self.c.onboarding.update(j['id'],phase='ready',message='已接入群聊，群里 @我说要做什么就行。')
            return

    def resolve(self,connection,data):
        keys={'chat_id','message_id','parent_id','root_id','mentions','sender_id'}
        if not isinstance(data,dict) or set(data)-keys:raise BridgeError(400,'invalid_request','话题消息无效。')
        chat=data.get('chat_id');mid=data.get('message_id');mentions=data.get('mentions',[])
        if not isinstance(chat,str) or not re.fullmatch(r'oc_[\w-]{1,125}',chat) or not isinstance(mid,str) or not re.fullmatch(r'om_[\w-]{1,160}',mid):
            raise BridgeError(400,'invalid_request','话题标识无效。')
        if any(data.get(k) is not None and (not isinstance(data[k],str) or len(data[k])>180) for k in ('parent_id','root_id')) or not isinstance(mentions,list) or len(mentions)>100 or any(not isinstance(m,str) for m in mentions):
            raise BridgeError(400,'invalid_request','话题引用无效。')
        with self.c.onboarding.lock,CONFIG_LOCK,self.store.db() as db:
            self.activate_dispatcher(connection,data)
            db.execute('BEGIN IMMEDIATE')
            ref,cfg,route=self.route_for(connection,chat)
            found=None
            for key in (mid,data.get('parent_id'),data.get('root_id')):
                if key:
                    found=db.execute('SELECT t.* FROM group_topics t JOIN group_topic_aliases a ON a.topic_id=t.id WHERE a.chat_id=? AND a.message_id=?',(chat,key)).fetchone()
                    if found:break
            if found:
                if found['app_ref']!=ref or found['app_id']!=cfg['app_id']:return {'topic':None,'handled_by_other':True}
                self.owner_route(found)
                topic=dict(found)
            else:
                eligible={v.get('bot_open_id'):r['connection_id'] for _,v,r in self.routes(chat) if v.get('bot_open_id')}
                owner=next((eligible[m] for m in mentions if m in eligible),None)
                if owner!=connection:return {'topic':None,'handled_by_other':bool(owner)}
                ident='topic_'+hashlib.sha256((chat+':'+mid).encode()).hexdigest()[:32]
                frozen={k:route[k] for k in ('connection_id','prompt_template','name')}
                frozen.update(mode='prompt',topic_collaboration=True,topic_id=ident)
                db.execute('INSERT OR IGNORE INTO group_topics VALUES (?,?,?,?,?,?,?,NULL,?)',
                           (ident,chat,mid,ref,cfg['app_id'],connection,json.dumps(frozen,ensure_ascii=False),time.time()))
                topic=dict(db.execute('SELECT * FROM group_topics WHERE id=?',(ident,)).fetchone())
            db.execute('INSERT OR IGNORE INTO group_topic_aliases VALUES (?,?,?)',(chat,mid,topic['id']))
            return {'topic':self.public(topic)}

    @staticmethod
    def public(topic):
        return {k:topic[k] for k in ('id','root_message_id','task_id','connection_id')}|{'route':json.loads(topic['route'])}

    def attach(self,connection,data):
        if not isinstance(data,dict) or set(data)!={'topic_id','task_id','message_id'}:raise BridgeError(400,'invalid_request','话题绑定无效。')
        with CONFIG_LOCK,self.store.db() as db:
            row=db.execute('SELECT * FROM group_topics WHERE id=? AND connection_id=?',(data['topic_id'],connection)).fetchone()
            if not row:raise BridgeError(403,'topic_forbidden','不能操作其他话题。')
            self.owner_route(row)
            rid='fs_'+hashlib.sha256((row['app_id']+':'+data['message_id']).encode()).hexdigest()[:48]
            msg=db.execute('SELECT task_id FROM messages WHERE connection_id=? AND request_id=?',(connection,rid)).fetchone()
            alias=db.execute('SELECT 1 FROM group_topic_aliases WHERE chat_id=? AND message_id=? AND topic_id=?',(row['chat_id'],data['message_id'],row['id'])).fetchone()
            if not msg or msg[0]!=data['task_id'] or not alias or (row['task_id'] and row['task_id']!=data['task_id']):
                raise BridgeError(409,'topic_changed','任务与话题不匹配。')
            db.execute('UPDATE group_topics SET task_id=? WHERE id=?',(data['task_id'],row['id']))
        return {'ok':True}

    def get(self,connection,ident):
        with self.store.db() as db:row=db.execute('SELECT * FROM group_topics WHERE id=? AND connection_id=?',(ident,connection)).fetchone()
        if not row:raise BridgeError(403,'topic_forbidden','不能读取其他话题。')
        self.owner_route(row)
        self.reconcile()
        with self.store.db() as db:
            pending=db.execute("SELECT COUNT(*) FROM group_discussions WHERE topic_id=? AND state='pending'",(ident,)).fetchone()[0]
            failed=db.execute("SELECT COUNT(*) FROM group_discussions WHERE topic_id=? AND state='failed'",(ident,)).fetchone()[0]
        return self.public(dict(row))|{'discussion_pending':pending,'discussion_failed':failed}

    def submit(self,connection,data):
        if not isinstance(data,dict) or set(data)!={'topic_id','message_id','message','request_id'}:raise BridgeError(400,'invalid_request','话题任务格式无效。')
        topic=self.get(connection['id'],data['topic_id'])
        with self.store.db() as db:
            row=db.execute('SELECT app_id,chat_id FROM group_topics WHERE id=?',(topic['id'],)).fetchone()
            alias=db.execute('SELECT 1 FROM group_topic_aliases WHERE chat_id=? AND message_id=? AND topic_id=?',(row['chat_id'],data['message_id'],topic['id'])).fetchone()
        expected='fs_'+hashlib.sha256((row['app_id']+':'+data['message_id']).encode()).hexdigest()[:48]
        if not alias or expected!=data['request_id']:raise BridgeError(403,'topic_forbidden','话题来源不匹配。')
        payload={'message':data['message'],'request_id':expected}
        if topic['task_id']:payload['task_id']=topic['task_id']
        status,result=self.store.submit(connection,payload,expected)
        self.attach(connection['id'],{'topic_id':topic['id'],'task_id':result['task_id'],'message_id':data['message_id']})
        return status,result

    def alias(self,connection,data):
        if not isinstance(data,dict) or set(data)!={'topic_id','message_id'} or not isinstance(data['message_id'],str) or not re.fullmatch(r'om_[\w-]{1,160}',data['message_id']):raise BridgeError(400,'invalid_request','回复标识无效。')
        with self.store.db() as db:
            row=db.execute('SELECT * FROM group_topics WHERE id=? AND connection_id=?',(data['topic_id'],connection)).fetchone()
            if not row:raise BridgeError(403,'topic_forbidden','不能修改其他话题。')
            db.execute('INSERT OR IGNORE INTO group_topic_aliases VALUES (?,?,?)',(row['chat_id'],data['message_id'],row['id']))
        return {'ok':True}

    def task_topic(self,task):
        root,_,_=self.c.lineage(task)
        with self.store.db() as db:r=db.execute('SELECT * FROM group_topics WHERE task_id=?',(root,)).fetchone()
        return dict(r) if r else None

    def ensure_topic(self,session):
        topic=self.task_topic(session['task_id'])
        if not topic:raise BridgeError(409,'no_group_topic','此任务不是群话题协作。')
        self.owner_route(topic);return topic

    def emit(self,db,topic,task,actor,key,body):
        old=db.execute('SELECT id,body FROM group_discussions WHERE task_id=? AND event_key=?',(task,key)).fetchone()
        if old:
            if old['body']!=body:raise BridgeError(409,'request_changed','同一编号不能发送不同讨论内容。')
            return old['id']
        if key.startswith(('say:','question:','answer:')):
            count=db.execute("SELECT COUNT(*) FROM group_discussions WHERE task_id=? AND (event_key LIKE 'say:%' OR event_key LIKE 'question:%' OR event_key LIKE 'answer:%')",(task,)).fetchone()[0]
            if count>=LIMIT:raise BridgeError(409,'discussion_limit','此任务的讨论消息已达上限，请汇总后结束。')
        ident='discussion_'+uuid.uuid4().hex
        db.execute('INSERT INTO group_discussions(id,topic_id,task_id,actor_id,event_key,body,created_at) VALUES (?,?,?,?,?,?,?)',(ident,topic['id'],task,actor,key,body,time.time()))
        return ident

    def say(self,session,data):
        if not isinstance(data,dict) or set(data)!={'request_id','text'} or not isinstance(data['request_id'],str) or not 8<=len(data['request_id'])<=100 or not isinstance(data['text'],str) or not 1<=len(data['text'].strip())<=800:
            raise BridgeError(400,'invalid_request','讨论需有唯一编号和 1 到 800 字的内容。')
        topic=self.ensure_topic(session)
        with self.store.db() as db:ident=self.emit(db,topic,session['task_id'],session['assistant_id'],'say:'+data['request_id'],data['text'].strip())
        return {'discussion_id':ident,'delivery':'queued'}

    def ask(self,session,data):
        if not isinstance(data,dict) or set(data)!={'request_id','question'} or not isinstance(data['question'],str) or not 1<=len(data['question'].strip())<=800 or not isinstance(data['request_id'],str) or not 8<=len(data['request_id'])<=100:
            raise BridgeError(400,'invalid_request','问题格式无效。')
        topic=self.ensure_topic(session)
        with self.store.db() as db:
            call=db.execute('SELECT parent_task FROM assistant_calls WHERE child_task=?',(session['task_id'],)).fetchone()
            if not call:raise BridgeError(400,'no_recipient','负责人可直接派任务；只有子助手向委托者提问。')
            old=db.execute('SELECT * FROM group_questions WHERE task_id=? AND request_id=?',(session['task_id'],data['request_id'])).fetchone()
            if old:
                if old['question']!=data['question'].strip():raise BridgeError(409,'request_changed','同一编号的问题不能变化。')
                if old['answer'] is not None:db.execute('INSERT OR IGNORE INTO group_question_reads VALUES (?)',(old['id'],))
                return {'question_id':old['id'],'answer':old['answer']}
            ident='question_'+uuid.uuid4().hex
            self.emit(db,topic,session['task_id'],session['assistant_id'],'question:'+ident,data['question'].strip())
            db.execute('INSERT INTO group_questions VALUES (?,?,?,?,?,?,NULL,?)',(ident,topic['id'],session['task_id'],call[0],data['request_id'],data['question'].strip(),time.time()))
        return {'question_id':ident,'answer':None}

    def questions(self,session):
        with self.store.db() as db:
            return [dict(r) for r in db.execute('SELECT q.id,q.question,c.target_name AS name FROM group_questions q JOIN assistant_calls c ON c.child_task=q.task_id WHERE q.recipient_task=? AND q.answer IS NULL ORDER BY q.created_at',(session['task_id'],))]

    def question(self,session,ident):
        with self.store.db() as db:
            r=db.execute('SELECT id,question,answer FROM group_questions WHERE id=? AND task_id=?',(ident,session['task_id'])).fetchone()
            if not r:raise BridgeError(403,'question_forbidden','不能读取其他助手的问题。')
            if r['answer'] is not None:db.execute('INSERT OR IGNORE INTO group_question_reads VALUES (?)',(ident,))
        return dict(r)

    def answer(self,session,data):
        if not isinstance(data,dict) or set(data)!={'question_id','answer'} or not isinstance(data['answer'],str) or not 1<=len(data['answer'].strip())<=800:raise BridgeError(400,'invalid_request','答复格式无效。')
        topic=self.ensure_topic(session)
        with self.store.db() as db:
            row=db.execute('SELECT * FROM group_questions WHERE id=? AND recipient_task=? AND topic_id=?',(data['question_id'],session['task_id'],topic['id'])).fetchone()
            if not row:raise BridgeError(403,'question_forbidden','此问题没有交给你。')
            text=data['answer'].strip()
            if row['answer'] and row['answer']!=text:raise BridgeError(409,'request_changed','已发送的答复不能改写。')
            self.emit(db,topic,session['task_id'],session['assistant_id'],'answer:'+row['id'],text)
            db.execute('UPDATE group_questions SET answer=? WHERE id=?',(text,row['id']))
            self.queue_answer_resume(db,row['task_id'])
        return {'ok':True}

    def unread_questions(self,db,task):
        return db.execute('SELECT q.id,q.answer FROM group_questions q WHERE q.task_id=? AND NOT EXISTS (SELECT 1 FROM group_question_reads r WHERE r.question_id=q.id) ORDER BY q.id',(task,)).fetchall()

    @staticmethod
    def answer_resume_key(rows):
        return 'question-resume:'+hashlib.sha256(json.dumps([(r['id'],r['answer']) for r in rows],ensure_ascii=False).encode()).hexdigest()

    def pause_for_question(self,task):
        # A completed model turn is not a completed assignment while its
        # clarification is unanswered or its answer has not been read yet.
        with self.store.db() as db:
            rows=self.unread_questions(db,task)
            if not rows:return False
            key=self.answer_resume_key(rows)
            if all(r['answer'] is not None for r in rows) and db.execute('SELECT 1 FROM assistant_resume_messages WHERE resume_key=?',(key,)).fetchone():return False
            db.execute("UPDATE tasks SET status='需要你处理',result='等待委托助手答复；收到答复后自动继续。',error_code='group_question_pending',updated_at=? WHERE id=?",(time.time(),task))
            self.queue_answer_resume(db,task)
        return True

    def queue_answer_resume(self,db,task):
        paused=db.execute("SELECT 1 FROM tasks WHERE id=? AND status='需要你处理' AND error_code='group_question_pending'",(task,)).fetchone()
        rows=self.unread_questions(db,task)
        if not paused or not rows or any(r['answer'] is None for r in rows):return
        text='Bridge 已收到委托助手对追问的答复。请用 question 查询原问题编号 '+', '.join(r['id'] for r in rows)+'，读取真实答复后继续原分工并返回计算或工作结果；不要重新提问、重建委托或仅报告等待状态。'
        self.c.queue_resume(db,task,self.answer_resume_key(rows),text)

    def reconcile(self):
        # Events describe actual durable task state, never fabricated role play.
        with self.store.db() as db:
            rows=[dict(r) for r in db.execute('SELECT c.*,t.status,t.error_code,m.delivery FROM assistant_calls c JOIN tasks t ON t.id=c.child_task JOIN messages m ON m.task_id=t.id ORDER BY c.created_at')]
        for row in rows:
            topic=self.task_topic(row['parent_task'])
            if not topic:continue
            with self.store.db() as db:
                # Routine lifecycle changes stay in the panel. The actual
                # question/result already appears in chat; don't announce it twice.
                if row['status'] in ('失败','已停止','需要你处理') and row['error_code'] not in ('group_question_pending','assistant_authorization_required'):
                    if row['delivery']=='failed' and row['status']=='失败':continue
                    body={'失败':'这一步没做成，具体原因会一起说明。','已停止':'这一步已经停了。','需要你处理':'这里卡住了，还需要补充信息或处理一下。'}[row['status']]
                    key='state:'+row['status']+(':'+str(row['error_code']) if row['status']=='需要你处理' else '')
                    if db.execute('SELECT 1 FROM group_discussions WHERE task_id=? AND event_key=?',(row['child_task'],key)).fetchone():continue
                    self.emit(db,topic,row['child_task'],row['target_id'],key,body)

    def deliver(self):
        with self.store.db() as db:rows=[dict(r) for r in db.execute("SELECT * FROM group_discussions WHERE state='pending' AND next_attempt<=? ORDER BY created_at LIMIT 12",(time.time(),))]
        for event in rows:
            with self.store.db() as db:topic=db.execute('SELECT * FROM group_topics WHERE id=?',(event['topic_id'],)).fetchone()
            if not topic:continue
            try:
                _,owner,_=self.owner_route(topic)
                root,_,_=self.c.lineage(event['task_id'])
                if self.store.task(root)['status']=='已停止':raise BridgeError(409,'parent_inactive','话题任务已停止。')
                with self.store.db() as db:
                    call=db.execute('SELECT c.target_id,s.assistant_id FROM assistant_calls c JOIN assistant_sessions s ON s.task_id=c.parent_task WHERE c.child_task=?',(event['task_id'],)).fetchone()
                if call and call['target_id'] not in self.c.allowed(call['assistant_id'],root):raise BridgeError(403,'target_forbidden','协作授权已取消。')
                actor,cfg,_=self.c.ready(event['actor_id'])
                # Speaking as a helper is allowed only if its bot is also explicitly bound to this group.
                native=owner if cfg['app_id']==owner['app_id'] else next((v for _,v,_ in self.routes(topic['chat_id'],receiving=False) if v['app_id']==cfg['app_id']),None)
                use=native or owner
                label=actor['name']+('' if native else ' · 由负责人转述')
                sender=self.sender_factory(use['app_id'],use['app_secret'])
                outgoing=sender.send_group(topic['chat_id'],event['id'],event['body'] if native else '【'+label+'】\n'+event['body'])
                if not outgoing:raise RuntimeError('missing_receipt')
                with self.store.db() as db:
                    db.execute("UPDATE group_discussions SET state='sent',outbound_id=? WHERE id=?",(outgoing,event['id']))
                    db.execute('INSERT OR IGNORE INTO group_topic_aliases VALUES (?,?,?)',(topic['chat_id'],outgoing,topic['id']))
            except BridgeError:
                with self.store.db() as db:db.execute("UPDATE group_discussions SET state='cancelled' WHERE id=?",(event['id'],))
            except Exception:
                # Stable Feishu UUIDs make retries safe, including a crash after send.
                attempts=event['attempts']+1
                with self.store.db() as db:db.execute("UPDATE group_discussions SET attempts=?,next_attempt=?,state=? WHERE id=?",(attempts,time.time()+min(300,5*2**min(attempts,6)),'failed' if attempts>=5 else 'pending',event['id']))
                LOG.warning('Group discussion delivery pending id=%s',event['id'])

    def start(self):
        def run():
            while not self.stop.is_set():
                try:self.reconcile();self.deliver()
                except Exception:LOG.exception('Group topic reconciliation failed')
                self.stop.wait(3)
        self.thread=threading.Thread(target=run,daemon=True);self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=3)
