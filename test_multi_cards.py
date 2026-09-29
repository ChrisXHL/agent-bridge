"""Two-project routing and two-card callback flows using a real isolated Bridge HTTP server."""
import copy
import json
import tempfile
import threading
import unittest
import http.client
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from card_config import default_cards,validate_cards
from codex_http_bridge import BridgeError
from feishu_settings import save,load,public
from feishu_worker import Inbox,FeishuAdapter,BridgeClient
from service import Store,make_handler

DOC='https://example.feishu.cn/docx/TestDocument'
VIDEO={'id':'video_review','name':'视频确认','template_id':'TestTemplate','version':'1.0.0',
       'variables':{'video_url':{'type':'url','required':True}},
       'actions':[{'name':'approve','label':'通过','fields':{'note':{'type':'text'}},
                   'prompt_template':'继续发布：{{variables.video_url}}；意见：{{form.note}}'},
                  {'name':'reject','label':'退回','fields':{},'prompt_template':'请修改：{{variables.video_url}}'}]}
class Sender:
    def __init__(self):self.cards=[];self.texts=[]
    def reply_card(self,source,content,request_id):
        self.cards.append((source,json.loads(content),request_id));return 'om_card_'+str(len(self.cards))
    def reply(self,source,status,body):
        self.texts.append((source,status,body));return 'om_text_'+str(len(self.texts))

def event(mid,chat='oc_a',parent=None,text='工作'):
    return {'event':{'sender':{'sender_type':'user','sender_id':{'open_id':'ou_owner'}},
           'message':{'message_id':mid,'chat_id':chat,'chat_type':'group','message_type':'text',
           'content':json.dumps({'text':'@bot '+text}),'mentions':[{'key':'@bot','id':{'open_id':'ou_bot'}}],
           'parent_id':parent}}}

def action(mid,chat='oc_a',name='approve',form=None,user='ou_owner'):
    return {'event':{'operator':{'open_id':user},'context':{'open_message_id':mid,'open_chat_id':chat},
            'action':{'tag':'button','name':name,'form_value':form or {}}}}

class MultiCardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        for name in ('a','b'):(self.root/name).mkdir()
        (self.root/'projects.json').write_text(json.dumps({'a':str(self.root/'a'),'b':str(self.root/'b')}))
        self.store=Store(self.root/'bridge.db',self.root/'projects.json',self.root/'missing.db')
        self.a=self.store.create_connection('群 A','a');self.b=self.store.create_connection('群 B','b')
        self.settings=self.root/'feishu.json';self.inbox=Inbox(self.root/'inbox.db')
        base={'app_id':'cli_test','app_secret':'secret-for-test','connection_id':self.a['id'],
              'allowed_open_ids':['ou_owner'],'allowed_chat_ids':['oc_a','oc_b'],'bot_open_id':'ou_bot',
              'enabled':True,'cards':default_cards()+[copy.deepcopy(VIDEO)],'default_card_types':['voice_review'],
              'routes':[{'chat_id':'oc_a','name':'群 A','connection_id':self.a['id'],'prompt_template':'A:{{message}}','card_types':['voice_review','video_review'],'enabled':True},
                        {'chat_id':'oc_b','name':'群 B','connection_id':self.b['id'],'prompt_template':'B:{{message}}','card_types':['video_review'],'enabled':True}]}
        save(self.settings,base,self.store)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.store,None,'http://127.0.0.1:0',feishu_settings_path=self.settings,feishu_inbox_path=self.root/'inbox.db'))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.sender=Sender();self.adapter=self.new_adapter()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def new_adapter(self):
        cfg=load(self.settings)
        return FeishuAdapter(cfg,self.inbox,BridgeClient(self.base,cfg['connection_key']),self.sender)
    def req(self,method,path,data=None,origin=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        headers={'Content-Type':'application/json'}
        if origin:headers['Origin']=origin
        c.request(method,path,body=json.dumps(data).encode() if data is not None else None,headers=headers)
        r=c.getresponse();out=(r.status,json.load(r));c.close();return out
    def source(self,mid='om_a',chat='oc_a'):
        self.assertTrue(self.adapter.handle_event(event(mid,chat)));self.adapter.pump_once()
        return self.inbox.cards.source(mid)
    def send(self,source='om_a',ctype='video_review',values=None,key='request_001'):
        return self.req('POST','/api/feishu/cards/send',{'source_message_id':source,'card_type':ctype,'variables':values or {'video_url':'https://example.com/video.mp4'},'request_id':key})
    def test_two_groups_and_route_change_preserve_task(self):
        a=self.source();b=self.source('om_b','oc_b')
        self.assertNotEqual(a['task_id'],b['task_id'])
        self.assertEqual(self.store.task(a['task_id'])['connection_id'],self.a['id'])
        self.assertEqual(self.store.task(b['task_id'])['connection_id'],self.b['id'])
        self.assertTrue(a['prompt'].startswith('A:工作'));self.assertTrue(b['prompt'].startswith('B:工作'))
        cfg=public(load(self.settings));cfg['routes'][0]['connection_id']=self.b['id'];cfg['routes'][0]['prompt_template']='NEW:{{message}}'
        save(self.settings,{'routes':cfg['routes']},self.store);self.adapter=self.new_adapter()
        self.assertTrue(self.adapter.handle_event(event('om_follow','oc_a',parent='om_a')))
        self.adapter.pump_once();follow=self.inbox.cards.source('om_follow')
        self.assertEqual(follow['task_id'],a['task_id']);self.assertTrue(follow['prompt'].startswith('A:工作'))
        self.assertFalse(self.adapter.handle_event(event('om_cross','oc_b',parent='om_a')))
    def test_send_is_idempotent_and_callback_snapshot_survives_config_edit(self):
        source=self.source();status,card=self.send();self.assertEqual(status,202)
        self.assertEqual(self.send()[1]['id'],card['id'])
        self.assertEqual(self.send(values={'video_url':'https://different.example/video'})[0],409)
        self.adapter.pump_once();self.assertEqual(len(self.sender.cards),1)
        self.assertEqual(self.sender.cards[0][1]['data']['template_variable']['video_url']['url'],'https://example.com/video.mp4')
        cfg=public(load(self.settings));cfg['cards'][1]['actions'][0]['prompt_template']='CHANGED'
        save(self.settings,{'cards':cfg['cards']},self.store);self.adapter=self.new_adapter()
        self.assertEqual(self.adapter.handle_card_action(action('om_card_1',user='ou_other')),'ignored')
        self.assertEqual(self.adapter.handle_card_action(action('om_card_1',chat='oc_b')),'ignored')
        self.assertEqual(self.adapter.handle_card_action(action('om_card_1',form={'unknown':'bad'})),'invalid_form')
        self.assertEqual(self.adapter.handle_card_action(action('om_card_1',form={'note':'OK'})),'accepted')
        self.assertEqual(self.adapter.handle_card_action(action('om_card_1',name='reject')),'already_submitted')
        self.adapter.pump_once() # original task still waiting; callback must wait
        self.assertIsNone(self.inbox.cards.source('om_card_1')['task_id'])
        self.store.set_task(source['task_id'],'已完成','视频准备好了')
        self.adapter.pump_once();cb=self.inbox.cards.source('om_card_1')
        self.assertEqual(cb['task_id'],source['task_id']);self.assertIn('继续发布',cb['prompt']);self.assertIn('OK',cb['prompt']);self.assertNotIn('CHANGED',cb['prompt'])
        self.adapter=self.new_adapter();self.adapter.pump_once()
        self.assertEqual(len(self.store.task(source['task_id'])['messages']),2)
        self.assertEqual(self.inbox.cards.list()[0]['state'],'submitted')
    def test_example_voice_marker_uses_generic_queue_and_bound_choice(self):
        source=self.source();self.store.set_task(source['task_id'],'已完成','FEISHU_REVIEW_DOC_URL: '+DOC)
        self.adapter.pump_once();self.assertEqual(len(self.sender.cards),1)
        choice=default_cards()[0]['actions'][0]['fields']['voice']['options'][0]['value']
        bad=json.loads(choice);bad['material_url']='https://evil.example'
        self.assertEqual(self.adapter.handle_card_action(action('om_card_1',name='approve_voice_material',form={'voice':json.dumps(bad)})),'invalid_form')
        self.assertEqual(self.adapter.handle_card_action(action('om_card_1',name='approve_voice_material',form={'voice':choice})),'accepted')
        self.adapter.pump_once();self.assertIn('example_voice',self.inbox.cards.source('om_card_1')['prompt'])
    def test_card_and_group_restrictions(self):
        self.source('om_b','oc_b')
        self.assertEqual(self.send('om_b','voice_review',{'review_doc_link':DOC})[0],403)
        self.assertEqual(self.send('om_b',values={'video_url':'javascript:alert(1)'})[0],400)
        self.assertEqual(self.send('om_missing')[0],404)
        status,_=self.req('POST','/api/feishu/cards/send',{},origin='https://evil.example');self.assertEqual(status,403)
        cfg=public(load(self.settings));cfg['routes'][1]['enabled']=False
        self.assertEqual(self.req('POST','/api/feishu/routes',cfg['routes'][1])[0],200)
        self.adapter=self.new_adapter();self.assertFalse(self.adapter.handle_event(event('om_disabled','oc_b')))
    def test_config_crud_and_secret_redaction(self):
        cfg=self.req('GET','/api/feishu')[1]
        self.assertNotIn('connection_key',json.dumps(cfg));self.assertNotIn('secret-for-test',json.dumps(cfg))
        self.assertEqual(self.req('POST','/api/feishu/cards/remove',{'id':'video_review'})[0],400)
        bad=copy.deepcopy(VIDEO);bad['actions'][0]['prompt_template']='{{variables.missing}}'
        self.assertEqual(self.req('POST','/api/feishu/cards',bad)[0],400)
        self.assertEqual(self.req('POST','/api/feishu/routes/remove',{'chat_id':'oc_b'})[0],200)
        self.assertEqual(len(self.req('GET','/api/feishu')[1]['routes']),1)
    def test_delivery_failure_does_not_duplicate_external_send(self):
        self.source();self.send()
        with patch.object(self.sender,'reply_card',side_effect=TimeoutError('timeout')) as send:
            self.adapter.pump_once();self.adapter.pump_once();self.assertEqual(send.call_count,1)
        self.assertEqual(self.inbox.cards.list()[0]['state'],'delivery_uncertain')
    def test_callback_durable_retry_after_bridge_failure(self):
        source=self.source();self.send();self.adapter.pump_once()
        self.store.set_task(source['task_id'],'已完成','ready')
        self.assertEqual(self.adapter.handle_card_action(action('om_card_1')),'accepted')
        with patch.object(self.adapter.bridge,'submit',side_effect=OSError('offline')): self.adapter.pump_once()
        self.assertIsNone(self.inbox.cards.source('om_card_1')['task_id'])
        self.adapter=self.new_adapter();self.adapter.pump_once();self.adapter.pump_once()
        self.assertEqual(len(self.store.task(source['task_id'])['messages']),2)

    def test_conversation_preview_has_no_writes_or_secrets(self):
        original=self.settings.read_bytes()
        status,ctx=self.req('GET','/api/feishu/workflow-context')
        self.assertEqual(status,200)
        for secret in ('connection_key','api_key','secret-for-test','pairing_code'):
            self.assertNotIn(secret,json.dumps(ctx))
        status,result=self.req('POST','/api/feishu/configure-workflow',{
            'revision':ctx['revision'],'group':'群 A','card':'文档审核与音色确认',
            'on_approve':'审核通过后生成英文配音。','dry_run':True})
        self.assertEqual(status,200);self.assertEqual(result['status'],'preview')
        self.assertEqual(self.settings.read_bytes(),original)
        self.assertEqual(self.inbox.cards.list(),[])

    def test_conversation_save_isolates_callback_and_preserves_bound_material(self):
        other_before=copy.deepcopy(load(self.settings)['routes'][1])
        ctx=self.req('GET','/api/feishu/workflow-context')[1]
        args={'revision':ctx['revision'],'group':'群 A','card':'voice_review',
              'on_approve':'审核通过后生成英文配音。','dry_run':False}
        status,result=self.req('POST','/api/feishu/configure-workflow',args)
        self.assertEqual(status,200);self.assertEqual(result['status'],'saved')
        cfg=load(self.settings); original=next(c for c in cfg['cards'] if c['id']=='voice_review')
        self.assertEqual(original,default_cards()[0])
        self.assertEqual(next(r for r in cfg['routes'] if r['chat_id']=='oc_b'),other_before)
        local=next(c for c in result['cards'] if c['id'].startswith('workflow_'))
        self.assertEqual(local['actions'][0]['fields'],original['actions'][0]['fields'])
        from card_config import callback_prompt
        choice=local['actions'][0]['fields']['voice']['options'][0]['value']
        prompt=callback_prompt(local,'approve_voice_material',{'review_doc_link':DOC},{'voice':choice})
        self.assertIn('生成英文配音',prompt);self.assertIn(DOC,prompt);self.assertIn('example_voice',prompt)
        self.assertEqual(self.req('POST','/api/feishu/configure-workflow',args)[0],409)
        args.update(revision=result['revision'],card=local['id'],on_approve='生成配音并返回链接。')
        status,again=self.req('POST','/api/feishu/configure-workflow',args)
        self.assertEqual(status,200)
        self.assertEqual(len([c for c in load(self.settings)['cards'] if c['id'].startswith('workflow_')]),1)

    def test_conversation_resolves_names_without_rerouting_other_groups(self):
        before=load(self.settings);ctx=self.req('GET','/api/feishu/workflow-context')[1]
        status,result=self.req('POST','/api/feishu/configure-workflow',{
            'revision':ctx['revision'],'group':'群 A','project':'b',
            'instructions':'翻译字幕并提交人工审核。','dry_run':False})
        self.assertEqual(status,200)
        self.assertEqual(result['workflow']['connection_id'],self.b['id'])
        self.assertIn('{{message}}',result['workflow']['prompt_template'])
        after=load(self.settings)
        self.assertEqual(before['routes'][1],next(r for r in after['routes'] if r['chat_id']=='oc_b'))
        self.assertEqual(before['cards'],after['cards'])

    def test_conversation_refuses_ambiguous_names_and_shared_overwrite(self):
        self.store.create_connection('另一个群','a')
        ctx=self.req('GET','/api/feishu/workflow-context')[1]
        original=self.settings.read_bytes()
        base={'revision':ctx['revision'],'group':'群 A','dry_run':False}
        self.assertEqual(self.req('POST','/api/feishu/configure-workflow',{**base,'project':'a'})[0],409)
        self.assertEqual(self.req('POST','/api/feishu/configure-workflow',{**base,'group':'不存在'})[0],409)
        self.assertEqual(self.req('POST','/api/feishu/configure-workflow',{**base,'card':'video_review','on_approve':'继续'})[0],409)
        bad=copy.deepcopy(VIDEO);bad['name']='覆盖共享卡'
        self.assertEqual(self.req('POST','/api/feishu/configure-workflow',{**base,'new_card':bad})[0],409)
        self.assertEqual(self.settings.read_bytes(),original)

    def test_two_phase_callback_keeps_original_task_and_source(self):
        cfg=load(self.settings);cfg['routes'][0]['review_prompt_template']='第二阶段：读文档评论并修订、对齐、配音。\n{{message}}'
        save(self.settings,{'routes':cfg['routes']},self.store);self.adapter=self.new_adapter()
        self.assertTrue(self.adapter.handle_event(event('om_original',text='https://example.com/original.fcpxml')))
        self.adapter.pump_once();source=self.inbox.cards.source('om_original')
        self.store.set_task(source['task_id'],'已完成','ready')
        self.assertEqual(self.send('om_original','voice_review',{'review_doc_link':DOC})[0],202)
        self.adapter.pump_once()
        # A later global edit cannot replace a sent card's phase-two instructions.
        cfg=load(self.settings);cfg['routes'][0]['review_prompt_template']='CHANGED {{message}}'
        save(self.settings,{'routes':cfg['routes']},self.store);self.adapter=self.new_adapter()
        choice=default_cards()[0]['actions'][0]['fields']['voice']['options'][0]['value']
        self.assertEqual(self.adapter.handle_card_action(action('om_card_1',name='approve_voice_material',form={'voice':choice})),'accepted')
        self.adapter.pump_once();cb=self.inbox.cards.source('om_card_1')
        self.assertEqual(cb['task_id'],source['task_id'])
        self.assertTrue(cb['prompt'].startswith('第二阶段：'))
        for token in ('original.fcpxml','om_original','card_approved',DOC,'example_voice','"document_comments_included": false'):
            self.assertIn(token,cb['prompt'])
        self.assertNotIn('CHANGED',cb['prompt']);self.assertNotIn('A:',cb['prompt'])

    def test_two_phase_feedback_is_serial_and_not_approval(self):
        cfg=load(self.settings);cfg['routes'][0]['review_prompt_template']='第二阶段 {{message}}'
        save(self.settings,{'routes':cfg['routes']},self.store);self.adapter=self.new_adapter()
        source=self.source();self.send();self.adapter.pump_once()
        self.assertTrue(self.adapter.handle_event(event('om_feedback',parent='om_card_1',text='我补充了文档评论')))
        self.adapter.pump_once();self.assertIsNone(self.inbox.cards.source('om_feedback')['task_id'])
        self.store.set_task(source['task_id'],'已完成','ready');self.adapter.pump_once()
        follow=self.inbox.cards.source('om_feedback')
        self.assertEqual(follow['task_id'],source['task_id']);self.assertIn('review_feedback',follow['prompt'])
        self.assertNotIn('card_approved',follow['prompt'])

if __name__=='__main__':unittest.main()
