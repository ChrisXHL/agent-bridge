import unittest
from unittest.mock import patch
import mcp_server

class ManagerShortcutStatusTests(unittest.TestCase):
 def status(self,phase=None,value=None):
  manager={'kind':'manager','phase':phase,'name':'助手管理员'} if phase else None
  def fetch(path):return {'assistants':[manager] if manager else []} if path.endswith('onboarding') else value or {}
  with patch('mcp_server.fetch',side_effect=fetch):return mcp_server.feishu_setup_status()
 def test_new_installation_uses_shortcut_without_manual_credentials(self):
  result=self.status();self.assertIn('创建助手管理员',result['next_step']);self.assertNotIn('填写',result['next_step']);self.assertEqual(result['assistant_manager']['status'],'not_created')
 def test_existing_manager_opens_and_removed_manager_restores(self):
  self.assertIn('打开助手管理员',self.status('ready')['next_step']);self.assertIn('恢复助手管理员',self.status('removed')['next_step'])
 def test_interrupted_registration_resumes_same_attempt(self):
  self.assertIn('扫码',self.status('waiting_scan')['next_step']);self.assertIn('继续配置',self.status('failed')['next_step']);self.assertIn('核对',self.status('needs_check')['next_step'])

if __name__=='__main__':unittest.main()
