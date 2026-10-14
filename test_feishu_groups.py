"""Isolated multi-application/group routing and preservation checks."""
import json
import time
from unittest.mock import patch
import unittest
import test_feishu_scenarios as scenarios
from test_feishu import FakeBridge, FakeSender, event
from feishu_groups import GroupSettings, atomic_write, revision
from feishu_settings import load
from feishu_worker import FeishuAdapter, Inbox
from feishu_supervisor import MultiFeishuSupervisor

class GroupTests(scenarios.FeishuScenarioTests):
    # Reuse HTTP fixture, not the inherited scenario cases.
    def setUp(self):
        super().setUp()
        status, result = self.setup_app()
        self.assertEqual(status, 200)
        self.manager=GroupSettings(self.settings_path,self.inbox_path,self.store)
        value=load(self.settings_path)
        value.update(allowed_chat_ids=['oc_old'],allowed_open_ids=['ou_owner'],prompt_template='OLD {{message}}',enabled=True)
        atomic_write(self.settings_path,value)
        self.before=self.settings_path.read_bytes()
        self.other=self.root/'other';self.other.mkdir()
        projects=self.root/'projects.json'
        projects.write_text(json.dumps({'project-1':str(self.root/'project'),'project-2':str(self.other)}))

    def group(self, chat='oc_old', **changes):
        return dict(app_ref='default', revision=revision(load(self.settings_path)),chat_id=chat,
                    name='Group',project_id='project-1',prompt_template='GROUP {{message}}',enabled=True,**changes)

    def pair(self,ref,chat):
        self.manager.pairing({'app_ref':ref,'action':'start'})
        _,path,inbox=self.manager.resolve(ref); v=load(path)
        Inbox(inbox).record_pairing(dict(message_id='om_'+chat,code=v['pairing_code'],sender_id='ou_member',chat_id=chat,chat_type='group'))
        return v

    def new_app(self):
        with patch('feishu_groups.probe',return_value={'bot_ready':True,'bot_open_id':'ou_bot2','bot_name':'Bot 2'}):
            return self.manager.save_app(dict(name='Company B',app_id='cli_b',app_secret='secret-app-b',project_id='project-2'))

    def test_catalog_is_read_only_and_no_credentials(self):
        status,body=self.request('GET','/api/feishu/catalog')
        self.assertEqual(status,200);self.assertEqual(body['groups'][0]['prompt_template'],'OLD {{message}}')
        self.assertEqual(self.before,self.settings_path.read_bytes())
        serialized=json.dumps(body)
        for value in ('secret-for-test',load(self.settings_path)['connection_key'],'app_secret'):
            self.assertNotIn(value,serialized)

    def test_update_group_and_stale_revision(self):
        p=self.group(); status,_=self.request('POST','/api/feishu/groups',p);self.assertEqual(status,200)
        p['prompt_template']='WRONG {{message}}'
        status,body=self.request('POST','/api/feishu/groups',p)
        self.assertEqual(status,409);self.assertEqual(body['error']['code'],'settings_changed')
        v=load(self.settings_path);self.assertEqual(v['prompt_template'],'OLD {{message}}')
        self.assertEqual(v['group_configs']['oc_old']['prompt_template'],'GROUP {{message}}')

    def test_new_app_isolated_and_duplicate_rejected(self):
        ref=self.new_app();self.assertEqual(self.settings_path.read_bytes(),self.before)
        self.assertEqual(len(self.manager.catalog()['apps']),2)
        with self.assertRaisesRegex(Exception,'已添加'):self.new_app()
        _,p,inbox=self.manager.resolve(ref)
        self.assertEqual(load(p)['allowed_open_ids'],[]);self.assertNotEqual(inbox,self.inbox_path)
        self.assertEqual(p.stat().st_mode & 0o777,0o600)

    def test_add_group_requires_correct_app_pairing(self):
        ref=self.new_app();v=self.pair(ref,'oc_second')
        p=self.group('oc_second',pairing_code=v['pairing_code'])
        status,_=self.request('POST','/api/feishu/groups',p);self.assertEqual(status,409)
        p.update(app_ref=ref,revision=revision(v),project_id='project-2')
        status,body=self.request('POST','/api/feishu/groups',p);self.assertEqual(status,200)
        self.assertEqual(len(body['groups']),2)
        self.assertEqual(self.settings_path.read_bytes(),self.before)
        self.assertEqual(load(self.manager.resolve(ref)[1])['allowed_open_ids'],[])

    def test_pairing_does_not_overwrite_existing_group(self):
        v=self.pair('default','oc_old')
        status,body=self.request('POST','/api/feishu/groups',self.group(pairing_code=v['pairing_code']))
        self.assertEqual(status,409);self.assertEqual(body['error']['code'],'group_exists')
        self.assertNotIn('group_configs',load(self.settings_path))

    def test_expired_pairing_cannot_add_group(self):
        v=self.pair('default','oc_new');v['pairing_expires_at']=time.time()-1;atomic_write(self.settings_path,v)
        status,_=self.request('POST','/api/feishu/groups',self.group('oc_new',pairing_code=v['pairing_code']))
        self.assertEqual(status,409)

    def test_private_default_edit_freezes_existing_group(self):
        v=load(self.settings_path)
        with patch('feishu_groups.probe') as probe:
            self.manager.save_app(dict(ref='default',revision=revision(v),name='Rename',project_id='project-2',prompt_template='PRIVATE {{message}}'))
            probe.assert_not_called()
        new=load(self.settings_path)
        self.assertEqual(new['group_configs']['oc_old']['prompt_template'],'OLD {{message}}')
        self.assertEqual(new['group_configs']['oc_old']['connection_id'],v['connection_id'])
        self.assertNotEqual(new['connection_id'],v['connection_id'])

    def test_routing_per_group_and_followup_keeps_frozen_route(self):
        v=load(self.settings_path);v['allowed_chat_ids']+=['oc_second']
        v['group_configs']={'oc_old':{'connection_id':'a','prompt_template':'A {{message}}'},'oc_second':{'connection_id':'b','prompt_template':'B {{message}}'}}
        inbox=Inbox(self.inbox_path); bridges={'a':FakeBridge(),'b':FakeBridge(),'c':FakeBridge()}
        adapter=FeishuAdapter(v,inbox,FakeBridge(),FakeSender(),client_factory=bridges.get)
        def msg(mid,chat,parent=None):return event(mid,chat=chat,chat_type='group',text='@b hello',parent=parent,mentions=[{'id':{'open_id':'ou_bot'},'key':'@b'}])
        self.assertTrue(adapter.handle_event(msg('om_a','oc_old')))
        self.assertTrue(adapter.handle_event(msg('om_b','oc_second')));adapter.pump_once()
        self.assertTrue(bridges['a'].submissions[0][0].startswith('A hello'))
        self.assertTrue(bridges['b'].submissions[0][0].startswith('B hello'))
        tid=inbox.linked_source('om_a')['task_id'];bridges['a'].tasks[tid]={'status':'已完成','result':'done'}
        v['group_configs']['oc_old']={'connection_id':'c','prompt_template':'C {{message}}'}
        self.assertTrue(adapter.handle_event(msg('om_follow','oc_old','om_a')));adapter.pump_once()
        self.assertEqual(bridges['c'].submissions,[])
        self.assertTrue(bridges['a'].submissions[-1][0].startswith('A hello'))
        self.assertEqual(bridges['a'].submissions[-1][1],tid)
        v['group_configs']['oc_second']['enabled']=False
        self.assertFalse(adapter.handle_event(msg('om_disabled','oc_second')))
        wrong=msg('om_wrong','oc_old');wrong['header']={'app_id':'cli_wrong'}
        self.assertFalse(adapter.handle_event(wrong))

    def test_inboxes_and_delivery_isolated_across_apps(self):
        ref=self.new_app();_,path,inbox_path=self.manager.resolve(ref)
        a=load(self.settings_path); b=load(path); b.update(allowed_chat_ids=['oc_old'],bot_open_id='ou_bot',prompt_template='B {{message}}')
        clients=[FakeBridge(),FakeBridge()]
        for config,ip,client in ((a,self.inbox_path,clients[0]),(b,inbox_path,clients[1])):
            adapter=FeishuAdapter(config,Inbox(ip),client,FakeSender())
            self.assertTrue(adapter.handle_event(event('om_shared',chat='oc_old',chat_type='group',mentions=[{'id':{'open_id':'ou_bot'},'key':'@b'}])))
            adapter.pump_once()
        self.assertEqual(len(clients[0].tasks),1);self.assertEqual(len(clients[1].tasks),1)
        self.assertNotEqual(list(clients[0].requests),list(clients[1].requests))
        self.assertTrue(clients[0].submissions[0][0].startswith('OLD'))
        self.assertTrue(clients[1].submissions[0][0].startswith('B'))

    def test_one_receiver_per_app_and_removal_of_duplicates(self):
        ref=self.new_app();_,path,_=self.manager.resolve(ref)
        with patch('feishu_supervisor.FeishuSupervisor') as child:
            s=MultiFeishuSupervisor(self.settings_path,self.inbox_path,'http://localhost')
            s.reconcile();s.reconcile();self.assertEqual(child.call_count,2)
            self.assertEqual(len(s.receivers),2)
            v=load(path);v['app_id']='cli_test';atomic_write(path,v)
            s.reconcile();self.assertEqual(set(s.receivers),{'default'})
            self.assertTrue(child.return_value.close.called)

# Avoid rerunning the fixture's pre-existing scenario tests in this class.
for _name in list(vars(scenarios.FeishuScenarioTests)):
    if _name.startswith('test_'):setattr(GroupTests,_name,None)

if __name__=='__main__':unittest.main()
