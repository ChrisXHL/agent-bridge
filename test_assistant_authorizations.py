import json
import time
from unittest.mock import Mock
import test_assistant_collaboration as fixtures
from assistant_collaboration import AssistantCollaboration
from codex_http_bridge import BridgeError
from feishu_groups import atomic_write
from feishu_worker import FeishuAdapter,FeishuSender,Inbox
from test_feishu import FakeBridge,event


class NestedTests(fixtures.CollaborationTests):
 def setUp(self):
  super().setUp()
  self.sender=Mock();self.sender.send_text.return_value='om_notice';self.sender.reply.return_value='om_ack'
  self.c.authorizations.sender_factory=lambda *_:self.sender
 def pending(self):
  with self.store.db() as db:return dict(db.execute('SELECT * FROM assistant_authorizations ORDER BY created_at DESC LIMIT 1').fetchone())
 def approve(self,a,row=None,verb='确认',sender=None,mid=None):
  row=row or self.pending();_,path,inbox=self.o.groups.resolve(a['app_ref']);cfg=json.loads(path.read_text())
  adapter=FeishuAdapter(cfg,Inbox(inbox),FakeBridge(),self.sender)
  for item in Inbox(inbox).submitted():adapter.bridge.tasks[item['task_id']]={'status':'执行中','result':None}
  adapter.handle_event(event(mid or 'om_'+str(time.time_ns()),sender=sender or a['owner_open_id'],text=verb+'协作 '+row['code']))
  adapter.pump_once();self.assertEqual(adapter.bridge.submissions,[])
  self.c.authorizations.tick()
 def child_session(self,batch):
  self.engine.tick()
  tid=self.c.result(self.session,batch)['results'][0]['task_id']
  value=json.loads((self.c.root/(tid+'.json')).read_text())
  return self.c.authenticate('Bearer '+value['token'])
 def natural(self,a,text='允许本次协作',parent=None,sender=None,mid=None,at=None):
  _,path,inbox=self.o.groups.resolve(a['app_ref']);cfg=json.loads(path.read_text())
  adapter=FeishuAdapter(cfg,Inbox(inbox),FakeBridge(),self.sender)
  for item in Inbox(inbox).submitted():adapter.bridge.tasks[item['task_id']]={'status':'执行中','result':None}
  payload=event(mid or 'om_natural_'+str(time.time_ns()),sender=sender or a['owner_open_id'],text=text,parent=parent)
  if at is not None:payload['event']['message']['create_time']=str(int(at*1000))
  adapter.handle_event(payload);adapter.pump_once()
  self.assertIsNotNone(adapter.inbox.linked_source(payload['event']['message']['message_id']))
  self.assertEqual(adapter.bridge.submissions,[])
  self.c.authorizations.tick()
  return self.sender.reply.call_args.args[2]
 def setup_natural(self):
  self.configure([])
  self.sender.send_text.side_effect=lambda *_:'om_notice_'+str(time.time_ns())
  self.call([{'assistant_id':self.a['id'],'message':'整理测试短文'}])
  self.c.authorizations.tick()
  row=self.pending();return row,json.loads(row['approvers'])
 def test_natural_consent_delivers_code_free_notice_and_resumes_once(self):
  row,owners=self.setup_natural()
  body=self.sender.send_text.call_args.args[2]
  self.assertIn('允许本次协作',body);self.assertNotIn(row['code'],body)
  self.natural(owners[0],sender='ou_other');self.assertFalse(any(a['approved'] for a in json.loads(self.pending()['approvers'])))
  self.natural(owners[0],mid='om_once');self.natural(owners[0],mid='om_once')
  self.assertEqual(sum(a['approved'] for a in json.loads(self.pending()['approvers'])),1)
  self.natural(owners[1]);self.assertEqual(self.pending()['status'],'approved')
  self.assertEqual(self.c.allowed(self.source['id'],'different-task'),set())
  self.assertEqual(self.c.selected(self.source['id']),[])
  self.engine.tick();self.assertEqual(self.fake.created,2)
 def test_multiple_pending_requires_reply_to_exact_notice(self):
  first,owners=self.setup_natural()
  self.call([{'assistant_id':self.b['id'],'message':'另一件事'}],rid='other-pending-request')
  self.c.authorizations.tick();second=self.pending()
  self.assertIn('有几件事',self.natural(owners[0]))
  with self.store.db() as db:self.assertFalse(any(a['approved'] for r in db.execute('SELECT approvers FROM assistant_authorizations') for a in json.loads(r[0])))
  notice=owners[0]['notice']['outbound_id']
  self.natural(owners[0],parent=notice)
  with self.store.db() as db:
   a=json.loads(db.execute('SELECT approvers FROM assistant_authorizations WHERE id=?',(first['id'],)).fetchone()[0])
   b=json.loads(db.execute('SELECT approvers FROM assistant_authorizations WHERE id=?',(second['id'],)).fetchone()[0])
  self.assertTrue(a[0]['approved']);self.assertFalse(any(x['approved'] for x in b))
  # Repeating consent while the first request awaits another owner must not
  # silently approve the other request.
  self.assertIn('有几件事',self.natural(owners[0]))
  with self.store.db() as db:
   b=json.loads(db.execute('SELECT approvers FROM assistant_authorizations WHERE id=?',(second['id'],)).fetchone()[0])
  self.assertFalse(any(x['approved'] for x in b))
 def test_delayed_reply_cannot_approve_a_newer_request_or_unknown_quote(self):
  row,owners=self.setup_natural()
  self.natural(owners[0],at=row['created_at']-1)
  self.natural(owners[0],parent='om_unrelated_message')
  self.assertFalse(any(a['approved'] for a in json.loads(self.pending()['approvers'])))
 def test_expired_quote_never_falls_through_to_another_pending_request(self):
  old,owners=self.setup_natural()
  with self.store.db() as db:db.execute('UPDATE assistant_authorizations SET expires_at=0 WHERE id=?',(old['id'],))
  self.c.authorizations.reconcile()
  self.call([{'assistant_id':self.b['id'],'message':'新的一件事'}],rid='after-expired-request')
  self.c.authorizations.tick()
  self.assertIn('已过期',self.natural(owners[0],parent=owners[0]['notice']['outbound_id']))
  self.assertFalse(any(a['approved'] for a in json.loads(self.pending()['approvers'])))
 def test_natural_denial_stops_without_granting_and_plain_agreement_is_not_consent(self):
  row,owners=self.setup_natural()
  from assistant_consent import parse
  for text in ('好的','对的','同意','可以','请允许本次协作然后删文件'):
   self.assertIsNone(parse(text))
  self.natural(owners[0],text='拒绝本次协作')
  self.assertEqual(self.pending()['status'],'denied');self.assertEqual(self.fake.created,1)
  self.assertEqual(self.c.allowed(self.source['id'],self.parent),set())
 def test_pending_two_owners_and_task_only_scope(self):
  self.configure([]);batch=self.call([{'assistant_name':self.a['name'],'message':'分析'}])
  self.assertTrue(batch['waiting_authorization']);self.engine.tick();self.assertEqual(self.fake.created,1)
  row=self.pending();owners=json.loads(row['approvers']);self.assertEqual(len(owners),2)
  self.c.authorizations.tick();self.assertEqual(self.sender.send_text.call_count,2)
  self.c.authorizations.tick();self.assertEqual(self.sender.send_text.call_count,2)
  self.approve(owners[0],sender='ou_stranger');self.assertEqual(self.pending()['status'],'pending')
  self.approve(owners[0]);self.assertEqual(self.pending()['status'],'pending');self.engine.tick();self.assertEqual(self.fake.created,1)
  # Restart preserves the first confirmation and duplicate events do not grant extra access.
  self.c=AssistantCollaboration(self.o,self.c.base_url);self.engine.collaboration=self.c
  self.c.authorizations.sender_factory=lambda *_:self.sender
  self.approve(owners[1],mid='om_final');self.approve(owners[1],mid='om_final')
  self.assertEqual(self.pending()['status'],'approved');self.assertEqual(self.c.selected(self.source['id']),[])
  self.assertIn(self.a['id'],self.c.allowed(self.source['id'],self.parent));self.assertEqual(self.c.allowed(self.source['id'],'other-root'),set())
  self.engine.tick();self.assertEqual(self.fake.created,2)
 def test_source_and_target_app_scoped_identity(self):
  self.configure([]);self.call([{'assistant_id':self.a['id'],'message':'分析'}]);row=self.pending();owners=json.loads(row['approvers'])
  # The same literal Open ID does not approve the second app automatically.
  self.approve(owners[0]);self.assertEqual(sum(x['approved'] for x in json.loads(self.pending()['approvers'])),1)
  wrong={**owners[1],'app_ref':owners[0]['app_ref']}
  self.approve(wrong);self.assertEqual(self.pending()['status'],'pending')
 def test_completed_turn_waits_for_late_confirmation_and_resumes_once(self):
  self.configure([]);batch=self.call([{'assistant_id':self.a['id'],'message':'核对算术'}])['batch_id']
  parent_thread=self.store.task(self.parent)['thread_id'];self.fake.complete(parent_thread,'等待你确认')
  self.engine.tick();self.assertEqual(self.store.task(self.parent)['error_code'],'assistant_authorization_required')
  self.c.authorizations.reconcile();row=self.pending();self.assertEqual(row['status'],'pending')
  owners=json.loads(row['approvers']);self.approve(owners[0])
  self.c=AssistantCollaboration(self.o,self.c.base_url);self.engine.collaboration=self.c
  self.c.authorizations.sender_factory=lambda *_:self.sender
  self.approve(owners[1]);self.c.authorizations.tick()
  self.engine.tick();self.assertEqual(len(self.fake.messages[parent_thread]),2)
  self.assertIn('assistant-call.py',self.fake.messages[parent_thread][-1])
  self.engine.tick();out=self.c.result(self.session,batch);child=self.store.task(out['results'][0]['task_id'])
  self.assertEqual(child['status'],'执行中');self.fake.complete(child['thread_id'],'165');self.engine.tick()
  self.assertFalse(self.c.result(self.session,batch)['pending'])
  self.fake.complete(parent_thread,'核对结果165');self.engine.tick()
  self.assertEqual(self.store.task(self.parent)['status'],'已完成')
  with self.store.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM assistant_resume_messages').fetchone()[0],1)
  self.assertEqual(self.c.selected(self.source['id']),[])
 def test_confirmation_racing_completed_turn_is_not_lost(self):
  self.configure([]);self.call([{'assistant_id':self.a['id'],'message':'核对'}]);row=self.pending()
  for owner in json.loads(row['approvers']):self.approve(owner)
  self.assertEqual(self.pending()['status'],'approved')
  parent_thread=self.store.task(self.parent)['thread_id'];self.fake.complete(parent_thread,'暂时等待')
  self.engine.tick();self.assertEqual(self.store.task(self.parent)['status'],'执行中')
  self.assertEqual(len(self.fake.messages[parent_thread]),2)
 def test_expired_paused_authorization_resumes_to_report_failure(self):
  self.configure([]);batch=self.call([{'assistant_id':self.a['id'],'message':'核对'}])['batch_id']
  thread=self.store.task(self.parent)['thread_id'];self.fake.complete(thread,'等待确认');self.engine.tick()
  with self.store.db() as db:db.execute('UPDATE assistant_authorizations SET expires_at=0')
  self.c.authorizations.reconcile();self.engine.tick()
  self.assertEqual(len(self.fake.messages[thread]),2)
  out=self.c.result(self.session,batch);self.assertFalse(out['pending']);self.assertEqual(out['results'][0]['error'],'assistant_authorization_expired')
 def test_paused_authorization_releases_capacity_and_children_wait_for_parent_resume(self):
  self.configure([]);batch=self.call([{'assistant_id':self.a['id'],'message':'核对'}])['batch_id']
  self.fake.complete(self.store.task(self.parent)['thread_id'],'等确认');self.engine.tick();self.engine.max_active=1
  other=self.receipt('om_during_authorization');self.engine.tick()
  self.assertEqual(self.store.task(other)['status'],'执行中')
  row=self.pending()
  for owner in json.loads(row['approvers']):self.approve(owner)
  self.engine.tick();out=self.c.result(self.session,batch)
  self.assertEqual(out['results'][0]['status'],'等待 Codex')
  self.fake.complete(self.store.task(other)['thread_id'],'done');self.engine.tick();self.engine.tick()
  self.assertEqual(self.c.result(self.session,batch)['results'][0]['status'],'执行中')
 def test_group_confirmation_and_wrong_app_header_are_rejected(self):
  self.configure([]);self.call();row=self.pending();a=json.loads(row['approvers'])[0]
  _,path,inbox=self.o.groups.resolve(a['app_ref']);cfg=json.loads(path.read_text())
  adapter=FeishuAdapter(cfg,Inbox(inbox),FakeBridge(),self.sender)
  payload=event('om_group_confirm',sender=a['owner_open_id'],chat_type='group',text='@_user_1 确认协作 '+row['code'],mentions=[{'id':{'open_id':cfg['bot_open_id']},'key':'@_user_1'}])
  self.assertFalse(adapter.handle_event(payload))
  payload=event('om_wrong_app',sender=a['owner_open_id'],text='确认协作 '+row['code']);payload['header']={'app_id':'cli_other'}
  self.assertFalse(adapter.handle_event(payload));self.assertEqual(self.pending()['status'],'pending')
 def test_denied_expired_and_parent_stop_fail_without_execution(self):
  self.configure([])
  for i,case in enumerate(('denied','expired','cancelled')):
   batch=self.call([{'assistant_id':self.a['id'],'message':'分析'}],rid='test-fail-'+str(i))['batch_id'];row=self.pending()
   if case=='denied':self.approve(json.loads(row['approvers'])[0],verb='拒绝')
   elif case=='expired':
    with self.store.db() as db:db.execute('UPDATE assistant_authorizations SET expires_at=0 WHERE id=?',(row['id'],))
   else:self.store.set_task(self.parent,'已停止')
   self.c.authorizations.reconcile();self.assertEqual(self.pending()['status'],case)
   result=self.c.result(self.session,batch);self.assertFalse(result['pending']);self.assertEqual(result['results'][0]['status'],'失败')
  self.assertEqual(self.fake.created,1)
 def test_owner_change_cancels_pending(self):
  self.configure([]);self.call();p=self.o.directory(self.a['id'])/'credentials.json';value=json.loads(p.read_text());value['owner_open_id']='ou_changed';atomic_write(p,value)
  self.c.authorizations.reconcile();self.assertEqual(self.pending()['status'],'cancelled')
 def test_same_manager_needs_one_verified_confirmation(self):
  _,mpath,_=self.o.groups.resolve(self.b['app_ref']);cfg=json.loads(mpath.read_text());cfg['assistant_manager']={'admin_open_id':'ou_admin'};atomic_write(mpath,cfg)
  managed={'app_ref':self.b['app_ref'],'admin_open_id':'ou_admin'}
  for j in (self.source,self.a):self.o.update(j['id'],managed_by=managed)
  self.configure([]);self.call([{'assistant_id':self.a['id'],'message':'分析'}]);row=self.pending();owners=json.loads(row['approvers'])
  self.assertEqual(len(owners),1);self.assertEqual(owners[0]['mode'],'manager')
  self.approve(owners[0]);self.assertEqual(self.pending()['status'],'approved')
 def test_nested_cycle_budget_and_separate_slots(self):
  batch=self.call()['batch_id'];self.engine.tick();self.engine.tick()
  child=self.child_session(batch)
  self.c.configure(self.a['id'],{'targets':[self.b['id']],'revision':self.c.settings(self.a['id'])['revision']})
  with self.assertRaises(BridgeError):self.c.call(child,{'request_id':'cycle-check','requests':[{'assistant_id':self.source['id'],'message':'回到祖先'}]})
  nested=self.c.call(child,{'request_id':'nested-real','requests':[{'assistant_id':self.b['id'],'message':'继续核查'}]})
  self.engine.tick();out=self.c.result(child,nested['batch_id']);self.assertEqual(out['results'][0]['status'],'执行中')
  # Both depth-1 slots are occupied; depth-2 still starts.
  with self.store.db() as db:self.assertEqual(db.execute('SELECT depth FROM assistant_calls WHERE child_task=?',(out['results'][0]['task_id'],)).fetchone()[0],2)
  for i in range(5):self.c.call(child,{'request_id':'budget-extra-'+str(i),'requests':[{'assistant_id':self.b['id'],'message':'核查'}]})
  with self.assertRaises(BridgeError):self.call(rid='root-over-budget')
  self.store.set_task(self.parent,'已停止')
  token=json.loads((self.c.root/(child['task_id']+'.json')).read_text())['token']
  with self.assertRaises(BridgeError):self.c.authenticate('Bearer '+token)
 def test_depth_three_has_no_more_delegation_context(self):
  # Fourth assistant needed to reach three edges without revisiting an ancestor.
  j=self.f.prepare();atomic_write(self.o.directory(j['id'])/'credentials.json',{'app_id':'cli_four','app_secret':'fake','owner_open_id':'ou_four','brand':'feishu'});self.f.finish(j);four=self.o.read(j['id'])
  child=self.child_session(self.call([{'assistant_id':self.a['id'],'message':'一级'}])['batch_id'])
  self.c.configure(self.a['id'],{'targets':[self.b['id']],'revision':self.c.settings(self.a['id'])['revision']})
  batch=self.c.call(child,{'request_id':'level-two','requests':[{'assistant_id':self.b['id'],'message':'二级'}]})['batch_id'];self.engine.tick()
  tid=self.c.result(child,batch)['results'][0]['task_id'];token=json.loads((self.c.root/(tid+'.json')).read_text())['token'];second=self.c.authenticate('Bearer '+token)
  self.c.configure(self.b['id'],{'targets':[four['id']],'revision':self.c.settings(self.b['id'])['revision']})
  third=self.c.call(second,{'request_id':'level-three','requests':[{'assistant_id':four['id'],'message':'三级'}]})['batch_id'];self.engine.tick()
  tid=self.c.result(second,third)['results'][0]['task_id'];task=self.store.task(tid)
  self.assertEqual(task['status'],'执行中');self.assertNotIn('assistant-call.py',self.fake.messages[task['thread_id']][0]);self.assertFalse((self.c.root/(tid+'.json')).exists())
 def test_send_text_sdk_contract_and_stable_uuid(self):
  sender=FeishuSender('fake','fake');sender.client=Mock();sender.client.im.v1.message.create.return_value=Mock(success=lambda:True,data=Mock(message_id='om_out'))
  self.assertEqual(sender.send_text('ou_owner','request-1','hello'),'om_out');sender.send_text('ou_owner','request-1','hello')
  calls=sender.client.im.v1.message.create.call_args_list;a,b=[x.args[0] for x in calls]
  self.assertEqual(a.receive_id_type,'open_id');self.assertEqual(a.request_body.receive_id,'ou_owner');self.assertEqual(a.request_body.uuid,b.request_body.uuid)


if __name__=='__main__':
 import unittest
 unittest.main()
