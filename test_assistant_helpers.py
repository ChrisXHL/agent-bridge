"""Helper scripts keep the receiver's loopback instance and structured errors."""
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


class HelperTests(unittest.TestCase):
    def modules(self):
        for name in ('update-assistant-self', 'assistant-schedule'):
            spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / 'scripts' / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            yield name, module

    def args(self, name):
        args = [name, '--source-id', 'om_test_source', '--token', 'a' * 64]
        return args + (['--rule', '以后附上证据来源。'] if name == 'update-assistant-self' else ['--action', 'list'])

    def test_custom_loopback_port_and_success(self):
        for name, module in self.modules():
            with self.subTest(name=name):
                response = io.BytesIO(b'{"ok":true}')
                with patch.object(sys, 'argv', self.args(name) + ['--bridge-url', 'http://127.0.0.1:19876/']), patch.object(module.request, 'urlopen', return_value=response) as send, patch('sys.stdout', new_callable=io.StringIO) as output:
                    self.assertEqual(module.main(), 0)
                    self.assertEqual(json.loads(output.getvalue()), {'ok': True})
                    self.assertTrue(send.call_args.args[0].full_url.startswith('http://127.0.0.1:19876/api/'))

    def test_rejects_remote_credentials_paths_and_invalid_port(self):
        for name, module in self.modules():
            for url in ('https://127.0.0.1:8797', 'http://example.com:8797', 'http://127.0.0.1:8797/path', 'http://user@127.0.0.1:8797', 'http://127.0.0.1:99999', 'http://127.0.0.1:8797?x=1'):
                with self.subTest(name=name, url=url), patch.object(sys, 'argv', self.args(name) + ['--bridge-url', url]), patch.object(module.request, 'urlopen') as send, patch('sys.stderr', new_callable=io.StringIO):
                    with self.assertRaises(SystemExit) as error:
                        module.main()
                    self.assertEqual(error.exception.code, 2)
                    send.assert_not_called()

    def test_structured_error_preserves_code_without_token(self):
        for name, module in self.modules():
            body = io.BytesIO(json.dumps({'error': {'code': 'source_ambiguous', 'message': '来源存在歧义'}}).encode())
            failure = HTTPError('http://127.0.0.1:8797', 409, 'conflict', {}, body)
            with self.subTest(name=name), patch.object(sys, 'argv', self.args(name)), patch.object(module.request, 'urlopen', side_effect=failure), patch('sys.stderr', new_callable=io.StringIO) as output:
                self.assertEqual(module.main(), 1)
                self.assertEqual(json.loads(output.getvalue())['error']['code'], 'source_ambiguous')
                self.assertNotIn('a' * 64, output.getvalue())


if __name__ == '__main__':
    unittest.main()
