"""Migration command inspects first, backs up privately, and never replays work."""
import argparse
import importlib.machinery
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader('prompt_migrate_cli', str(Path(__file__).parent / 'scripts/unify-assistant-prompts'))
cli = loader.load_module()


class MigrationCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.root = Path(self.tmp.name)
        self.path = self.root / 'feishu-apps/app_aaaaaaaaaaaaaaaa/config.json'
        self.path.parent.mkdir(parents=True)
        self.cfg = {'app_id': 'cli_fixture', 'connection_id': 'conn_fixture', 'app_name': 'Fixture',
            'prompt_template': 'PRIVATE {{message}}', 'allowed_chat_ids': ['oc_team'],
            'group_configs': {'oc_team': {'chat_id': 'oc_team', 'prompt_template': 'GROUP {{message}}'}}}
        self.path.write_text(json.dumps(self.cfg))
        self.args = argparse.Namespace(state_dir=str(self.root), bridge_url='http://unused', source_group=[], apply=False)

    def tearDown(self):self.tmp.cleanup()

    def run_cli(self, tasks=None):
        with patch.object(cli.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(tasks or []).encode())):
            return cli.run(self.args)

    def test_readonly_then_apply_private_backup_and_idempotency(self):
        before = self.path.read_bytes()
        self.assertEqual(self.run_cli()['assistants'][0]['state'], 'ready')
        self.assertEqual(self.path.read_bytes(), before)
        self.args.apply = True
        self.assertEqual(self.run_cli()['assistants'][0]['state'], 'migrated')
        actual = json.loads(self.path.read_text())
        self.assertEqual(actual['prompt_template'], 'GROUP {{message}}')
        self.assertEqual(actual['prompt_policy_version'], 1)
        self.assertNotIn('prompt_template', actual['group_configs']['oc_team'])
        backups = list((self.root / 'prompt-policy-backups').glob('*/config.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), self.cfg)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.run_cli()['assistants'][0]['state'], 'already_shared')

    def test_busy_and_pending_receipts_preserved(self):
        self.args.apply = True
        tasks = [{'connection_id': 'conn_fixture', 'status': '执行中'}]
        self.assertEqual(self.run_cli(tasks)['assistants'][0]['state'], 'busy_preserved')
        with sqlite3.connect(self.path.parent / 'inbox.sqlite3') as db:
            db.execute('CREATE TABLE inbound (task_id TEXT)');db.execute('INSERT INTO inbound VALUES(NULL)')
        self.assertEqual(self.run_cli()['assistants'][0]['state'], 'pending_receipt_preserved')
        self.assertEqual(json.loads(self.path.read_text()), self.cfg)

    def test_mixed_inherited_route_requires_choice(self):
        self.cfg['allowed_chat_ids'].append('oc_inherited')
        self.path.write_text(json.dumps(self.cfg))
        self.assertEqual(self.run_cli()['assistants'][0]['state'], 'prompt_source_ambiguous')
        self.args.source_group = ['app_aaaaaaaaaaaaaaaa=oc_team']
        self.assertEqual(self.run_cli()['assistants'][0]['state'], 'ready')


if __name__ == '__main__':unittest.main()
