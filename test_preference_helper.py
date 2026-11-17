import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


class PreferenceHelperTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('preference_helper', Path(__file__).parent / 'scripts/personal-preference.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.args = ['personal-preference', '--source-id', 'om_test_preference', '--token', 'a' * 64]

    def test_uses_actual_loopback_and_no_user_identity_override(self):
        args = self.args + ['--bridge-url', 'http://127.0.0.1:19876', '--action', 'set', '--key', 'verbosity', '--value', 'concise']
        with patch.object(sys, 'argv', args), patch.object(self.module.request, 'urlopen', return_value=io.BytesIO(b'{"updated":true}')) as call, patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(self.module.main(), 0)
            req = call.call_args.args[0]
            self.assertEqual(req.full_url, 'http://127.0.0.1:19876/api/feishu/personal-preferences')
            self.assertEqual(set(json.loads(req.data)), {'source_id', 'token', 'action', 'key', 'value'})

    def test_rejects_remote_url_and_invalid_action_shape_before_network(self):
        for extra in (['--bridge-url', 'http://example.com:8797', '--action', 'list'],
                      ['--action', 'set', '--key', 'verbosity'],
                      ['--action', 'clear', '--key', 'format'],
                      ['--action', 'delete', '--key', 'format', '--value', 'table']):
            with self.subTest(extra=extra), patch.object(sys, 'argv', self.args + extra), patch.object(self.module.request, 'urlopen') as call, patch('sys.stderr', new_callable=io.StringIO):
                with self.assertRaises(SystemExit):self.module.main()
                call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
