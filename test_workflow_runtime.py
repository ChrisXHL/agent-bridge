"""Isolated durable workflow tests: real SQLite, fake external APIs, no Feishu sends."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from card_config import default_cards
from codex_http_bridge import BridgeError
from feishu_worker import Inbox
from workflow_contract import validate_workflow
from workflow_evidence import capture
from test_multi_cards import Sender, action

DOC='https://example.feishu.cn/docx/Fixture'
XML='<fcpxml version="1.10"><library><event><project><sequence duration="100/25s"><spine><title offset="0s" duration="25/25s" name="你好"><text><text-style ref="ts1">你好</text-style></text></title></spine></sequence></project></event></library></fcpxml>'

def definition():
    return {'name':'fixture','steps':[
      {'id':'write','name':'写稿','kind':'agent','needs':[],'prompt':'write','produces':{'original':{'kind':'file'},'doc':{'kind':'feishu_document'}},'next':'review'},
      {'id':'review','name':'审稿','kind':'review','needs':['doc'],'form_card':'voice_review','next':'align','revise':'revise'},
      {'id':'revise','name':'修改','kind':'agent','needs':['original','doc'],'prompt':'revise','produces':{'doc':{'kind':'feishu_document'}},'next':'review'},
      {'id':'align','name':'对齐','kind':'agent','needs':['original','doc'],'prompt':'align','produces':{'xml':{'kind':'fcpxml','reference':'original'}},'next':'finish'},
      {'id':'finish','name':'交付','kind':'agent','needs':['xml','doc'],'prompt':'deliver','produces':{'receipt':{'kind':'delivery'}}}]}

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.inbox=Inbox(self.root/'inbox.db');self.rt=self.inbox.workflows
        self.config={'app_id':'cli_test','allowed_open_ids':['ou_owner'],'allowed_chat_ids':['oc_a'],'cards':default_cards()}
        self.source={'message_id':'om_root','chat_id':'oc_a','sender_id':'ou_owner','text':'original request','prompt':'initial','connection_id':'conn','app_id':'cli_test','chat_type':'group','route_json':'{}'}
        self.inbox.accept(self.source);self.rid=self.rt.start(self.source,definition(),self.config)
        self.inbox.mark_submitted('om_root','task_original');self.sender=Sender()
        self.doc={'url':DOC,'document':{'revision_id':1,'content':'English'},'unresolved_comments':[]}
        self.original=self.root/'source.fcpxml';self.original.write_text(XML)
    def tearDown(self):self.tmp.cleanup()
    def snapshot(self,url):return copy.deepcopy(self.doc)
    def get(self):return self.rt.get(self.rid)
    def act(self,action,**kw):return self.rt.act({'run_id':self.rid,'revision':self.get()['revision'],'action':action,**kw},snapshotter=self.snapshot,receipt_reader=lambda v:{'verified':True})
    def ready(self):
        self.act('complete',outputs={'original':{'path':str(self.original)},'doc':{'url':DOC}})
        self.rt.pump(self.config,self.sender,snapshotter=self.snapshot)
    def click(self,decision='approve',mid='om_card_1',user='ou_owner'):
        choice=default_cards()[0]['actions'][0]['fields']['voice']['options'][0]['value']
        return self.rt.accept_callback(self.config,action(mid,name='workflow_'+decision,user=user,form={'voice':choice}))
    def verified(self,decision='approve'):
        self.assertEqual(self.click(decision),'accepted');self.rt.pump(self.config,self.sender,snapshotter=self.snapshot)
    def test_multistage_same_task_and_real_evidence(self):
        self.ready();self.assertEqual(self.get()['state'],'waiting_review')
        self.assertEqual(self.get()['task_id'],'task_original')
        self.assertIn('Fixture',self.sender.cards[0][1]['body']['elements'][0]['content'])
        self.verified();run=self.get();self.assertEqual(run['step_id'],'align');self.assertEqual(run['decision']['form']['voice']['voice'],'example_voice')
        pending=self.inbox.pending()[0];self.assertEqual(self.inbox.original_source(pending)['task_id'],'task_original')
        self.assertTrue(self.rt.dispatch_allowed(pending))
        translated=self.root/'en.fcpxml';translated.write_text(XML.replace('你好','Hello'))
        self.act('complete',outputs={'xml':{'path':str(translated)}})
        self.act('complete',outputs={'receipt':{'message_ids':['om_delivered'],'file_keys':['file_a']}})
        self.assertEqual(self.get()['state'],'completed')
        self.assertFalse(self.rt.dispatch_allowed(pending))
    def test_revision_loop_comments_and_frozen_config(self):
        self.ready();self.config['cards'][0]['actions'][0]['fields']['voice']['options'][0]['data']['voice']='changed'
        self.doc['unresolved_comments']=[{'comment_id':'comment','text':'change this'}]
        self.verified('revise');self.assertEqual(self.get()['step_id'],'revise')
        self.assertEqual(self.get()['decision']['form']['voice']['voice'],'example_voice')
        self.doc['document']['revision_id']=2
        self.act('complete',outputs={'doc':{'url':DOC}});self.rt.pump(self.config,self.sender,snapshotter=self.snapshot)
        self.assertEqual(self.click(mid='om_card_1'),'already_submitted')
        self.assertEqual(self.click(mid='om_card_2'),'accepted');self.rt.pump(self.config,self.sender,snapshotter=self.snapshot)
        self.assertEqual(self.get()['step_id'],'align')
    def test_changed_doc_blocks_approval_retry_refreshes(self):
        self.ready();self.doc['document']['content']='edited'
        self.verified();self.assertEqual(self.get()['state'],'blocked')
        self.act('retry');self.rt.pump(self.config,self.sender,snapshotter=self.snapshot)
        self.assertEqual(self.click(mid='om_card_1'),'stale_review')
        self.assertEqual(self.click(mid='om_card_2'),'accepted');self.rt.pump(self.config,self.sender,snapshotter=self.snapshot)
        self.assertEqual(self.get()['step_id'],'align')
    def test_duplicate_and_unauthorized_click(self):
        self.ready();self.assertEqual(self.click(user='ou_stranger'),'ignored')
        self.assertEqual(self.click(),'accepted');self.assertEqual(self.click(),'already_submitted')
        self.rt.pump(self.config,self.sender,snapshotter=self.snapshot)
        self.assertEqual(len(self.inbox.pending()),1)
    def test_restart_preserves_run_artifacts_and_config(self):
        self.ready();old=self.get();self.original.write_text('changed original on disk')
        again=Inbox(self.root/'inbox.db').workflows.get(self.rid)
        self.assertEqual(old,again);self.assertEqual(Path(again['artifacts']['original']['path']).read_text(),XML)
    def test_changed_timing_rejected(self):
        self.ready();self.verified();p=self.root/'wrong.fcpxml';p.write_text(XML.replace('25/25s','30/25s'))
        with self.assertRaises(BridgeError):self.act('complete',outputs={'xml':{'path':str(p)}})
        self.assertEqual(self.get()['step_id'],'align')
    def test_owned_artifact_tampering_blocks(self):
        self.ready();Path(self.get()['artifacts']['original']['path']).write_text('bad')
        self.verified();self.assertEqual(self.get()['state'],'blocked')
    def test_stale_revision_fenced(self):
        old=self.get()['revision'];self.act('fail',reason='test')
        with self.assertRaises(BridgeError):self.rt.act({'run_id':self.rid,'revision':old,'action':'cancel'})
    def test_effect_survives_retry_and_cannot_duplicate(self):
        e=self.act('effect_begin',key='clone',parameters={'model':'fixture'})
        self.assertEqual(e['state'],'claimed');self.assertEqual(self.act('effect_begin',key='clone',parameters={'model':'fixture'})['state'],'started')
        self.act('fail',reason='interrupted');self.act('retry')
        with self.assertRaises(BridgeError):self.act('complete',outputs={'original':{'path':str(self.original)},'doc':{'url':DOC}})
        self.act('effect_finish',effect_id=e['effect_id'],state='succeeded',result={'provider_id':'verified-fixture'})
        self.assertEqual(self.act('effect_begin',key='clone',parameters={'model':'fixture'})['state'],'succeeded')
    def test_cancellation_fences_queued_stages(self):
        self.ready();self.verified();pending=self.inbox.pending()[0];self.act('cancel')
        self.assertFalse(self.rt.dispatch_allowed(pending));self.assertEqual(self.get()['state'],'cancelled')
    def test_ambiguous_send_not_retried(self):
        self.act('complete',outputs={'original':{'path':str(self.original)},'doc':{'url':DOC}})
        class BadSender:
            def reply_card(*args):raise OSError('ambiguous')
        self.rt.pump(self.config,BadSender(),snapshotter=self.snapshot)
        with self.assertRaises(BridgeError):self.act('retry')
        self.assertEqual(self.get()['state'],'blocked')
    def test_remote_change_blocks_paid_effect(self):
        self.ready();self.verified();self.doc['document']['content']='late edit'
        with self.assertRaises(BridgeError):self.act('effect_begin',key='tts',inputs=['doc'])
    def test_graph_validation_and_bundled_example(self):
        w=definition();w['steps'][-1]['next']='write'
        with self.assertRaises(BridgeError):validate_workflow(w,self.config['cards'])
        w=definition();w['steps'][2]['needs'].append('xml')
        with self.assertRaises(BridgeError):validate_workflow(w,self.config['cards'])
        validate_workflow(json.loads(Path('examples/subtitle-workflow.json').read_text()),self.config['cards'])
    def test_delivery_requires_real_reader(self):
        with self.assertRaises(BridgeError):capture({'kind':'delivery'},{'message_ids':['om_fake'],'file_keys':['file_fake']},self.root/'receipt',{})

    def test_reopen_review_invalidates_downstream_and_preserves_voice(self):
        self.ready();self.verified();p=self.root/'en.fcpxml';p.write_text(XML.replace('你好','Hi'))
        self.act('complete',outputs={'xml':{'path':str(p)}})
        self.assertIn('xml',self.get()['artifacts'])
        self.act('reopen_review',stage_id='review')
        self.assertNotIn('xml',self.get()['artifacts']);self.assertEqual(self.get()['state'],'waiting_review')
        self.assertEqual(self.get()['choices']['voice']['voice'],'example_voice')

    def test_cannot_jump_to_unvisited_review(self):
        with self.assertRaises(BridgeError):self.act('reopen_review',stage_id='review')

    def test_callback_ack_does_not_fetch_external_document(self):
        self.ready()
        from unittest.mock import patch
        with patch('workflow_runtime.document_snapshot',side_effect=AssertionError('must be async')):
            self.assertEqual(self.click(),'accepted')
        self.assertEqual(self.get()['state'],'checking_review')

if __name__=='__main__':unittest.main()

class AdapterWorkflowTests(unittest.TestCase):
    # Reuse the isolated HTTP-server fixture, not its test methods.
    from test_multi_cards import MultiCardTests as Fixture
    setUp=Fixture.setUp
    tearDown=Fixture.tearDown
    req=Fixture.req
    new_adapter=Fixture.new_adapter
    source=Fixture.source

    def configure(self):
        status,ctx=self.req('GET','/api/feishu/workflow-context');self.assertEqual(status,200)
        args={'revision':ctx['revision'],'group':'群 A','workflow':definition(),'dry_run':True}
        self.assertEqual(self.req('POST','/api/feishu/configure-workflow',args)[1]['status'],'preview')
        args['dry_run']=False
        self.assertEqual(self.req('POST','/api/feishu/configure-workflow',args)[1]['status'],'saved')
        self.adapter=self.new_adapter()

    def test_http_config_new_run_reuses_original_bridge_task(self):
        self.configure();source=self.source();rt=self.inbox.workflows;rid=rt.for_source(source)
        self.assertIn('只执行当前阶段',source['prompt'])
        self.assertEqual(self.req('GET','/api/feishu/workflow-runs/'+rid)[1]['task_id'],source['task_id'])
        doc={'document':{'content':'fixture','revision_id':1},'unresolved_comments':[]};snapshot=lambda _:copy.deepcopy(doc)
        p=self.root/'original.fcpxml';p.write_text(XML)
        rt.act({'run_id':rid,'revision':1,'action':'complete','outputs':{'original':{'path':str(p)},'doc':{'url':DOC}}},snapshotter=snapshot)
        self.store.set_task(source['task_id'],'已完成','translated');self.adapter.pump_once()
        self.assertEqual(len(self.sender.cards),1)
        choice=default_cards()[0]['actions'][0]['fields']['voice']['options'][0]['value']
        self.assertEqual(self.adapter.handle_card_action(action('om_card_1',name='workflow_approve',form={'voice':choice})),'accepted')
        rt.pump(self.adapter.config,self.sender,snapshotter=snapshot)
        self.adapter.pump_once()
        pending=[x for x in self.inbox.submitted() if x['message_id'].startswith('wf_')][0]
        self.assertEqual(pending['task_id'],source['task_id'])
        self.assertEqual(len(self.store.task(source['task_id'])['messages']),2)
        self.assertEqual(rt.get(rid)['step_id'],'align')
        self.assertEqual(self.req('POST','/api/feishu/workflow-run',{'run_id':rid,'revision':1,'action':'cancel'})[0],409)
        self.assertEqual(self.req('POST','/api/feishu/workflow-run',[])[0],400)

    def test_task_finish_without_artifacts_is_blocked(self):
        self.configure();source=self.source();self.store.set_task(source['task_id'],'已完成','I am done')
        self.adapter.pump_once();rid=self.inbox.workflows.for_source(source)
        self.assertEqual(self.inbox.workflows.get(rid)['state'],'blocked')
        self.assertEqual(len(self.sender.cards),0)
        self.assertIn('没有提交',self.sender.texts[-1][2])

    def test_old_task_stays_legacy_after_configuration(self):
        source=self.source();self.configure();self.store.set_task(source['task_id'],'已完成','FEISHU_REVIEW_DOC_URL: '+DOC)
        self.adapter.pump_once();self.assertIsNone(self.inbox.workflows.for_source(source))
        self.assertEqual(self.sender.cards[0][1]['type'],'template')
