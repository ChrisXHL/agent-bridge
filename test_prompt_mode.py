"""Prompt-only mode: no real Feishu sends or real Codex tasks."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_http_bridge import BridgeError
from feishu_settings import public, save, load
from feishu_worker import Inbox, FeishuAdapter
from service import Store, Engine
from test_feishu import FakeBridge, FakeSender, event
from test_service import FakeTransport


class PromptModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        project = self.root/'project'; project.mkdir()
        self.mapping = self.root/'projects.json'
        self.mapping.write_text(json.dumps({'p':str(project)}))
        self.store = Store(self.root/'bridge.sqlite3', self.mapping, self.root/'state.db')
        self.conn = self.store.create_connection('fixture', 'p')
        self.inbox = Inbox(self.root/'feishu-inbox.sqlite3')
        self.config = {'app_id':'cli_fixture', 'connection_id':self.conn['id'],
                       'allowed_open_ids':['ou_owner'], 'allowed_chat_ids':['oc_team'],
                       'bot_open_id':'ou_bot', 'prompt_template':'RULES\n{{message}}'}
        self.bridge = FakeBridge(); self.sender = FakeSender()
        self.adapter = FeishuAdapter(self.config,self.inbox,self.bridge,self.sender)

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_route_and_card_settings_do_not_change_new_prompt(self):
        self.config.update(cards=[{'anything':'old'}],default_card_types=['review'],
            routes=[{'chat_id':'oc_private','prompt_template':'OLD {{message}}','workflow':{'name':'old'}}])
        self.assertTrue(self.adapter.handle_event(event()))
        self.adapter.pump_once()
        self.assertEqual(self.bridge.submissions[0][0],'RULES\n检查项目')

    def test_fresh_inbox_has_no_card_or_workflow_tables(self):
        with self.inbox.db() as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables,{'inbound','pairing'})

    def test_review_marker_is_returned_as_text_without_card(self):
        self.adapter.handle_event(event());self.adapter.pump_once()
        tid=self.inbox.linked_source('om_first')['task_id']
        self.bridge.tasks[tid]={'status':'已完成','result':'FEISHU_REVIEW_DOC_URL: https://example.feishu.cn/docx/fixture'}
        self.adapter.pump_once();self.adapter.pump_once()
        self.assertEqual(len(self.sender.sent),1)
        self.assertIn('FEISHU_REVIEW_DOC_URL',self.sender.sent[0][2])

    def test_new_settings_ignore_but_preserve_private_legacy_config(self):
        path=self.root/'feishu.json'
        old={**self.config,'enabled':False,'app_secret':'fixture-secret',
             'cards':[{'legacy':'preserve exactly'}],'routes':[{'legacy':'preserve exactly'}]}
        path.write_text(json.dumps(old))
        out=save(path,{'prompt_template':'NEW {{message}}'},self.store)
        self.assertNotIn('cards',out);self.assertNotIn('routes',out)
        self.assertEqual(load(path)['cards'],old['cards'])
        self.assertEqual(load(path)['routes'],old['routes'])
        self.assertEqual(load(path)['app_secret'],old['app_secret'])
        with self.assertRaises(BridgeError):save(path,{'routes':[]},self.store)

    def archive_fixture(self):
        _, result=self.store.submit(self.conn,{'message':'historical'},'old-request')
        self.tid=result['task_id']
        self.inbox.accept({'message_id':'om_legacy','chat_id':'oc_private','sender_id':'ou_owner',
            'text':'old','prompt':'old','connection_id':self.conn['id'],'app_id':'cli_fixture','route_json':'{}'})
        self.inbox.mark_submitted('om_legacy',self.tid)
        return self.store.task(self.tid)

    def test_archived_task_is_not_dispatched_or_mutated(self):
        before=self.archive_fixture()
        fake=FakeTransport();engine=Engine(self.store,fake)
        engine.tick();engine.tick()
        self.assertEqual(self.store.task(self.tid),before)
        self.assertEqual(fake.created,0)
        self.assertTrue(before['archived'])

    def test_old_inbox_rows_are_not_submitted_or_notified(self):
        self.archive_fixture()
        self.inbox.accept({'message_id':'wf_old','chat_id':'oc_private','sender_id':'ou_owner',
            'text':'old stage','prompt':'old stage','route_json':'{}'})
        with self.inbox.db() as db:before=[tuple(r) for r in db.execute('SELECT * FROM inbound')]
        self.adapter.pump_once()
        self.assertEqual(self.bridge.submissions,[]);self.assertEqual(self.sender.sent,[])
        with self.inbox.db() as db:self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM inbound')],before)

    def test_archived_followups_cannot_replay_old_task(self):
        self.archive_fixture()
        self.assertFalse(self.adapter.handle_event(event('om_followup',parent='om_legacy')))
        with self.assertRaises(BridgeError) as error:
            self.store.submit(self.conn,{'task_id':self.tid,'message':'continue'},'new-request')
        self.assertEqual(error.exception.code,'historical_task')

    def test_completed_legacy_workflow_status_is_read_without_rewriting_task(self):
        self.archive_fixture()
        with self.inbox.db() as db:
            db.execute('CREATE TABLE workflow_runs (task_id TEXT,state TEXT,definition TEXT)')
            db.execute('INSERT INTO workflow_runs VALUES (?,?,?)',(self.tid,'completed','{"name":"Old flow"}'))
        task=self.store.task(self.tid)
        self.assertEqual(task['status'],'等待 Codex')
        self.assertEqual(task['display_status'],'已完成')
        self.assertEqual(task['archive_name'],'Old flow')

    def test_missing_private_project_mapping_is_supported(self):
        self.mapping.unlink()
        with patch.object(self.store,'_codex_projects',return_value=[]):
            self.assertEqual(self.store.projects(),[])

    def test_tool_list_contains_no_card_or_workflow_actions(self):
        import mcp_server
        self.assertEqual({t['name'] for t in mcp_server.TOOLS},
            {'bridge_panel','bridge_list_tasks','bridge_list_connections','bridge_feishu_setup_status'})
