"""Imported first by `unittest discover`: new assistants default to the real
Desktop workspace, so point every test at a throwaway root instead."""
import atexit
import os
import shutil
import tempfile
import unittest

if not os.environ.get("BRIDGE_WORKSPACE_ROOT"):
    _root = tempfile.mkdtemp(prefix="bridge-test-workspace-")
    os.environ["BRIDGE_WORKSPACE_ROOT"] = _root
    atexit.register(shutil.rmtree, _root, True)


class DefaultWorkspaceTests(unittest.TestCase):
    def test_default_root_is_desktop_workspace_and_created(self):
        import claude_runtime
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BRIDGE_WORKSPACE_ROOT", None); os.environ.pop("FEISHU_CONFIG", None)
            root = claude_runtime.assistant_workspace_root(home)
            self.assertEqual(root, claude_runtime.Path(home) / "Desktop" / "Agent Bridge 助手工作区")
            self.assertTrue(root.is_dir())
            first = claude_runtime.new_assistant_folder("资料/助手", home=home)
            self.assertEqual(first, root / "资料-助手")
            (first / "a.txt").write_text("x")
            self.assertEqual(claude_runtime.new_assistant_folder("资料/助手", home=home), root / "资料-助手 2")
            self.assertEqual(claude_runtime.new_assistant_folder("新助手", taken=[str(root / "新助手")], home=home), root / "新助手 2")


if __name__ == "__main__":
    unittest.main()
