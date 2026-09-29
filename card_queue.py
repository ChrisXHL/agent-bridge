"""Persistent generic card delivery and one-shot callback intake."""
import hashlib
import json
import time
from codex_http_bridge import BridgeError
from card_config import card_content, callback_prompt, values_for
from phase_prompts import review_prompt

class CardQueue:
    def __init__(self,inbox):
        self.inbox=inbox
        with inbox.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS outgoing_cards (
                id TEXT PRIMARY KEY, source_message_id TEXT NOT NULL, app_id TEXT NOT NULL,
                task_id TEXT NOT NULL, connection_id TEXT, chat_id TEXT NOT NULL,sender_id TEXT NOT NULL,
                card_type TEXT NOT NULL, snapshot TEXT NOT NULL, variables TEXT NOT NULL,
                fingerprint TEXT NOT NULL,state TEXT NOT NULL, message_id TEXT UNIQUE,
                selected_action TEXT,error TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL)''')
    def enqueue(self, config, source, card_type, variables, request_id):
        if not isinstance(request_id,str) or not 8<=len(request_id)<=100 or not all(c.isalnum() or c in '-_.' for c in request_id):
            raise BridgeError(400,'invalid_request_id','request_id 需为 8 到 100 位字母、数字或 -_.')
        if not source.get('task_id'): raise BridgeError(409,'task_not_ready','原消息尚未关联 Codex 任务')
        if source.get('app_id') and source['app_id']!=config['app_id']: raise BridgeError(409,'app_changed','原任务的飞书应用已切换')
        policy=json.loads(source.get('route_json') or '{}')
        if source.get('chat_type')=='group' and source['chat_id'] not in config.get('allowed_chat_ids',[]):
            raise BridgeError(403,'chat_removed','原群已不在允许名单')
        allowed=policy.get('card_types',config.get('default_card_types',['voice_review']))
        if card_type not in allowed: raise BridgeError(403,'card_not_allowed','此会话流程未启用该卡片')
        cid=hashlib.sha256((config['app_id']+':'+source['message_id']+':'+request_id).encode()).hexdigest()[:40]
        fingerprint=hashlib.sha256(json.dumps([card_type,variables],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        # A retried request always returns its original frozen configuration.
        with self.inbox.db() as db:
            old=db.execute('SELECT * FROM outgoing_cards WHERE id=?',(cid,)).fetchone()
        if old:
            if old['fingerprint']!=fingerprint: raise BridgeError(409,'idempotency_conflict','同一 request_id 已用于不同卡片内容')
            return self.public(dict(old))
        from card_config import default_cards
        card=next((c for c in config.get('cards',default_cards()) if c['id']==card_type),None)
        if not card: raise BridgeError(404,'card_not_found','未找到卡片配置')
        values_for(card['variables'],variables)
        with self.inbox.db() as db:
            db.execute('''INSERT OR IGNORE INTO outgoing_cards
              (id,source_message_id,app_id,task_id,connection_id,chat_id,sender_id,card_type,snapshot,
               variables,fingerprint,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'queued',?,?)''',
              (cid,source['message_id'],config['app_id'],source['task_id'],source.get('connection_id') or config.get('connection_id'),
               source['chat_id'],source['sender_id'],card_type,json.dumps(card,ensure_ascii=False),
               json.dumps(variables,ensure_ascii=False),fingerprint,time.time(),time.time()))
            row=dict(db.execute('SELECT * FROM outgoing_cards WHERE id=?',(cid,)).fetchone())
            if row['fingerprint']!=fingerprint: raise BridgeError(409,'idempotency_conflict','request_id 冲突')
        return self.public(row)
    @staticmethod
    def public(row):
        return {k:row.get(k) for k in ['id','source_message_id','task_id','card_type','state','message_id','selected_action','error','created_at']}
    def list(self):
        with self.inbox.db() as db:
            return [self.public(dict(r)) for r in db.execute('SELECT * FROM outgoing_cards ORDER BY created_at DESC LIMIT 100')]
    def source(self,message_id):
        with self.inbox.db() as db:
            r=db.execute('SELECT * FROM inbound WHERE message_id=?',(message_id,)).fetchone()
        if not r: raise BridgeError(404,'source_not_found','未找到这条飞书来源消息')
        return dict(r)
    def has_source(self,message_id):
        with self.inbox.db() as db:
            return bool(db.execute('SELECT 1 FROM outgoing_cards WHERE source_message_id=?',(message_id,)).fetchone())
    def is_callback(self,message_id):
        with self.inbox.db() as db:
            return bool(db.execute('SELECT 1 FROM outgoing_cards WHERE message_id=?',(message_id,)).fetchone())
    def deliver(self,config,sender):
        with self.inbox.db() as db:
            rows=[dict(r) for r in db.execute("SELECT * FROM outgoing_cards WHERE state='queued'")]
        for r in rows:
            if r['app_id']!=config['app_id']: continue
            with self.inbox.db() as db:
                claimed=db.execute("UPDATE outgoing_cards SET state='sending',updated_at=? WHERE id=? AND state='queued'",(time.time(),r['id'])).rowcount
            if not claimed: continue
            try:
                message_id=sender.reply_card(r['source_message_id'],card_content(json.loads(r['snapshot']),json.loads(r['variables'])),r['id'])
                with self.inbox.db() as db:
                    db.execute("UPDATE outgoing_cards SET state='sent',message_id=?,updated_at=? WHERE id=?",(message_id,time.time(),r['id']))
            except Exception:
                with self.inbox.db() as db:
                    db.execute("UPDATE outgoing_cards SET state='delivery_uncertain',error=?,updated_at=? WHERE id=?",('发送结果未确认，请在飞书核对；系统不会自动重复发送',time.time(),r['id']))
                import logging;logging.getLogger(__name__).exception('Card delivery failed id=%s',r['id'])
        # A crash during the network call must not cause a duplicate external send.
        with self.inbox.db() as db:
            db.execute("UPDATE outgoing_cards SET state='delivery_uncertain',error='发送中断，请在飞书核对',updated_at=? WHERE state='sending' AND updated_at<?",(time.time(),time.time()-120))
    def accept(self,config,payload):
        event=payload.get('event') or {};action=event.get('action') or {};ctx=event.get('context') or {}
        operator=(event.get('operator') or {}).get('open_id');message_id=ctx.get('open_message_id')
        if action.get('tag')!='button' or operator not in config.get('allowed_open_ids',[]): return None
        with self.inbox.db() as db:
            db.execute('BEGIN IMMEDIATE')
            r=db.execute('SELECT * FROM outgoing_cards WHERE message_id=?',(message_id,)).fetchone()
            if not r: return None
            if r['app_id']!=config['app_id'] or r['sender_id']!=operator or r['chat_id']!=ctx.get('open_chat_id'): return 'ignored'
            if r['state'] in ('pending','submitted'): return 'already_submitted'
            if r['state']!='sent': return 'ignored'
            try:
                prompt=callback_prompt(json.loads(r['snapshot']),action.get('name'),json.loads(r['variables']),action.get('form_value') or {})
                source=self.source(r['source_message_id'])
                prompt=review_prompt(config,source,self.inbox.original_source(source),prompt,
                                     'card_approved' if action.get('name')=='approve_voice_material' else 'card_action:'+str(action.get('name')))
            except BridgeError: return 'invalid_form'
            origin=db.execute('SELECT route_json,chat_type FROM inbound WHERE message_id=?',(r['source_message_id'],)).fetchone()
            if origin and origin['chat_type']=='group' and r['chat_id'] not in config.get('allowed_chat_ids',[]): return 'ignored'
            db.execute('''INSERT OR IGNORE INTO inbound
                (message_id,chat_id,sender_id,text,prompt,parent_id,connection_id,app_id,route_json,chat_type,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (message_id,r['chat_id'],operator,prompt,prompt,r['source_message_id'],r['connection_id'],r['app_id'],origin['route_json'] if origin else None,origin['chat_type'] if origin else None,time.time(),time.time()))
            db.execute("UPDATE outgoing_cards SET state='pending',selected_action=?,updated_at=? WHERE id=?",(action['name'],time.time(),r['id']))
        return 'accepted'
    def submitted(self,message_id):
        with self.inbox.db() as db:
            db.execute("UPDATE outgoing_cards SET state='submitted',updated_at=? WHERE message_id=? AND state='pending'",(time.time(),message_id))
