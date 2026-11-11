"""Real loopback ingress and dispatch; Feishu delivery and Codex are isolated fixtures."""
import hashlib
import json
import subprocess
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import Mock,patch
from urllib.error import HTTPError
import test_feishu as events
import test_assistant_collaboration as collaboration_fixtures
from feishu_groups import atomic_write,revision
from feishu_worker import Inbox,FeishuAdapter,BridgeClient,FeishuSender
from feishu_probe import check_topic_permissions
from service import make_handler
from codex_http_bridge import BridgeError


class Sender:
 def __init__(self,app,sent):self.app=app;self.sent=sent
 def reply_topic(self,mid,key,body):
  outbound='om_'+hashlib.sha256((self.app+mid+key).encode()).hexdigest()[:24]
  self.sent.append({'app':self.app,'root':mid,'key':key,'body':body,'id':outbound});return outbound
 def send_group(self,chat,key,body):
  outbound='om_'+hashlib.sha256((self.app+chat+key).encode()).hexdigest()[:24]
  self.sent.append({'app':self.app,'chat':chat,'key':key,'body':body,'id':outbound,'mode':'group'});return outbound
 def reply(self,*args):return self.reply_topic(*args)
 def download_resource(self,mid,key,kind,out,limit):out.write(b'real fixture attachment')


class TopicTests(unittest.TestCase):
 def setUp(self):
  self.f=collaboration_fixtures.CollaborationTests();self.f.setUp();self.o=self.f.o;self.store=self.f.store;self.engine=self.f.engine;self.fake=self.f.fake
  self.fake.complete(self.store.task(self.f.parent)['thread_id'],'private done');self.engine.tick()
  self.configs={};self.adapters={};self.sent=[]
  for i,j in enumerate(self.f.jobs):
   _,p,inbox=self.o.groups.resolve(j['app_ref']);cfg=json.loads(p.read_text())
   cfg.update(bot_open_id='ou_bot_'+str(i),allowed_chat_ids=['oc_shared','oc_other'],group_configs={chat:{'chat_id':chat,'name':'话题测试群','connection_id':cfg['connection_id'],'prompt_template':'群用途 {{message}}','enabled':True,'topic_collaboration':True} for chat in ('oc_shared','oc_other')})
   atomic_write(p,cfg);self.configs[j['id']]=cfg
  self.server=ThreadingHTTPServer(('127.0.0.1',0),None);self.base='http://127.0.0.1:'+str(self.server.server_port)
  self.server.RequestHandlerClass=make_handler(self.store,self.engine,self.base,feishu_settings_path=self.o.groups.config_path,feishu_inbox_path=self.o.groups.inbox_path)
  self.c=self.server.RequestHandlerClass.collaboration;self.c.topics.sender_factory=lambda app,secret:Sender(app,self.sent)
  self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
  for j in self.f.jobs:
   cfg=self.configs[j['id']];_,_,inbox=self.o.groups.resolve(j['app_ref']);key=self.store.connection(cfg['connection_id'],reveal=True)['api_key']
   client=BridgeClient(self.base,key);self.adapters[j['id']]=FeishuAdapter(cfg,Inbox(inbox),client,Sender(cfg['app_id'],self.sent))
  self.main=self.adapters[self.f.source['id']];self.cfg=self.configs[self.f.source['id']]
  self.main.pump_once();self.sent.clear()
 def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.f.tearDown()
 def event(self,mid='om_group',sender='ou_member',parent=None,chat='oc_shared',mention=True):
  return events.event(mid,sender=sender,chat=chat,chat_type='group',parent=parent,text='@bot 请安排资料整理' if mention else '补充一个要求',mentions=[{'id':{'open_id':self.cfg['bot_open_id']},'key':'@bot','name':'主助手'}] if mention else [])
 def start(self,mid='om_group'):
  self.assertTrue(self.main.handle_event(self.event(mid)));self.main.pump_once();self.engine.tick()
  row=self.main.inbox.linked_source(mid);return row['task_id'],json.loads(row['route_json'])['topic_id']
 def session(self,tid):
  context=json.loads((self.c.root/(tid+'.json')).read_text());return self.c.authenticate('Bearer '+context['token']),context
 def test_multimember_followup_without_mention_keeps_topic_thread_and_frozen_rules(self):
  task,topic=self.start();thread=self.store.task(task)['thread_id']
  self.assertIn('群话题协作',self.fake.messages[thread][0])
  self.assertTrue(self.main.handle_event(self.event('om_follow',sender='ou_another',parent='om_group',mention=False)))
  self.main.pump_once();self.assertIsNone(self.main.inbox.linked_source('om_follow')['task_id'])
  self.fake.complete(thread,'阶段结果');self.engine.tick();self.main.pump_once();self.engine.tick()
  self.assertEqual(self.main.inbox.linked_source('om_follow')['task_id'],task)
  self.assertEqual(self.store.task(task)['thread_id'],thread)
  self.assertIn('群用途',self.fake.messages[thread][-1]);self.assertTrue(all(x.get('chat')=='oc_shared' and x.get('mode')=='group' for x in self.sent))
 def test_no_ambient_chat_no_other_group_no_bot_generated_tasks(self):
  self.start()
  for event in (self.event('om_noise',mention=False),self.event('om_cross',parent='om_group',chat='oc_other',mention=False)):
   self.assertFalse(self.main.handle_event(event))
  bot=self.event('om_loop',sender=self.cfg['app_id'],parent='om_group');bot['event']['sender']['sender_type']='app';self.assertFalse(self.main.handle_event(bot))

 def test_other_bot_mention_is_polled_once_into_the_existing_group(self):
  self.assertIsNone(self.main.inbox.bot_message_cursor('oc_shared'))
  created=str(int(time.time()*1000)+1000)
  mention=SimpleNamespace(id=self.cfg['bot_open_id'],key='@bot',name='主助手')
  message=SimpleNamespace(create_time=created,message_id='om_other_bot',msg_type='text',
      sender=SimpleNamespace(sender_type='app',id='cli_other'),mentions=[mention],
      body=SimpleNamespace(content=json.dumps({'text':'@bot 请核查资料'})),parent_id=None,root_id=None)
  response=SimpleNamespace(success=lambda:True,data=SimpleNamespace(items=[message],has_more=False))
  self.main.sender.list_group_messages=lambda *args:response
  self.main.poll_bot_messages();self.main.pump_once();self.engine.tick()
  self.assertIsNotNone(self.main.inbox.linked_source('om_other_bot')['task_id'])
  count=len(self.store.tasks())
  self.main.next_bot_poll=0;self.main.poll_bot_messages();self.main.pump_once()
  self.assertEqual(len(self.store.tasks()),count)
 def dispatcher(self):
  from assistant_onboarding import DISPATCHER_PLAN
  j=self.f.source;owner=json.loads((self.o.directory(j['id'])/'credentials.json').read_text())['owner_open_id']
  self.o.update(j['id'],kind='dispatcher',plan=dict(DISPATCHER_PLAN))
  self.cfg['task_dispatcher']={'assistant_id':j['id'],'owner_open_id':owner};atomic_write(self.o.groups.resolve(j['app_ref'])[1],self.cfg)
  return owner
 def test_dispatcher_creator_first_mention_connects_group_and_delivers_same_message(self):
  owner=self.dispatcher();old=json.loads(self.o.groups.resolve(self.f.source['app_ref'])[1].read_text());self.o.update(self.f.source['id'],phase='waiting_group')
  with patch('feishu_probe.check_topic_permissions',return_value={'verified':True}),patch('feishu_probe.chat_name',return_value='新测试群'):
   self.assertTrue(self.main.handle_event(self.event('om_auto_group',sender=owner,chat='oc_new')))
  self.main.pump_once();self.engine.tick();row=self.main.inbox.linked_source('om_auto_group')
  self.assertEqual(self.store.task(row['task_id'])['status'],'执行中');self.assertEqual(self.o.read(self.f.source['id'])['phase'],'ready')
  fresh=json.loads(self.o.groups.resolve(self.f.source['app_ref'])[1].read_text())
  self.assertTrue(fresh['group_configs']['oc_new']['topic_collaboration']);self.assertIn('openchat',fresh['group_configs']['oc_new']['prompt_template'])
  for key in ('app_id','app_secret','allowed_open_ids','prompt_template'):self.assertEqual(fresh[key],old[key])
  self.assertEqual(fresh['group_configs']['oc_shared'],old['group_configs']['oc_shared'])
 def test_creator_invitation_connects_business_assistant_without_pairing(self):
  owner=json.loads((self.o.directory(self.f.source['id'])/'credentials.json').read_text())['owner_open_id']
  path=self.o.groups.resolve(self.f.source['app_ref'])[1]
  self.cfg['auto_group_owner_open_id']=owner;atomic_write(path,self.cfg)
  before=path.read_bytes()
  event={'header':{'app_id':self.cfg['app_id']},'event':{'chat_id':'oc_new','operator_id':{'open_id':owner}}}
  with patch('feishu_probe.check_topic_permissions',return_value={'verified':True}),patch('feishu_probe.chat_name',return_value='数据分析群'):
   self.assertTrue(self.main.handle_group_added(event))
   self.assertTrue(self.main.handle_group_added(event))
  updated=json.loads(path.read_text())
  self.assertEqual(updated['group_configs']['oc_new']['name'],'数据分析群')
  self.assertIn('openchat',updated['group_configs']['oc_new']['prompt_template'])
  self.assertEqual(updated['group_configs']['oc_shared'],json.loads(before)['group_configs']['oc_shared'])
  self.assertTrue(self.main.handle_event(self.event('om_after_invite',chat='oc_new')))
  self.main.pump_once();self.engine.tick()
  self.assertIsNotNone(self.main.inbox.linked_source('om_after_invite')['task_id'])
 def test_other_member_invitation_does_not_connect_group(self):
  path=self.o.groups.resolve(self.f.source['app_ref'])[1];before=path.read_bytes()
  event={'header':{'app_id':self.cfg['app_id']},'event':{'chat_id':'oc_new','operator_id':{'open_id':'ou_other'}}}
  self.assertFalse(self.main.handle_group_added(event));self.assertEqual(path.read_bytes(),before)
 def test_creator_first_mention_fallback_without_group_message_scope(self):
  owner=json.loads((self.o.directory(self.f.source['id'])/'credentials.json').read_text())['owner_open_id']
  self.cfg['auto_group_owner_open_id']=owner;atomic_write(self.o.groups.resolve(self.f.source['app_ref'])[1],self.cfg)
  with patch('feishu_probe.check_topic_permissions',side_effect=BridgeError(409,'missing','missing')),patch('feishu_probe.chat_name',return_value=None):
   self.assertTrue(self.main.handle_event(self.event('om_first_mention',sender=owner,chat='oc_new')))
  self.main.pump_once();self.engine.tick()
  row=self.main.inbox.linked_source('om_first_mention')
  self.assertIsNotNone(row['task_id'])
  self.assertFalse(json.loads(self.o.groups.resolve(self.f.source['app_ref'])[1].read_text())['group_configs']['oc_new']['topic_collaboration'])
 def test_dispatcher_does_not_connect_group_for_other_member_or_ambient_message(self):
  owner=self.dispatcher();path=self.o.groups.resolve(self.f.source['app_ref'])[1];before=path.read_bytes()
  self.assertFalse(self.main.handle_event(self.event('om_other_owner',chat='oc_new')))
  self.assertFalse(self.main.handle_event(self.event('om_ambient_owner',sender=owner,chat='oc_new',mention=False)))
  with self.assertRaises(HTTPError) as exc:self.main.bridge.topic_resolve({'chat_id':'oc_new','message_id':'om_forged','mentions':[self.cfg['bot_open_id']],'sender_id':'ou_other'})
  self.assertEqual(exc.exception.code,403);self.assertEqual(before,path.read_bytes())
 def test_dispatcher_discovers_only_group_helpers_and_rejects_private_target(self):
  self.c.configure_policy({'mode':'open'})
  self.dispatcher();task,_=self.start();session,_=self.session(task)
  a=self.configs[self.f.a['id']];a['group_configs']['oc_shared']['topic_collaboration']=False;atomic_write(self.o.groups.resolve(self.f.a['app_ref'])[1],a)
  b=self.configs[self.f.b['id']];b['allowed_chat_ids']=[];atomic_write(self.o.groups.resolve(self.f.b['app_ref'])[1],b)
  peers=self.c.peers_for(self.f.source['id'],task)
  self.assertEqual([p['assistant_id'] for p in peers],[self.f.a['id']])
  with self.assertRaises(BridgeError) as exc:self.c.call(session,{'request_id':'dispatcher-private','requests':[{'assistant_id':self.f.b['id'],'message':'不要跨群调用'}]})
  self.assertEqual(exc.exception.code,'target_not_in_group')
 def test_open_group_collaboration_needs_no_individual_grants(self):
  self.dispatcher();task,_=self.start();session,_=self.session(task)
  self.c.configure(self.f.source['id'],{'targets':[],'revision':self.c.settings(self.f.source['id'])['revision']})
  self.c.configure_policy({'mode':'open'})
  result=self.c.call(session,{'request_id':'open-group-call','requests':[{'assistant_id':self.f.a['id'],'message':'只整理测试短文'}]})
  self.assertFalse(result['waiting_authorization'])
  with self.store.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM assistant_authorizations').fetchone()[0],0)
 def test_temporary_topic_failure_keeps_receipt_and_recovers_once(self):
  with patch.object(self.main.bridge,'topic_resolve',side_effect=OSError('temporarily unavailable')):
   self.assertTrue(self.main.handle_event(self.event()))
  self.assertEqual(len(self.main.inbox.deferred_topic_events()),1)
  self.main.pump_once();self.assertEqual(self.main.inbox.deferred_topic_events(),[])
  task=self.main.inbox.linked_source('om_group')['task_id'];self.main.pump_once()
  with self.store.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM messages WHERE task_id=?',(task,)).fetchone()[0],1)
 def test_saved_group_rule_changes_do_not_rewrite_existing_topic(self):
  task,_=self.start();cfg=self.cfg;cfg['group_configs']['oc_shared']['prompt_template']='新规则 {{message}}';atomic_write(self.o.groups.resolve(self.f.source['app_ref'])[1],cfg)
  self.assertTrue(self.main.handle_event(self.event('om_frozen',parent='om_group',mention=False)))
  self.assertIn('群用途',self.main.inbox.linked_source('om_frozen')['prompt'])
  self.assertNotIn('新规则',self.main.inbox.linked_source('om_frozen')['prompt'])
 def test_one_owner_when_multiple_bots_receive_same_group_event(self):
  event=self.event();self.assertTrue(self.main.handle_event(event))
  self.assertFalse(self.adapters[self.f.a['id']].handle_event(event))
  self.main.pump_once()
  with self.store.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM group_topics').fetchone()[0],1)
 def test_different_roots_make_separate_tasks_and_connection_cannot_read_foreign_topic(self):
  one,topic=self.start();two,_=self.start('om_group_2');self.assertNotEqual(one,two)
  with self.assertRaises(HTTPError) as error:self.adapters[self.f.a['id']].bridge.topic(topic)
  self.assertEqual(error.exception.code,403)
 def test_discussions_questions_answers_and_native_helper_identity(self):
  task,topic=self.start();session,_=self.session(task)
  # A helper can speak as itself without subscribing to unmentioned group traffic.
  cfg=self.configs[self.f.a['id']];cfg['group_configs']['oc_shared']['topic_collaboration']=False;atomic_write(self.o.groups.resolve(self.f.a['app_ref'])[1],cfg)
  out=self.c.call(session,{'request_id':'topic-delegate-1','requests':[{'assistant_id':self.f.a['id'],'message':'核实资料'}]})
  self.engine.tick();child=self.c.result(session,out['batch_id'])['results'][0]['task_id'];child_session,_=self.session(child)
  self.c.topics.say(child_session,{'request_id':'discussion-1','text':'我先核对材料来源。'})
  q=self.c.topics.ask(child_session,{'request_id':'question-1','question':'这次只整理中文资料吗？'})
  self.assertEqual(self.c.result(session,out['batch_id'])['questions'][0]['id'],q['question_id'])
  self.c.topics.answer(session,{'question_id':q['question_id'],'answer':'是，只整理中文资料。'})
  self.assertEqual(self.c.topics.question(child_session,q['question_id'])['answer'],'是，只整理中文资料。')
  self.c.topics.reconcile();self.c.topics.deliver();count=len(self.sent);self.c.topics.reconcile();self.c.topics.deliver();self.assertEqual(len(self.sent),count)
  helper=[x for x in self.sent if x['app']==self.configs[self.f.a['id']]['app_id']];self.assertTrue(helper);self.assertTrue(all(x.get('chat')=='oc_shared' and x.get('mode')=='group' for x in self.sent))
  with self.assertRaises(BridgeError):self.c.topics.answer(child_session,{'question_id':q['question_id'],'answer':'越权改写'})
  with self.assertRaises(BridgeError):self.c.topics.say(child_session,{'request_id':'discussion-1','text':'同编号不同内容'})
 def test_wait_returns_question_immediately_and_cli_can_answer(self):
  task,_=self.start();session,context=self.session(task)
  out=self.c.call(session,{'request_id':'topic-delegate-1','requests':[{'assistant_id':self.f.a['id'],'message':'核实资料'}]});self.engine.tick()
  child=self.c.result(session,out['batch_id'])['results'][0]['task_id'];s,_=self.session(child);q=self.c.topics.ask(s,{'request_id':'question-1','question':'需要多少字？'})
  cmd=[sys.executable,str(Path(__file__).parent/'scripts/assistant-call.py'),'--context',str(self.c.root/(task+'.json'))]
  start=time.monotonic();r=subprocess.run(cmd+['wait','--batch',out['batch_id'],'--seconds','60'],capture_output=True,text=True,timeout=5)
  self.assertEqual(r.returncode,0,r.stderr);self.assertLess(time.monotonic()-start,5);self.assertTrue(json.loads(r.stdout)['questions'])
  r=subprocess.run(cmd+['answer','--id',q['question_id'],'--text','三百字'],capture_output=True,text=True,timeout=5);self.assertEqual(r.returncode,0,r.stderr)
  self.assertNotIn(context['token'],r.stdout)
 def test_late_answer_resumes_paused_child_once_in_same_chat(self):
  task,_=self.start();session,_=self.session(task)
  batch=self.c.call(session,{'request_id':'topic-delegate-1','requests':[{'assistant_id':self.f.a['id'],'message':'核实资料'}]})['batch_id'];self.engine.tick()
  child=self.c.result(session,batch)['results'][0]['task_id'];child_session,_=self.session(child);thread=self.store.task(child)['thread_id']
  q=self.c.topics.ask(child_session,{'request_id':'question-1','question':'范围仍为 1 至 10 吗？'})
  self.fake.complete(thread,'还未收到答复，等待。');self.engine.tick()
  self.assertEqual(self.store.task(child)['error_code'],'group_question_pending')
  self.assertTrue(self.c.result(session,batch)['pending']);self.assertFalse(self.c.all_collected(task))
  # Reconstruct the coordinator as on restart; the pending question is durable.
  from group_topics import GroupTopics
  self.c.topics=GroupTopics(self.c)
  self.c.topics.answer(session,{'question_id':q['question_id'],'answer':'仍为 1 至 10'})
  self.c.topics.answer(session,{'question_id':q['question_id'],'answer':'仍为 1 至 10'})
  with self.store.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM assistant_resume_messages WHERE task_id=?',(child,)).fetchone()[0],1)
  self.engine.tick();self.assertEqual(self.store.task(child)['thread_id'],thread);self.assertEqual(self.store.task(child)['status'],'执行中')
  self.assertIn(q['question_id'],self.fake.messages[thread][-1]);self.assertIn('assistant-call.py',self.fake.messages[thread][-1])
  self.assertEqual(self.c.topics.question(child_session,q['question_id'])['answer'],'仍为 1 至 10')
  self.fake.complete(thread,'165');self.engine.tick();out=self.c.result(session,batch)
  self.assertFalse(out['pending']);self.assertEqual(out['results'][0]['result'],'165')
  self.fake.complete(self.store.task(task)['thread_id'],'核对结果 165');self.engine.tick()
  self.assertEqual(self.store.task(task)['status'],'已完成')
 def test_answer_race_only_resumes_when_answer_was_not_read(self):
  task,_=self.start();session,_=self.session(task)
  batch=self.c.call(session,{'request_id':'topic-delegate-1','requests':[{'assistant_id':self.f.a['id'],'message':'核实资料'},{'assistant_id':self.f.b['id'],'message':'独立核实'}]})['batch_id'];self.engine.tick();self.engine.tick()
  children=self.c.result(session,batch)['results']
  for i,row in enumerate(children):
   child=row['task_id'];s,_=self.session(child);q=self.c.topics.ask(s,{'request_id':'question-1','question':'用中文吗？'})
   self.c.topics.answer(session,{'question_id':q['question_id'],'answer':'是'})
   if i==0:self.c.topics.question(s,q['question_id'])
   self.fake.complete(self.store.task(child)['thread_id'],'收到' if i==0 else '还没读到答复')
  self.engine.tick()
  self.assertEqual(self.store.task(children[0]['task_id'])['status'],'已完成')
  self.assertEqual(self.store.task(children[1]['task_id'])['status'],'执行中')
  with self.store.db() as db:
   self.assertEqual(db.execute('SELECT COUNT(*) FROM assistant_resume_messages WHERE task_id=?',(children[0]['task_id'],)).fetchone()[0],0)
   self.assertEqual(db.execute('SELECT COUNT(*) FROM assistant_resume_messages WHERE task_id=?',(children[1]['task_id'],)).fetchone()[0],1)
 def test_stopping_parent_blocks_queued_answer_resume(self):
  task,_=self.start();session,_=self.session(task)
  batch=self.c.call(session,{'request_id':'topic-delegate-1','requests':[{'assistant_id':self.f.a['id'],'message':'核实资料'}]})['batch_id'];self.engine.tick()
  child=self.c.result(session,batch)['results'][0]['task_id'];s,_=self.session(child)
  q=self.c.topics.ask(s,{'request_id':'question-1','question':'用中文吗？'})
  self.fake.complete(self.store.task(child)['thread_id'],'等待');self.engine.tick()
  self.c.topics.answer(session,{'question_id':q['question_id'],'answer':'是'});self.store.set_task(task,'已停止');self.engine.tick()
  self.assertEqual(self.store.task(child)['status'],'已停止')
  self.assertEqual(len(self.fake.messages[self.store.task(child)['thread_id']]),1)
 def test_waiting_for_answers_does_not_occupy_child_slots(self):
  task,_=self.start();session,_=self.session(task)
  batch=self.c.call(session,{'request_id':'topic-delegate-1','requests':[{'assistant_id':self.f.a['id'],'message':'核实资料'},{'assistant_id':self.f.b['id'],'message':'独立核实'}]})['batch_id'];self.engine.tick();self.engine.tick()
  for row in self.c.result(session,batch)['results']:
   s,_=self.session(row['task_id']);self.c.topics.ask(s,{'request_id':'question-1','question':'用中文吗？'})
   self.fake.complete(self.store.task(row['task_id'])['thread_id'],'等待答复')
  self.engine.tick()
  later=self.c.call(session,{'request_id':'topic-delegate-2','requests':[{'assistant_id':self.f.a['id'],'message':'另一项无需澄清的核实'}]})['batch_id'];self.engine.tick()
  self.assertEqual(self.c.result(session,later)['results'][0]['status'],'执行中')
 def test_unbound_helper_is_transparently_relayed_by_owner(self):
  task,_=self.start();session,_=self.session(task)
  cfg=self.configs[self.f.a['id']];cfg['allowed_chat_ids']=[];atomic_write(self.o.groups.resolve(self.f.a['app_ref'])[1],cfg)
  out=self.c.call(session,{'request_id':'topic-delegate-1','requests':[{'assistant_id':self.f.a['id'],'message':'核实资料'}]});self.engine.tick()
  child=self.c.result(session,out['batch_id'])['results'][0]['task_id'];s,_=self.session(child)
  self.c.topics.say(s,{'request_id':'discussion-1','text':'已核实来源。'});self.c.topics.deliver()
  self.assertEqual(self.sent[-1]['app'],self.cfg['app_id']);self.assertIn('由负责人转述',self.sent[-1]['body'])
 def test_reply_to_helper_discussion_continues_owner_topic(self):
  task,_=self.start();session,_=self.session(task)
  self.c.topics.say(session,{'request_id':'discussion-1','text':'开始分工。'});self.c.topics.deliver();mid=self.sent[-1]['id']
  self.assertTrue(self.main.handle_event(self.event('om_reply_to_discussion',sender='ou_other',parent=mid,mention=False)))
  route=json.loads(self.main.inbox.linked_source('om_reply_to_discussion')['route_json']);self.assertIn('topic_id',route)
 def test_disabled_group_prevents_pending_discussion_delivery(self):
  task,_=self.start();session,_=self.session(task);self.c.topics.say(session,{'request_id':'discussion-1','text':'开始核查。'})
  cfg=self.cfg;cfg['group_configs']['oc_shared']['enabled']=False;atomic_write(self.o.groups.resolve(self.f.source['app_ref'])[1],cfg)
  self.c.topics.deliver();self.assertEqual([x for x in self.sent if x['key']!='received:om_group'],[])
 def test_stopped_task_cannot_publish_queued_discussion(self):
  task,_=self.start();s,_=self.session(task);self.c.topics.say(s,{'request_id':'discussion-1','text':'已收到任务。'})
  self.store.set_task(task,'已停止');self.c.topics.deliver();self.assertEqual([x for x in self.sent if x['key']!='received:om_group'],[])
 def test_turning_topic_default_off_does_not_lose_existing_result(self):
  task,topic=self.start();cfg=self.cfg;cfg['group_configs']['oc_shared']['topic_collaboration']=False;atomic_write(self.o.groups.resolve(self.f.source['app_ref'])[1],cfg)
  self.assertEqual(self.main.bridge.topic(topic)['task_id'],task)
  self.fake.complete(self.store.task(task)['thread_id'],'已完成');self.engine.tick();self.main.pump_once()
  self.assertTrue(self.sent);self.assertEqual(self.sent[-1]['chat'],'oc_shared')
  self.assertEqual(self.sent[-1]['body'],'已完成');self.assertNotIn(task,self.sent[-1]['body'])
 def test_delivery_retry_keeps_event_identity_and_records_reply_alias(self):
  task,topic=self.start();s,_=self.session(task);self.c.topics.say(s,{'request_id':'discussion-1','text':'开始核对。'})
  sender=Sender(self.cfg['app_id'],self.sent);real=sender.send_group;keys=[]
  def send(mid,key,body):
   keys.append(key)
   if len(keys)==1:raise OSError('temporary')
   return real(mid,key,body)
  sender.send_group=send;self.c.topics.sender_factory=lambda *_:sender
  self.c.topics.deliver()
  with self.store.db() as db:db.execute('UPDATE group_discussions SET next_attempt=0')
  self.c.topics.deliver();self.assertEqual(keys[0],keys[1]);self.assertEqual(len([x for x in self.sent if x['key']!='received:om_group']),1)
  self.assertTrue(self.main.handle_event(self.event('om_retry_alias',parent=self.sent[-1]['id'],mention=False)))
 def test_topic_submit_rejects_alias_from_different_topic(self):
  task,topic=self.start();_,other=self.start('om_group_2')
  client=self.main.bridge
  with self.assertRaises(HTTPError):client.topic_submit(other,'om_group','伪造请求','fs_fake')
 def test_discussion_budget_and_recovery_do_not_duplicate_delivery(self):
  task,_=self.start();session,_=self.session(task)
  for i in range(24):self.c.topics.say(session,{'request_id':'discussion-'+str(i),'text':'讨论 '+str(i)})
  with self.assertRaises(BridgeError):self.c.topics.say(session,{'request_id':'discussion-over','text':'多余消息'})
  self.c.topics.deliver();self.c.topics.deliver();count=len(self.sent)
  from group_topics import GroupTopics
  recovered=GroupTopics(self.c,sender_factory=lambda app,secret:Sender(app,self.sent));recovered.deliver();self.assertEqual(len(self.sent),count)
 def test_backend_requires_real_group_message_permission_before_enabling(self):
  cfg=self.cfg;data={'app_ref':self.f.source['app_ref'],'revision':revision(cfg),'chat_id':'oc_shared','name':'话题群','project_id':self.f.source['project_id'],'prompt_template':'群 {{message}}','topic_collaboration':True,'enabled':True}
  with patch('feishu_groups.check_topic_permissions',side_effect=BridgeError(409,'topic_permissions_missing','缺少群消息权限')):
   with self.assertRaises(BridgeError):self.o.groups.save_group(data)
  self.assertEqual(json.loads(self.o.groups.resolve(self.f.source['app_ref'])[1].read_text()),cfg)
  data['enabled']=False
  with patch('feishu_groups.check_topic_permissions') as check:
   self.o.groups.save_group(data);check.assert_not_called()
 def test_group_defaults_reuse_business_purpose_with_group_delivery(self):
  defaults=self.o.groups.group_defaults({'app_ref':self.f.source['app_ref']})
  self.assertIn('openchat',defaults['prompt_template']);self.assertNotIn('仅允许创建者私聊',defaults['prompt_template'])
  self.assertEqual(defaults['prompt_template'].count('{{message}}'),1)
 def test_minimal_scope_check_and_sdk_plain_group_message(self):
  def call(path,**kwargs):
   if path.startswith('/auth'):return {'code':0,'tenant_access_token':'FAKE_TOKEN'}
   return {'code':0,'data':{'scopes':[{'scope_name':s,'grant_status':1,'scope_type':'tenant'} for s in ('im:message.group_msg','im:message:readonly')]}}
  with patch('feishu_probe._call',side_effect=call):self.assertEqual(check_topic_permissions('cli_fake','FAKE_SECRET'),{'verified':True})
  s=FeishuSender('fake','fake');s.client=Mock();s.client.im.v1.message.create.return_value=Mock(success=lambda:True,data=Mock(message_id='om_group'))
  s.send_group('oc_shared','same-event','讨论');one=s.client.im.v1.message.create.call_args.args[0];s.send_group('oc_shared','same-event','讨论');two=s.client.im.v1.message.create.call_args.args[0]
  self.assertEqual(one.receive_id_type,'chat_id');self.assertEqual(one.request_body.receive_id,'oc_shared');self.assertEqual(one.request_body.uuid,two.request_body.uuid);s.client.im.v1.message.reply.assert_not_called()


if __name__=='__main__':unittest.main()
