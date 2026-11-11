import unittest
from unittest.mock import patch
from codex_runtime import resolve_codex_binary

class RuntimeTests(unittest.TestCase):
    def test_desktop_precedes_standalone(self):
        desktop='/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
        with patch('codex_runtime.Path.is_file',return_value=True),patch('codex_runtime.os.access',return_value=True):
            self.assertEqual(resolve_codex_binary(),desktop)
    def test_explicit_override_is_preserved(self):
        self.assertEqual(resolve_codex_binary('/custom/codex'),'/custom/codex')
    def test_cli_fallback_without_desktop(self):
        with patch('codex_runtime.Path.is_file',return_value=False),patch('codex_runtime.shutil.which',return_value='/opt/bin/codex'):
            self.assertEqual(resolve_codex_binary(),'/opt/bin/codex')
