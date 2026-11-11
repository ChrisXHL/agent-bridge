import json
import time
import unittest
from unittest.mock import patch
import test_assistant_onboarding as fixtures
from assistant_manager import AssistantManager, HELP, interpret
from feishu_groups import atomic_write, revision
from feishu_worker import FeishuAdapter, Inbox
from test_feishu import FakeBridge, FakeSender, event
from codex_http_bridge import BridgeError

class ManagerTests(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.AssistantTests();self.f.setUp();self.onboard=self.f.manager
  self.path=self.f.root/'feishu.json';self.cfg=json.loads(self.path.read_text())
  self.cfg.update(enabled=True,assistant_manager={'admin_open_id':'ou_creator'},allowed_open_ids=['ou_creator','ou_other'])
  atomic_write(self.path,self.cfg);self.baseline=self.path.read_bytes()
  self.inbox=Inbox(self.f.root/'inbox.sqlite3');self.bridge=FakeBridge();self.sender=FakeSender()
  self.adapter=FeishuAdapter(self.cfg,self.inbox,self.bridge,self.sender)
  self.intent={'action':'create','target_id':'','requirements':'整理资料并创建飞书文档','audience':'private','reply':''}
  self.manager=AssistantManager(self.onboard,interpreter=lambda *_:dict(self.intent),sender_factory=lambda *_:self.sender)
 def tearDown(self):self.manager.close();self.f.tearDown()
 def receipt(self,ident='om_management',text='创建一个资料整理助手'):
  self.assertTrue(self.adapter.handle_event(event(ident,sender='ou_creator',text=text)))
  item=self.inbox.linked_source(ident);return self.manager.receive('default',item)
 def child(self,**extra):
  j=self.f.prepare(**extra);self.onboard.update(j['id'],managed_by={'app_ref':'default','admin_open_id':'ou_creator'})
  self.f.finish(j);return self.onboard.read(j['id'])
 def test_only_owner_private_messages_enter_management(self):
  self.assertFalse(self.adapter.handle_event(event('om_other',sender='ou_other')))
  self.cfg.update(allowed_chat_ids=['oc_group'],bot_open_id='ou_bot')
  self.assertFalse(self.adapter.handle_event(event('om_group',sender='ou_creator',chat='oc_group',chat_type='group',mentions=[{'id':{'open_id':'ou_bot'},'key':'@bot'}])))
  r=self.receipt();self.adapter.pump_once();self.assertEqual(self.bridge.submissions,[])
  self.assertEqual(json.loads(r['source']['route_json'])['mode'],'assistant_manager')
 def test_tampered_source_rejected(self):
  r=self.receipt()
  for changes in [{'sender_id':'ou_other'},{'app_id':'cli_other'},{'chat_type':'group'},{'connection_id':'wrong'},{'route_json':'{"mode":"prompt"}'}]:
   with self.assertRaises(BridgeError):self.manager.receive('default',{**r['source'],**changes,'message_id':'om_tamper'})
 def question(self,j):
  r=self.receipt('om_question','之前创建失败了，切换账号之后链接就失效了')
  self.intent.update(action='help',target_id=j['id'],requirements='',reply='你是想继续配置“资料助手”，重新获取有效的扫码链接吗？',proposal={'action':'retry','target_id':j['id'],'requirements':'','audience':'private'})
  self.manager.process(r['id']);self.manager.notify(r,'result',self.manager.read(r['id'])['result'])
  return self.manager.read(r['id'])
 def test_short_confirmation_resumes_exact_question_after_restart(self):
  j=self.child();self.onboard.update(j['id'],phase='failed');question=self.question(j)
  answer=self.receipt('om_yes','对的')
  restored=AssistantManager(self.onboard,interpreter=lambda *_:self.fail('明确确认不应重新猜助手'),sender_factory=lambda *_:self.sender)
  with patch.object(self.onboard,'retry') as retry:restored.process(answer['id']);retry.assert_called_once_with(j['id'],{})
  out=restored.read(answer['id']);self.assertEqual(out['job_id'],j['id']);self.assertEqual(out['confirmation_of'],question['id'])
 def test_explicit_reply_to_old_question_cannot_execute_twice(self):
  j=self.child();self.onboard.update(j['id'],phase='failed');question=self.question(j)
  for mid in ('om_yes_one','om_yes_two'):
   r=self.receipt(mid,'对的');r['source']['parent_id']=question['notifications']['result']['outbound_id'];self.manager.save(r)
   with patch.object(self.onboard,'retry') as retry:self.manager.process(r['id'])
   self.assertEqual(retry.call_count,1 if mid=='om_yes_one' else 0)
 def test_context_has_real_question_and_is_scoped_to_chat_identity(self):
  j=self.child();question=self.question(j);answer=self.receipt('om_followup','对的')
  history=self.manager.conversation(answer);self.assertEqual(history[-1]['id'],question['id'])
  context=self.manager.context(history);self.assertIn('重新获取有效',context[-1]['管理员'])
  for field in ('sender_id','chat_id','app_id','connection_id'):
   other={**answer,'source':{**answer['source'],field:'other'}}
   self.assertEqual(self.manager.conversation(other),[])
  self.assertEqual(self.manager.conversation({**answer,'app_ref':'other'}),[])
 def test_replied_question_selects_correct_context_not_latest_topic(self):
  j=self.child();question=self.question(j)
  later=self.receipt('om_other_topic','查看读书助手');self.intent.update(action='help',reply='另一个话题',proposal=None);self.manager.process(later['id']);self.manager.notify(later,'result','另一个话题')
  answer=self.receipt('om_anchored','对的');answer['source']['parent_id']=question['notifications']['result']['outbound_id']
  self.assertEqual(self.manager.conversation(answer)[-1]['id'],question['id'])
 def test_unsent_or_old_question_cannot_be_confirmed_and_context_omits_links(self):
  j=self.child();question=self.question(j);answer=self.receipt('om_late','对的')
  question['notifications']['result']['state']='sending';self.assertIsNone(self.manager.confirmation(answer,[question]));self.assertEqual(self.manager.context([question])[0]['管理员'],'')
  question['notifications']['result'].update(state='sent',body='确认链接 https://open.feishu.cn/?user_code=PRIVATE_TOKEN')
  self.assertNotIn('PRIVATE_TOKEN',json.dumps(self.manager.context([question])))
  answer['created_at']=question['created_at']+1801;self.assertIsNone(self.manager.confirmation(answer,[question]))
 def test_topic_switch_does_not_consume_pending_confirmation(self):
  j=self.child();self.question(j);answer=self.receipt('om_switch','创建一个新的会议助手')
  self.assertIsNone(self.manager.confirmation(answer,self.manager.conversation(answer)))
 def test_interpreter_receives_related_dialog_for_legacy_questions(self):
  j=self.child();r=self.receipt('om_legacy','之前创建失败，链接失效了')
  self.intent.update(action='help',target_id=j['id'],reply='你是想继续配置资料助手吗？');self.manager.process(r['id']);self.manager.notify(r,'result','你是想继续配置资料助手吗？')
  answer=self.receipt('om_legacy_yes','对的');seen=[]
  def interpreter(text,candidates,directory,context):
   seen.extend(context);return dict(action='retry',target_id=j['id'],requirements='',audience='private',reply='')
  self.manager.interpreter=interpreter;self.manager.process(answer['id'])
  self.assertIn('继续配置资料助手',seen[-1]['管理员']);self.assertEqual(self.manager.read(answer['id'])['job_id'],j['id'])
 def test_duplicate_message_and_crash_replay_create_once(self):
  r=self.receipt();self.manager.process(r['id']);first=self.manager.read(r['id'])['job_id']
  self.manager.process(r['id']);self.assertEqual(self.manager.read(r['id'])['job_id'],first)
  self.assertEqual(len(list(self.onboard.root.glob('asst_*/job.json'))),1)
  self.assertEqual(self.manager.receive('default',r['source'])['id'],r['id'])
  self.assertEqual(self.path.read_bytes(),self.baseline)
 def test_manager_bootstrap_is_singleton(self):
  a=self.onboard.bootstrap_manager();b=self.onboard.bootstrap_manager()
  self.assertEqual(a['id'],b['id']);self.assertEqual(a['kind'],'manager')
  self.assertEqual(self.onboard.read(a['id'])['plan']['name'],'助手管理员')
 def test_cannot_manage_foreign_or_existing_assistant(self):
  j=self.f.prepare();self.f.finish(j);r=self.receipt()
  with self.assertRaises(BridgeError):self.manager.target(r,j['id'])
 def test_explicit_removal_and_restore_intent_requires_unique_exact_name(self):
  candidates=[{'id':'one','name':'读书笔记助手'}]
  for text,action in [('删除读书笔记助手','remove'),('移除「读书笔记助手」','remove'),('恢复读书笔记助手','restore')]:
   d=interpret(text,candidates,self.f.root);self.assertEqual(d['action'],action);self.assertEqual(d['target_id'],'one')
  self.assertEqual(interpret('删除读书助手',candidates,self.f.root)['action'],'help')
  self.assertEqual(interpret('删除读书笔记助手',candidates*2,self.f.root)['action'],'help')
 def test_manager_remove_restore_preserves_other_apps(self):
  j=self.child();r=self.receipt();self.intent.update(action='remove',target_id=j['id'])
  self.manager.process(r['id']);self.assertEqual(self.onboard.read(j['id'])['phase'],'removed')
  self.assertIn('机器人仍保留',self.manager.read(r['id'])['result'])
  r=self.receipt('om_restore');self.intent['action']='restore';self.manager.process(r['id'])
  self.assertEqual(self.onboard.read(j['id'])['phase'],'ready')
  self.assertEqual(self.path.read_bytes(),self.baseline)
 def test_manager_cannot_remove_unowned_assistant(self):
  j=self.f.prepare();self.f.finish(j);r=self.receipt();self.intent.update(action='remove',target_id=j['id'])
  self.manager.process(r['id']);self.assertEqual(self.manager.read(r['id'])['error_code'],'target_forbidden')
  self.assertEqual(self.onboard.read(j['id'])['phase'],'ready')
  self.onboard.update(j['id'],managed_by={'app_ref':'another','admin_open_id':'ou_creator'})
  with self.assertRaises(BridgeError):self.manager.target(r,j['id'])
 def test_status_only_lists_owned_children_and_never_credentials(self):
  own=self.child();foreign=self.f.prepare();self.onboard.update(foreign['id'],phase='ready')
  r=self.receipt();self.intent.update(action='status',requirements='');self.manager.process(r['id'])
  out=self.manager.read(r['id'])['result'];self.assertEqual(out.count('已配置'),1)
  self.assertNotIn('SECRET',out);self.assertNotIn('connection_key',out);self.assertIn('applink.feishu.cn',out)
 def test_purpose_update_preserves_credentials_project_and_other_bots(self):
  j=self.child();_,path,_=self.onboard.groups.resolve(j['app_ref']);before=json.loads(path.read_text())
  r=self.receipt(text='修改用途');self.intent.update(action='update',target_id=j['id'],requirements='把会议记录整理成纪要并提取待办')
  with patch('assistant_manager.plan_with_codex',return_value={**fixtures.PLAN,'instructions':'整理会议纪要并提取待办。'}):self.manager.process(r['id'])
  after=json.loads(path.read_text())
  for key in ['app_id','app_secret','connection_id','connection_key','allowed_open_ids','allowed_chat_ids']:self.assertEqual(before[key],after[key])
  self.assertIn('会议纪要',after['prompt_template']);self.assertEqual(after['prompt_template'].count('{{message}}'),1)
  self.assertEqual(self.path.read_bytes(),self.baseline);self.assertEqual(self.onboard.read(j['id'])['project_id'],j['project_id'])
 def test_manager_can_update_its_own_management_preference(self):
  r=self.receipt('om_self_rule','以后修改助手用途时，保留原有报告风格')
  self.intent.update(action='self_update',target_id='',requirements='修改已有助手用途时保留仍然适用的报告风格。')
  self.manager.process(r['id'])
  saved=json.loads(self.path.read_text())
  self.assertIn('报告风格',saved['self_update_rule'])
  self.assertEqual(self.manager.read(r['id'])['status'],'done')
  self.manager.process(r['id'])
  self.assertEqual(json.loads(self.path.read_text()),saved)
 def test_concurrent_manual_edit_is_not_overwritten(self):
  j=self.child();_,path,_=self.onboard.groups.resolve(j['app_ref']);r=self.receipt();self.intent.update(action='update',target_id=j['id'])
  def planner(*_):
   cfg=json.loads(path.read_text());cfg['prompt_template']='手工新规则 {{message}}';atomic_write(path,cfg);return dict(fixtures.PLAN)
  with patch('assistant_manager.plan_with_codex',side_effect=planner):self.manager.process(r['id'])
  self.assertEqual(json.loads(path.read_text())['prompt_template'],'手工新规则 {{message}}')
  self.assertEqual(self.manager.read(r['id'])['error_code'],'settings_changed')
 def test_update_recovers_after_config_saved_before_job_checkpoint(self):
  j=self.child();r=self.receipt();d={**self.intent,'action':'update','target_id':j['id']}
  with patch('assistant_manager.plan_with_codex',return_value=dict(fixtures.PLAN)),patch.object(self.onboard,'update',side_effect=RuntimeError('crash')):
   with self.assertRaises(RuntimeError):self.manager.update_purpose(r,d)
  r=self.manager.read(r['id'])
  with patch('assistant_manager.plan_with_codex',side_effect=AssertionError('must reuse plan')):self.manager.update_purpose(r,d)
  self.assertEqual(self.onboard.read(j['id'])['requirements'],d['requirements'])
 def test_revoked_manager_cannot_commit_after_planning(self):
  j=self.child();_,path,_=self.onboard.groups.resolve(j['app_ref']);original=path.read_bytes();r=self.receipt();self.intent.update(action='update',target_id=j['id'])
  def planner(*_):
   cfg=json.loads(self.path.read_text());cfg['enabled']=False;atomic_write(self.path,cfg);return dict(fixtures.PLAN)
  with patch('assistant_manager.plan_with_codex',side_effect=planner):self.manager.process(r['id'])
  self.assertEqual(path.read_bytes(),original)
 def test_notifications_are_durable_and_idempotent(self):
  r=self.receipt();self.manager.notify(r,'received','收到');self.manager.notify(r,'received','收到')
  self.assertEqual([x[1] for x in self.sender.sent],['received'])
  restored=AssistantManager(self.onboard,sender_factory=lambda *_:self.sender);restored.notify(r,'received','收到')
  self.assertEqual(len(self.sender.sent),1)
 def test_uncertain_old_reply_not_blindly_repeated(self):
  r=self.receipt();r['notifications']['test']={'state':'sending','first_attempt':time.time()-700,'last_attempt':0,'body':'结果'};self.manager.save(r)
  self.manager.notify(r,'test','结果');self.assertEqual([x[1] for x in self.sender.sent],['received'])
 def test_uncertain_app_retry_does_not_create_new_registration(self):
  j=self.child();self.onboard.update(j['id'],phase='needs_check');r=self.receipt();self.intent.update(action='retry',target_id=j['id'])
  with patch.object(self.onboard,'retry',side_effect=AssertionError('must not guess confirmation')):self.manager.process(r['id'])
  self.assertEqual(self.manager.read(r['id'])['job_id'],j['id']);self.assertEqual(self.onboard.read(j['id'])['phase'],'needs_check')
 def test_background_delivers_confirmation_then_ready_to_original_message(self):
  r=self.receipt();self.manager.process(r['id']);job_id=self.manager.read(r['id'])['job_id']
  atomic_write(self.onboard.directory(job_id)/'registration.json',{'status':'waiting_scan','url':'https://open.feishu.cn/page/launcher?user_code=TEST','expires_at':time.time()+600})
  self.onboard.update(job_id,phase='waiting_scan',name='资料助手',message='请确认')
  self.manager.tick();self.assertIn('https://open.feishu.cn/',self.sender.sent[-1][2])
  self.onboard.update(job_id,phase='ready',app_id='cli_created',message='已配置')
  self.manager.tick();self.manager.tick()
  self.assertEqual(len(self.sender.sent),3);self.assertTrue(all(x[0]=='om_management' for x in self.sender.sent))
  self.assertIn('applink.feishu.cn',self.sender.sent[-1][2]);self.assertEqual(self.bridge.submissions,[])

if __name__=='__main__':unittest.main()
