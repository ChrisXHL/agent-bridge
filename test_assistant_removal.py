"""Archive/restore with isolated fixtures; no real app is removed."""
import json
import unittest
from unittest.mock import patch
import test_assistant_onboarding as fixtures
from assistant_onboarding import AssistantOnboarding
from codex_http_bridge import BridgeError
from feishu_groups import atomic_write

class RemovalTests(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.AssistantTests();self.f.setUp();self.o=self.f.manager
  self.receiver=patch.object(self.o,'wait_for_receiver',return_value=True);self.receiver.start()
  j=self.f.prepare();self.f.finish(j);self.j=self.o.read(j['id'])
  _,self.path,_=self.o.groups.resolve(self.j['app_ref'])
  self.before=json.loads(self.path.read_text());self.cid=self.before['connection_id']
  self.status=self.f.store.connection(self.cid)['status']
 def tearDown(self):self.receiver.stop();self.f.tearDown()
 def remove(self):return self.o.lifecycle(self.j['id'],'remove')
 def restore(self):return self.o.lifecycle(self.j['id'],'restore')
 def test_remove_restore_keeps_history_credentials_and_original_apps(self):
  conn=self.f.store.connection(self.cid)
  task=self.f.store.submit(conn,{'message':'历史任务'},'archive-history-test')[1]
  self.f.store.set_task(task['task_id'],'已完成','历史结果')
  original_task=self.f.store.task(task['task_id'])
  self.assertEqual(self.remove()['phase'],'removed')
  self.assertFalse(json.loads(self.path.read_text())['enabled'])
  self.assertEqual(self.f.store.connection(self.cid)['status'],'已停用')
  self.assertFalse(any(a['ref']==self.j['app_ref'] for a in self.o.groups.catalog()['apps']))
  self.assertEqual((self.f.root/'feishu.json').read_bytes(),self.f.old)
  self.assertEqual(self.f.store.task(task['task_id']),original_task)
  self.assertTrue((self.o.directory(self.j['id'])/'credentials.json').exists())
  self.assertEqual(self.restore()['phase'],'ready')
  self.assertEqual(json.loads(self.path.read_text()),self.before)
  self.assertEqual(self.f.store.connection(self.cid)['status'],self.status)
 def test_repeated_actions_are_idempotent_and_archived_job_never_launches(self):
  self.remove();self.remove();self.f.mock_launch.reset_mock();self.o.list();self.f.mock_launch.assert_not_called()
  self.restore();self.restore()
  self.assertEqual(json.loads(self.path.read_text()),self.before)
 def test_busy_task_prevents_any_change(self):
  self.f.store.submit(self.f.store.connection(self.cid),{'message':'执行任务'},'archive-busy-test')
  with self.assertRaises(BridgeError) as e:self.remove()
  self.assertEqual(e.exception.code,'assistant_busy')
  self.assertEqual(json.loads(self.path.read_text()),self.before)
  self.assertEqual(self.o.read(self.j['id'])['phase'],'ready')
 def test_authorization_control_receipt_does_not_block_removal(self):
  from feishu_worker import Inbox
  _,_,inbox=self.o.groups.resolve(self.j['app_ref'])
  Inbox(inbox).accept({'message_id':'om_auth','chat_id':'oc_private','sender_id':'ou_creator','text':'确认协作 1234ABCD','prompt':'','connection_id':self.cid,'app_id':self.before['app_id'],'chat_type':'p2p','route_json':'{"mode":"assistant_authorization"}'})
  self.assertEqual(self.remove()['phase'],'removed')
 def test_shared_connection_blocks_removal(self):
  p=self.f.root/'feishu.json';old=json.loads(p.read_text());old['connection_id']=self.cid;atomic_write(p,old)
  with self.assertRaises(BridgeError) as e:self.remove()
  self.assertEqual(e.exception.code,'shared_connection')
  self.assertEqual(json.loads(self.path.read_text()),self.before)
 def test_restore_preserves_original_disabled_state_and_other_edits(self):
  cfg=dict(self.before);cfg['enabled']=False;atomic_write(self.path,cfg)
  self.f.store.update_connection(self.cid,{'status':'已停用'})
  self.remove();cfg=json.loads(self.path.read_text());cfg['prompt_template']='后续修改 {{message}}';atomic_write(self.path,cfg)
  self.restore();after=json.loads(self.path.read_text())
  self.assertFalse(after['enabled']);self.assertEqual(after['prompt_template'],'后续修改 {{message}}')
  self.assertEqual(self.f.store.connection(self.cid)['status'],'已停用')
 def test_restart_completes_interrupted_archive_and_restore(self):
  real=self.o.update
  def crash(ident,**kw):
   if kw.get('phase') in ('removed','ready'):raise RuntimeError('crash before final checkpoint')
   return real(ident,**kw)
  with patch.object(self.o,'update',side_effect=crash):
   with self.assertRaises(RuntimeError):self.remove()
  fresh=AssistantOnboarding(self.o.groups);fresh.list()
  self.assertEqual(fresh.read(self.j['id'])['phase'],'removed')
  with patch.object(self.o,'update',side_effect=crash):
   with self.assertRaises(RuntimeError):self.restore()
  fresh.list();self.assertEqual(fresh.read(self.j['id'])['phase'],'ready')
  self.assertEqual(json.loads(self.path.read_text()),self.before)
 def test_api_validates_payload_and_does_not_expose_checkpoint(self):
  url='/api/feishu/onboarding/'+self.j['id']+'/remove'
  with self.assertRaises(BridgeError):self.o.dispatch(url,'POST',{'app_ref':'default'})
  out=self.o.dispatch(url,'POST',{})
  self.assertNotIn('archive_snapshot',out);self.assertNotIn('SECRET',json.dumps(out))
  with self.assertRaises(BridgeError):self.o.retry(self.j['id'],{})

if __name__=='__main__':unittest.main()
