"""Isolation, recovery and privacy gates for the assistant creation journey."""
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from assistant_onboarding import AssistantOnboarding, build_prompt, checked_plan, prompt_for_job, other_local_receiver
from codex_http_bridge import BridgeError
from feishu_groups import GroupSettings, atomic_write
from service import Store
from feishu_worker import Inbox

PLAN={'name':'资料助手','summary':'整理资料并返回飞书文档链接','instructions':'按用户资料提取要点并创建文档，读回后交付链接。','limitations':''}
CREDENTIAL={'app_id':'cli_aaaaaaaaaaaaaaaa','app_secret':'SECRET_DO_NOT_EXPOSE','owner_open_id':'ou_creator','brand':'feishu'}

class AssistantTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  self.project=self.root/'existing';self.project.mkdir();atomic_write(self.root/'projects.json',{'existing':str(self.project)})
  self.store=Store(self.root/'bridge.sqlite3',self.root/'projects.json',self.root/'no-state')
  old=self.store.create_connection('旧机器人','existing')
  atomic_write(self.root/'feishu.json',{'app_id':'cli_old','app_secret':'OLD_SECRET','connection_id':old['id'],'connection_key':old['api_key'],'enabled':False,'prompt_template':'OLD {{message}}','allowed_open_ids':[],'allowed_chat_ids':[]})
  self.old=(self.root/'feishu.json').read_bytes();self.old_connections=self.store.connections()
  self.manager=AssistantOnboarding(GroupSettings(self.root/'feishu.json',self.root/'inbox.sqlite3',self.store),planner=lambda *_:dict(PLAN))
  self.launch=patch.object(self.manager,'launch');self.mock_launch=self.launch.start()
  self.receiver=patch.object(self.manager,'wait_for_receiver',return_value=True);self.receiver.start()
 def tearDown(self):self.manager.stop.set();self.receiver.stop();self.launch.stop();self.tmp.cleanup()
 def start(self,**extra):
  payload={'request_id':uuid.uuid4().hex,'requirements':'请把收到的资料整理为飞书文档','audience':'private',**extra}
  j=self.manager.start(payload);return j,payload
 def prepare(self,**extra):
  j,_=self.start(**extra);d=self.manager.directory(j['id']);atomic_write(d/'credentials.json',CREDENTIAL)
  self.manager.update(j['id'],plan=dict(PLAN),name=PLAN['name'],summary=PLAN['summary'],limitations='',profile_saved=True)
  return self.manager.read(j['id'])
 def finish(self,j):
  with patch('feishu_groups.probe',return_value={'bot_ready':True,'bot_open_id':'ou_bot','bot_name':'资料助手'}):self.manager.finish(j['id'])
 def link(self):return self.manager.link_existing({'app_id':CREDENTIAL['app_id'],'project_id':'existing'})
 def test_double_submit_reuses_same_attempt(self):
  j,p=self.start();other=self.manager.start(p);self.assertEqual(j['id'],other['id']);self.assertEqual(self.mock_launch.call_count,1)
  p['requirements']='另一种不同的业务用途'
  with self.assertRaises(BridgeError):self.manager.start(p)
 def test_existing_link_reuses_pending_job_without_changing_old_bot(self):
  payload={'app_id':CREDENTIAL['app_id'],'project_id':'existing'}
  first=self.manager.dispatch('/api/feishu/onboarding/existing','POST',payload)
  again=self.manager.dispatch('/api/feishu/onboarding/existing','POST',payload)
  self.assertEqual(first['id'],again['id']);self.assertEqual(first['kind'],'existing')
  self.assertEqual(self.mock_launch.call_count,1)
  self.assertEqual(first['expected_app_id'],CREDENTIAL['app_id'])
  self.assertEqual(self.manager.list()['assistants'],[])
  self.assertEqual(self.manager.existing_options()['jobs'][0]['id'],first['id'])
  self.assertEqual((self.root/'feishu.json').read_bytes(),self.old)
  self.assertEqual(self.store.connections(),self.old_connections)
 def test_existing_link_requires_existing_app_id_and_project(self):
  for payload in ({},{'app_id':CREDENTIAL['app_id'],'project_id':''},
                  {'app_id':'cli_invalid','project_id':'existing'},
                  {'app_id':CREDENTIAL['app_id'],'project_id':'missing'}):
   with self.assertRaises(BridgeError):self.manager.link_existing(payload)
  self.assertEqual(self.mock_launch.call_count,0)
  self.assertEqual(list(self.manager.root.glob('asst_*/job.json')),[])
 def test_existing_link_blocks_other_running_bridge_receiver(self):
  with tempfile.TemporaryDirectory() as separate:
   other=Path(separate)/'Other Bridge'/'config.json'
   atomic_write(other,{'app_id':CREDENTIAL['app_id'],'enabled':True})
   command=f'python /tmp/feishu_worker.py --config {other} --state-db /tmp/inbox.sqlite3'
   with patch('assistant_onboarding.subprocess.check_output',return_value=command):
    self.assertTrue(other_local_receiver(CREDENTIAL['app_id'],self.root))
    with self.assertRaisesRegex(BridgeError,'另一套 Bridge'):self.link()
  self.assertEqual(self.mock_launch.call_count,0)
 def test_existing_link_rejects_registration_for_other_app(self):
  link=self.link()
  atomic_write(self.manager.directory(link['id'])/'credentials.json',{**CREDENTIAL,'app_id':'cli_bbbbbbbbbbbbbbbb'})
  with self.assertRaisesRegex(BridgeError,'不一致'):self.manager.finish(link['id'])
  self.assertEqual((self.root/'feishu.json').read_bytes(),self.old)
  self.assertEqual(self.store.connections(),self.old_connections)
 def test_existing_link_keeps_existing_bot_and_prompt(self):
  j=self.prepare();self.finish(j)
  ready=self.manager.read(j['id']);_,path,_=self.manager.groups.resolve(ready['app_ref']);before=path.read_bytes()
  link=self.link()
  self.assertEqual(link['phase'],'already_connected')
  self.assertEqual(link['app_ref'],ready['app_ref'])
  self.assertEqual(path.read_bytes(),before)
  self.assertEqual(len(self.store.connections()),len(self.old_connections)+1)
 def test_existing_link_detects_changed_secret_without_touching_connection(self):
  link=self.link()
  with patch('feishu_groups.probe',return_value={'bot_ready':True,'bot_open_id':'ou_bot','bot_name':'已有机器人'}):
   ref=self.manager.groups.save_app({'name':'已有机器人','app_id':CREDENTIAL['app_id'],'app_secret':CREDENTIAL['app_secret'],
    'project_id':'existing','enabled':True,'allowed_open_ids':[],'prompt_template':'TEST {{message}}'})
  _,path,_=self.manager.groups.resolve(ref);before=path.read_bytes()
  atomic_write(self.manager.directory(link['id'])/'credentials.json',{**CREDENTIAL,'app_secret':'DIFFERENT_SECRET'})
  with self.assertRaisesRegex(BridgeError,'不同的密钥'):self.manager.finish(link['id'])
  self.assertEqual(path.read_bytes(),before)
  self.assertEqual(len(self.store.connections()),len(self.old_connections)+1)
 def test_existing_link_saves_new_connection_without_rewriting_old_bot(self):
  link=self.link();directory=self.manager.directory(link['id'])
  atomic_write(directory/'credentials.json',CREDENTIAL)
  self.manager.update(link['id'],profile_saved=True)
  info={'bot_ready':True,'bot_open_id':'ou_bot','bot_name':'已有机器人'}
  with patch('feishu_probe.probe',return_value=info),patch('feishu_groups.probe',return_value=info):
   self.manager.finish(link['id'])
  ready=self.manager.read(link['id']);self.assertEqual(ready['phase'],'ready')
  _,path,_=self.manager.groups.resolve(ready['app_ref']);cfg=json.loads(path.read_text())
  self.assertEqual(cfg['app_name'],'已有机器人')
  self.assertEqual(cfg['allowed_open_ids'],['ou_creator'])
  self.assertEqual(cfg['auto_group_owner_open_id'],'ou_creator')
  self.assertEqual(cfg['prompt_template'].count('{{message}}'),1)
  self.assertEqual(ready['project_id'],'existing')
  self.assertEqual(json.loads((self.root/'projects.json').read_text()),{'existing':str(self.project)})
  self.assertEqual((self.root/'feishu.json').read_bytes(),self.old)
  self.assertEqual(len(self.store.connections()),len(self.old_connections)+1)
 def test_existing_link_builds_generic_prompt_without_business_permissions(self):
  link=self.link();j=self.manager.read(link['id']);j['name']='现有助手'
  prompt=prompt_for_job(j,CREDENTIAL)
  self.assertEqual(prompt.count('{{message}}'),1)
  self.assertIn(j['profile'],prompt)
  self.assertIn('检查实际权限',prompt)
  self.assertNotIn('可申请的基础能力是飞书文档',prompt)
  self.assertEqual((self.root/'feishu.json').read_bytes(),self.old)
 def test_existing_link_can_retry_uncertain_scan_without_claiming_new_creation(self):
  link=self.link();directory=self.manager.directory(link['id'])
  atomic_write(directory/'registration.json',{'status':'needs_check'})
  self.manager.update(link['id'],phase='needs_check')
  self.manager.retry(link['id'],{})
  self.assertEqual(self.manager.read(link['id'])['phase'],'planning')
  self.assertFalse((directory/'registration.json').exists())
 def test_no_parallel_scan_attempt(self):
  self.start()
  with self.assertRaisesRegex(BridgeError,'正在创建'):self.start()
 def test_dispatcher_bootstrap_is_independent_and_idempotent(self):
  from assistant_onboarding import DISPATCHER_PLAN
  first=self.manager.bootstrap_dispatcher();again=self.manager.bootstrap_dispatcher()
  self.assertEqual(first['id'],again['id']);self.assertEqual(first['kind'],'dispatcher');self.assertEqual(first['audience'],'group')
  self.assertEqual(self.manager.read(first['id'])['plan'],DISPATCHER_PLAN);self.assertEqual(self.mock_launch.call_count,1)
  self.assertEqual((self.root/'feishu.json').read_bytes(),self.old)
 def test_dispatcher_finish_waits_for_first_request_without_pairing_code(self):
  from assistant_onboarding import DISPATCHER_PLAN
  j=self.prepare(audience='group');self.manager.update(j['id'],kind='dispatcher',plan=dict(DISPATCHER_PLAN))
  self.finish(j);live=self.manager.read(j['id']);public=self.manager.public(live)
  self.assertEqual(live['phase'],'waiting_group');self.assertTrue(public['auto_group_activation']);self.assertNotIn('pairing_expired',public)
  cfg=json.loads(self.manager.groups.resolve(live['app_ref'])[1].read_text())
  self.assertEqual(cfg['task_dispatcher'],{'assistant_id':j['id'],'owner_open_id':CREDENTIAL['owner_open_id']});self.assertFalse(cfg.get('pairing_code'))
  self.assertEqual((self.root/'feishu.json').read_bytes(),self.old)
 def test_finish_isolated_and_crash_recovery_is_idempotent(self):
  j=self.prepare();self.finish(j);ready=self.manager.read(j['id'])
  self.assertEqual(ready['phase'],'ready');self.assertEqual((self.root/'feishu.json').read_bytes(),self.old)
  self.assertEqual(len(self.store.connections()),len(self.old_connections)+1)
  # Simulate lost final checkpoint after the app was saved.
  self.manager.update(j['id'],phase='connecting',app_ref=None);self.finish(j)
  self.assertEqual(len(self.store.connections()),len(self.old_connections)+1)
  self.assertEqual(self.store.connection(self.old_connections[0]['id']),self.old_connections[0])
  self.assertEqual(json.loads((self.root/'projects.json').read_text())['existing'],str(self.project))
 def test_public_status_never_exposes_secret_or_private_paths(self):
  j=self.prepare();self.finish(j);out=json.dumps(self.manager.public(self.manager.read(j['id'])))
  for secret in [CREDENTIAL['app_secret'],'OLD_SECRET','connection_key','credentials.json',str(self.root)]:self.assertNotIn(secret,out)
 def test_retry_after_bot_rename_keeps_original_workspace(self):
  j=self.prepare();self.finish(j);ready=self.manager.read(j['id']);original=ready['workspace']
  self.manager.update(j['id'],name='更换后的显示名称',phase='connecting');self.finish(j)
  self.assertEqual(self.manager.read(j['id'])['workspace'],original)
  self.assertEqual(len(self.store.connections()),len(self.old_connections)+1)
 def test_prompt_has_one_placeholder_and_correct_identity(self):
  j=self.prepare();j['requirements']='资料处理 {{message}}';j['plan']=checked_plan({**PLAN,'instructions':'创建 {{message}} 文档并读回。'})
  prompt=build_prompt(j,CREDENTIAL);self.assertEqual(prompt.count('{{message}}'),1);self.assertIn(j['profile'],prompt);self.assertIn('ou_creator',prompt)
  group=build_prompt(j,CREDENTIAL,group=True);self.assertNotIn('ou_creator',group);self.assertIn('openchat',group)
 def test_retry_with_saved_credentials_does_not_register_again(self):
  j=self.prepare();self.manager.update(j['id'],phase='failed');self.manager.retry(j['id'],{})
  with patch('assistant_onboarding.subprocess.Popen') as process,patch('feishu_groups.probe',return_value={'bot_ready':True,'bot_open_id':'ou_bot','bot_name':'资料助手'}):
   self.manager.run(j['id']);process.assert_not_called()
  self.assertEqual(self.manager.read(j['id'])['phase'],'ready')
 def test_uncertain_creation_requires_explicit_confirmation(self):
  j,_=self.start();self.manager.update(j['id'],phase='needs_check')
  with self.assertRaises(BridgeError):self.manager.retry(j['id'],{})
  self.manager.retry(j['id'],{'confirmed_not_created':True});self.assertEqual(self.manager.read(j['id'])['phase'],'planning')
 def test_recovered_qr_does_not_automatically_create_another_bot(self):
  j,_=self.start();d=self.manager.directory(j['id']);atomic_write(d/'registration.json',{'status':'waiting_scan','pid':-1})
  with patch('assistant_onboarding.subprocess.Popen') as process:self.manager.run(j['id'],recover=True);process.assert_not_called()
  self.assertEqual(self.manager.read(j['id'])['phase'],'needs_check')
 def test_invalid_job_id_cannot_read_arbitrary_files(self):
  for ident in ['../feishu','../../config.json','asst_bad']:
   with self.assertRaises(BridgeError):self.manager.read(ident)
 def test_existing_project_is_not_replaced(self):
  j=self.prepare(project_id='existing');self.finish(j);self.assertEqual(self.manager.read(j['id'])['project_id'],'existing')
  self.assertEqual(json.loads((self.root/'projects.json').read_text()),{'existing':str(self.project)})
 def test_bot_not_ready_keeps_credentials_for_resume(self):
  j=self.prepare()
  with patch('feishu_groups.probe',return_value={'bot_ready':False}):self.manager.run(j['id'])
  out=self.manager.public(self.manager.read(j['id']));self.assertEqual(out['phase'],'failed');self.assertIn('审批',out['message'])
  self.assertTrue((self.manager.directory(j['id'])/'credentials.json').exists());self.assertEqual((self.root/'feishu.json').read_bytes(),self.old)
 def test_unknown_fields_and_oversized_requirements_rejected(self):
  for extra in [{'app_secret':'no'},{'requirements':'x'*2001},{'audience':'everyone'},{'request_id':'../x'}]:
   with self.assertRaises(BridgeError):self.start(**extra)
 def test_group_creation_waits_for_invitation_without_pairing_code(self):
  j=self.prepare(audience='group');self.finish(j)
  waiting=self.manager.read(j['id']);self.assertEqual(waiting['phase'],'waiting_group')
  value=json.loads(self.manager.groups.resolve(waiting['app_ref'])[1].read_text())
  self.assertEqual(value['auto_group_owner_open_id'],'ou_creator')
  self.assertEqual(value['allowed_chat_ids'],[])
  self.assertFalse(value.get('pairing_code'))
  self.assertTrue(self.manager.public(waiting)['auto_group_activation'])
  self.assertEqual((self.root/'feishu.json').read_bytes(),self.old)

if __name__=='__main__':unittest.main()
