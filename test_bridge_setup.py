import importlib.util
import json
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import bridge_setup as setup


class SetupTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'package';self.root.mkdir();self.state=Path(self.temp.name)/'private'
 def tearDown(self):self.temp.cleanup()
 def ready(self,mode='stopped'):
  return {'checks':{k:True for k in ('platform','python','python_dependencies','node','npm','node_dependencies','lark_cli','codex','codex_login')},'missing':[],'runtime':{'state':mode},'stage':'needs_agent'}
 def test_doctor_does_not_create_state_or_claim_message_delivery(self):
  with patch.object(setup,'probe',return_value=False),patch.object(setup,'runtime',return_value={'state':'stopped'}):
   value=setup.doctor(self.root,self.state,18797)
  self.assertFalse(self.state.exists());self.assertFalse(any(value['acceptance'].values()));self.assertEqual(value['execution_backend'],'Codex')
 def test_port_conflict_is_not_restarted_or_written(self):
  value=self.ready('other_installation')
  with patch.object(setup,'doctor',return_value=value),patch.object(setup,'run_logged') as run:
   self.assertEqual(setup.install(self.root,self.state,18797),value);run.assert_not_called()
  self.assertFalse((self.state/'assistant-collaboration/policy.json').exists())
 def test_failed_prepare_keeps_private_log_and_can_retry_without_recreating_bot(self):
  value=self.ready();value['checks']['python_dependencies']=False
  with patch.object(setup,'doctor',return_value=value),patch.object(setup,'run_logged',side_effect=RuntimeError('sensitive raw output')):
   result=setup.prepare(self.root,self.state)
  self.assertNotIn('sensitive',json.dumps(result));self.assertEqual(result['next_action'],'inspect_private_setup_log_then_retry_same_command')
  self.assertEqual((self.state/'setup.json').stat().st_mode&0o777,0o600)
 def test_install_creates_open_defaults_only_for_fresh_state_and_repeats_safely(self):
  home=Path(self.temp.name)/'home';root=self.root;state=self.state
  with patch.object(setup,'doctor',return_value=self.ready()),patch.object(setup,'runtime',return_value={'state':'running'}),patch.object(setup,'run_logged'),patch.object(setup,'probe',return_value=False),patch.object(setup.Path,'home',return_value=home):
   setup.install(root,state,18797)
   policy=state/'assistant-collaboration/policy.json';self.assertEqual(json.loads(policy.read_text()),{'mode':'open'})
   policy.write_text('{"mode":"confirm"}')
   setup.install(root,state,18797);self.assertEqual(json.loads(policy.read_text()),{'mode':'confirm'})
   plist=next((home/'Library/LaunchAgents').glob('*.plist'));value=plistlib.loads(plist.read_bytes())
   self.assertEqual(value['WorkingDirectory'],str(root.resolve()));self.assertIn(str(root/'.tools/node_modules/.bin'),value['EnvironmentVariables']['PATH'])
 def test_existing_state_without_policy_does_not_get_open_access(self):
  self.state.mkdir();(self.state/'bridge.sqlite3').touch()
  with patch.object(setup,'doctor',return_value=self.ready()),patch.object(setup,'runtime',return_value={'state':'running'}),patch.object(setup,'run_logged'),patch.object(setup,'probe',return_value=False),patch.object(setup.Path,'home',return_value=Path(self.temp.name)/'home'):
   setup.install(self.root,self.state,18797)
  self.assertFalse((self.state/'assistant-collaboration/policy.json').exists())
 def test_manager_never_posts_to_another_installation(self):
  with patch.object(setup,'get',return_value={'product':'codex-bridge','source_root':'/different-copy'}) as get:
   self.assertEqual(setup.manager()['stage'],'other_installation');self.assertEqual(get.call_count,1)
 def test_ready_manager_returns_use_action_instead_of_polling_forever(self):
  instance={'product':'codex-bridge','source_root':str(setup.ROOT.resolve()),'state_dir':str(setup.DEFAULT_STATE.resolve())}
  with patch.object(setup,'get',side_effect=[instance,{'phase':'ready','id':'asst_test'}]):
   self.assertEqual(setup.manager()['next_action'],'open_existing_manager_in_feishu')
 def test_unsupported_platform_never_installs(self):
  with patch.object(setup.platform,'system',return_value='Linux'),patch.object(setup,'doctor',return_value={'stage':'unsupported_platform'}),patch.object(setup,'run_logged') as run:
   self.assertEqual(setup.install(self.root,self.state)['stage'],'unsupported_platform');run.assert_not_called()
  self.assertFalse(self.state.exists())


class PackageTests(unittest.TestCase):
 def test_archive_is_allowlisted_and_refuses_private_identifiers(self):
  spec=importlib.util.spec_from_file_location('share',Path(__file__).parent/'scripts/build-share.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
  import zipfile
  with tempfile.TemporaryDirectory() as t:
   result=module.build(Path(t)/'delivery.zip')
   with zipfile.ZipFile(result['path']) as z:
    names=z.namelist();self.assertFalse(any('/artifacts/' in n or '/data/' in n or '/.venv/' in n or n.endswith('/projects.json') for n in names))
    self.assertTrue(any(n.endswith('/bridge_setup.py') for n in names));self.assertTrue(any(n.endswith('/AGENTS.md') for n in names))
    manifest=json.loads(z.read(next(n for n in names if n.endswith('/PACKAGE-MANIFEST.json'))));self.assertFalse(manifest['runtime_state_included'])


if __name__=='__main__':unittest.main()
