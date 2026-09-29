"""Natural-language requests are real queued tasks in an isolated engine, never live sends."""
import copy
import json
import unittest
from card_config import default_cards
from codex_http_bridge import BridgeError
import test_multi_cards as multi
import test_workflow_runtime as workflow_tests
definition=workflow_tests.definition
from workflow_contract import validate_workflow,review_fields

class CardAssistantTests(unittest.TestCase):
    setUp=multi.MultiCardTests.setUp
    tearDown=multi.MultiCardTests.tearDown
    req=multi.MultiCardTests.req
    new_adapter=multi.MultiCardTests.new_adapter

    def send(self,**overrides):
        return self.req('POST','/api/feishu/card-assistant',{'chat_id':'oc_a','message':'默认示例音色，素材随音色绑定','request_id':'request_card_1',**overrides})

    def test_dispatch_followup_and_idempotency(self):
        code,first=self.send();self.assertEqual(code,202)
        self.assertEqual(self.send()[1]['task_id'],first['task_id'])
        task=self.store.task(first['task_id']);self.assertEqual(task['connection_id'],self.a['id'])
        self.assertEqual(len(task['messages']),1);self.assertIn('不发送飞书消息',task['messages'][0]['content'])
        self.assertIn('codex-bridge-workflows',task['messages'][0]['content'])
        self.assertEqual(self.send(task_id=first['task_id'],request_id='request_card_2',message='添加修改按钮')[0],409)
        self.store.set_task(first['task_id'],'已完成','已保存，默认示例音色。')
        code,second=self.send(task_id=first['task_id'],request_id='request_card_2',message='添加修改按钮')
        self.assertEqual(second['task_id'],first['task_id'])
        self.assertEqual(len(self.store.task(first['task_id'])['messages']),2)
        rows=self.req('GET','/api/feishu/card-assistant')[1]
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['message'],'添加修改按钮')
        self.assertNotIn('api_key',json.dumps(rows))
        self.assertEqual(self.inbox.pending(),[])

    def test_group_and_task_isolation(self):
        _,a=self.send();self.store.set_task(a['task_id'],'已完成','done')
        self.assertEqual(self.send(chat_id='oc_b',task_id=a['task_id'],request_id='other_req_1')[0],403)
        self.assertEqual(self.send(chat_id='oc_unknown')[0],409)
        business=self.store.submit(self.a,{'message':'business'},'business_0001')[1]
        self.assertEqual(self.send(task_id=business['task_id'],request_id='other_req_2')[0],403)
        self.assertEqual(self.send(task_id={})[0],400)
        self.assertEqual(self.req('POST','/api/feishu/card-assistant',{'chat_id':'oc_a','message':'test','request_id':'req_12345'},origin='https://evil.example')[0],403)

    def test_conflicting_retry_does_not_create_task(self):
        self.send();self.assertEqual(self.send(message='different')[0],409)
        self.assertEqual(len(self.store.tasks()),1)

    def test_native_fields_preview_save_preserves_old_global_card(self):
        _,ctx=self.req('GET','/api/feishu/workflow-context');w=definition()
        w['steps'][1].pop('form_card')
        w['steps'][1]['fields']={'voice':{'type':'choice','required':True,'options':[{'value':'example_voice','data':{'voice':'example_voice','label':'示例音色','material_url':'https://example.feishu.cn/file/Fixture'}}]}}
        args={'revision':ctx['revision'],'group':'群 A','workflow':w,'dry_run':True}
        self.assertEqual(self.req('POST','/api/feishu/configure-workflow',args)[0],200)
        args['dry_run']=False;self.assertEqual(self.req('POST','/api/feishu/configure-workflow',args)[0],200)
        _,after=self.req('GET','/api/feishu/workflow-context')
        self.assertEqual(after['cards'],ctx['cards']);self.assertEqual(after['groups'][1],ctx['groups'][1])
        self.assertEqual(after['groups'][0]['workflow']['workflow'],w)

class InlineReviewTests(unittest.TestCase):
    def test_renderer_and_callback_use_frozen_inline_choices(self):
        fixture=workflow_tests.WorkflowTests();fixture.setUp()
        try:
            w=definition();fields={'voice':{'type':'choice','required':True,'options':[{'value':'bound_voice','data':{'voice':'newvoice','label':'案例音色','material_url':'https://example.com/fixture.wav'}}]}}
            w['steps'][1]['fields']=fields
            validate_workflow(w,default_cards())
            with fixture.inbox.db() as db:db.execute('UPDATE workflow_runs SET definition=? WHERE id=?',(json.dumps(w),fixture.rid))
            fixture.ready()
            card=fixture.sender.cards[0][1];select=card['body']['elements'][1]['elements'][0]
            self.assertEqual(select['options'][0]['text']['content'],'案例音色')
            from test_multi_cards import action
            res=fixture.rt.accept_callback(fixture.config,action('om_card_1',name='workflow_approve',form={'voice':'bound_voice'}))
            self.assertEqual(res,'accepted');fixture.rt.pump(fixture.config,fixture.sender,snapshotter=fixture.snapshot)
            self.assertEqual(fixture.get()['choices']['voice']['material_url'],'https://example.com/fixture.wav')
        finally:fixture.tearDown()

    def test_inline_invalid_type_rejected(self):
        w=definition();w['steps'][1]['fields']={'unsafe':{'type':'object'}}
        with self.assertRaises(BridgeError):validate_workflow(w,default_cards())

if __name__=='__main__':unittest.main()
