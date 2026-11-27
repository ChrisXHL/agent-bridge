"""Every assistant task carries the shared autonomy/permission principles."""
import unittest

from assistant_prompt import AUTONOMY_PRINCIPLES, autonomy_principles


class AutonomyPrinciplesTest(unittest.TestCase):
    def test_text_is_complete_and_injectable(self):
        text = autonomy_principles()
        for part in ('默认授权', '自主执行', '最小打扰', '默认工作闭环', '权限边界', '最终交付',
                     '默认自主执行，必要时才询问；主动解决问题，而不是上报问题。'):
            self.assertIn(part, text)
        self.assertNotIn('{{message}}', text)

    def test_every_task_path_injects_it(self):
        import inspect
        import assistant_collaboration, assistant_schedules, feishu_worker, platform_channel, weixin_bridge
        for module in (feishu_worker, assistant_schedules, assistant_collaboration, platform_channel, weixin_bridge):
            self.assertIn('autonomy_principles()', inspect.getsource(module), module.__name__)

    def test_schedule_template_contains_principles(self):
        from assistant_schedules import _task_template
        cfg = {'prompt_template': '做事：{{message}}'}
        self.assertIn(AUTONOMY_PRINCIPLES.strip(), _task_template(cfg, {}, 'p2p', 'oc_x', 'ou_x'))


if __name__ == '__main__':
    unittest.main()
