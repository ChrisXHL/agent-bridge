"""Isolated multi-application/group routing and preservation checks."""
import json
import time
from unittest.mock import patch
import unittest
import test_feishu_scenarios as scenarios
from test_feishu import FakeBridge, FakeSender, event
from feishu_groups import AccountIdentity, GroupSettings, atomic_write, revision
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

    def test_passwordless_migration_imports_routes_disabled_and_preserves_source(self):
        from feishu_migration import export_bundle, import_bundle
        original = load(self.settings_path)
        original['app_id'] = 'cli_aaaaaaaaaaaaaaaa'
        original['self_update_rule'] = '每次说明已核实的来源。'
        original['auto_group_owner_open_id'] = 'ou_owner'
        original['group_configs'] = {'oc_old': {'chat_id': 'oc_old', 'name': '旧群',
            'connection_id': original['connection_id'], 'prompt_template': 'OLD {{message}}',
            'enabled': True, 'topic_collaboration': False,
            'self_update_rule': '在本群优先给简短结论。'}}
        atomic_write(self.settings_path, original)
        package = export_bundle(self.manager, {'selected': [{'ref': 'default', 'revision': revision(original)}]})['bundle']
        self.assertEqual(package['version'], 2)
        self.assertEqual(package['apps'][0]['app_secret'], original['app_secret'])
        target = self.root/'target'; target.mkdir()
        other = GroupSettings(target/'feishu.json', target/'inbox.sqlite3', self.store)
        with patch('feishu_groups.probe', return_value={'bot_ready': True, 'bot_open_id': 'ou_bot', 'bot_name': 'Bot'}):
            result = import_bundle(other, {'bundle': package, 'project_id': 'project-2'})
        imported = load(other.resolve(result['created'][0]['ref'])[1])
        self.assertFalse(imported['enabled'])
        self.assertEqual(imported['app_secret'], original['app_secret'])
        self.assertEqual(imported['prompt_template'], original['prompt_template'])
        self.assertEqual(imported['self_update_rule'], original['self_update_rule'])
        self.assertEqual(imported['auto_group_owner_open_id'], 'ou_owner')
        self.assertEqual(imported['allowed_chat_ids'], ['oc_old'])
        self.assertEqual(imported['group_configs']['oc_old']['prompt_template'], 'OLD {{message}}')
        self.assertEqual(imported['group_configs']['oc_old']['self_update_rule'], '在本群优先给简短结论。')
        self.assertEqual(self.store.connection(imported['connection_id'])['project_id'], 'project-2')
        self.assertEqual(load(self.settings_path), original)
        with self.assertRaisesRegex(Exception, '已经接入'):
            import_bundle(other, {'bundle': package, 'project_id': 'project-2'})

    def test_copy_assistant_config_without_feishu_identity(self):
        from assistant_template import apply_template, export_template
        source = load(self.settings_path)
        source['prompt_template'] = '只处理已核实的信息：{{message}}'
        source['self_update_rule'] = '先给结论，再给证据。'
        atomic_write(self.settings_path, source)
        exported = export_template(self.manager, {'ref': 'default', 'revision': revision(source)})['bundle']
        self.assertEqual(exported['format'], 'codex-bridge-assistant-template')
        serialized = json.dumps(exported, ensure_ascii=False)
        for private in ('app_id', 'app_secret', 'allowed_open_ids', 'allowed_chat_ids',
                        'connection_key', 'chat_id', source['app_id'], source['app_secret'], 'oc_old', 'ou_owner'):
            self.assertNotIn(private, serialized)
        target_ref = self.new_app()
        _, target_path, _ = self.manager.resolve(target_ref)
        target = load(target_path)
        target.update(allowed_open_ids=['ou_friend'], allowed_chat_ids=['oc_target'],
                      group_configs={'oc_target': {'chat_id': 'oc_target', 'name': '目标群',
                          'connection_id': target['connection_id'], 'prompt_template': '目标群 {{message}}',
                          'enabled': True, 'topic_collaboration': False}})
        atomic_write(target_path, target)
        result = apply_template(self.manager, {'bundle': exported, 'target_ref': target_ref,
                                               'revision': revision(target), 'project_id': 'project-1'})
        copied = load(target_path)
        self.assertTrue(result['applied'])
        self.assertEqual(copied['prompt_template'], source['prompt_template'])
        self.assertEqual(copied['self_update_rule'], source['self_update_rule'])
        for key in ('app_id', 'app_secret', 'allowed_open_ids', 'allowed_chat_ids', 'enabled'):
            self.assertEqual(copied[key], target[key])
        self.assertEqual(copied['group_configs']['oc_target']['prompt_template'], '目标群 {{message}}')
        self.assertEqual(self.store.connection(copied['connection_id'])['project_id'], 'project-1')
        self.assertEqual(load(self.settings_path), source)

    def test_copy_assistant_rejects_tampered_bundle_and_stale_target(self):
        from assistant_template import apply_template, export_template
        source = load(self.settings_path)
        bundle = export_template(self.manager, {'ref': 'default', 'revision': revision(source)})['bundle']
        target_ref = self.new_app()
        _, target_path, _ = self.manager.resolve(target_ref)
        target = load(target_path)
        payload = {'bundle': bundle, 'target_ref': target_ref,
                   'revision': revision(target), 'project_id': 'project-2'}
        with self.assertRaisesRegex(Exception, '助手配置内容无效'):
            apply_template(self.manager, {**payload, 'bundle': {**bundle,
                'assistant': {**bundle['assistant'], 'app_secret': 'must-not-import'}}})
        with self.assertRaisesRegex(Exception, '配置已在其他地方更新'):
            apply_template(self.manager, {**payload, 'revision': 'stale'})
        self.assertEqual(load(target_path), target)

    def test_assistant_template_http_export_is_separate_from_robot_migration(self):
        source = load(self.settings_path)
        status, result = self.request('POST', '/api/feishu/assistant-template/export',
                                      {'ref': 'default', 'revision': revision(source)})
        self.assertEqual(status, 200, result)
        self.assertEqual(result['bundle']['format'], 'codex-bridge-assistant-template')
        self.assertNotIn('apps', result['bundle'])
        self.assertEqual(load(self.settings_path), source)

    def test_old_encrypted_bundle_can_still_be_imported(self):
        import os
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from feishu_migration import export_bundle, import_bundle, _key, _field
        original = load(self.settings_path)
        atomic_write(self.settings_path, {**original, 'app_id': 'cli_aaaaaaaaaaaaaaaa'})
        current = load(self.settings_path)
        plain = export_bundle(self.manager, {'selected': [{'ref': 'default', 'revision': revision(current)}]})['bundle']
        salt, nonce = os.urandom(16), os.urandom(12)
        password = 'old-private-migration-passphrase'
        encrypted = AESGCM(_key(password, salt)).encrypt(nonce,
            json.dumps({'version': 1, 'apps': plain['apps']}).encode(), b'codex-bridge-feishu-migration-v1')
        legacy = {'format': plain['format'], 'version': 1, 'salt': _field(salt),
                  'nonce': _field(nonce), 'data': _field(encrypted)}
        target = self.root/'old-target'; target.mkdir()
        other = GroupSettings(target/'feishu.json', target/'inbox.sqlite3', self.store)
        with self.assertRaisesRegex(Exception, '旧加密包需要'):
            import_bundle(other, {'bundle': legacy, 'project_id': 'project-2'})
        with patch('feishu_groups.probe', return_value={'bot_ready': True, 'bot_open_id': 'ou_bot', 'bot_name': 'Bot'}):
            result = import_bundle(other, {'bundle': legacy, 'password': password, 'project_id': 'project-2'})
        self.assertEqual(load(other.resolve(result['created'][0]['ref'])[1])['app_secret'], current['app_secret'])

    def test_migration_batch_stop_needs_prepared_target_and_keeps_other_robot(self):
        from feishu_migration import stop_selected
        other_ref = self.new_app()
        other_before = load(self.manager.resolve(other_ref)[1])
        first = load(self.settings_path)
        with self.assertRaisesRegex(Exception, '新电脑导入'):
            stop_selected(self.manager, {'selected': [{'ref': 'default', 'revision': revision(first)}],
                                         'target_prepared': False})
        self.assertEqual(load(self.settings_path), first)
        result = stop_selected(self.manager, {'selected': [{'ref': 'default', 'revision': revision(first)}],
                                               'target_prepared': True})
        self.assertEqual(result['stopped'], 1)
        self.assertFalse(load(self.settings_path)['enabled'])
        self.assertEqual(load(self.manager.resolve(other_ref)[1]), other_before)

    def test_migration_refuses_special_bots_that_need_local_management_records(self):
        from feishu_migration import export_bundle
        before = load(self.settings_path)
        atomic_write(self.settings_path, {**before, 'assistant_manager': {'admin_open_id': 'ou_owner'}})
        current = load(self.settings_path)
        self.assertFalse(self.manager.catalog()['apps'][0]['migration_supported'])
        with self.assertRaisesRegex(Exception, '暂不能用配置包迁移'):
            export_bundle(self.manager, {'selected': [{'ref': 'default', 'revision': revision(current)}]})
        self.assertEqual(load(self.settings_path), current)

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
        self.assertIn(load(self.settings_path)['connection_id'],body['managed_connection_ids'])
        self.assertEqual(self.before,self.settings_path.read_bytes())
        serialized=json.dumps(body)
        for value in ('secret-for-test',load(self.settings_path)['connection_key'],'app_secret'):
            self.assertNotIn(value,serialized)

    def test_new_group_uses_fetched_feishu_name_when_left_blank(self):
        value=self.pair('default','oc_named_group')
        payload=self.group('oc_named_group');payload['name']='';payload['pairing_code']=value['pairing_code']
        with patch('feishu_groups.chat_name',return_value='真实飞书群名') as lookup:
            status,catalog=self.request('POST','/api/feishu/groups',payload)
        self.assertEqual(status,200)
        lookup.assert_called_once_with(value['app_id'],value['app_secret'],'oc_named_group')
        self.assertEqual(next(g['name'] for g in catalog['groups'] if g['chat_id']=='oc_named_group'),'真实飞书群名')
        self.assertEqual(load(self.settings_path)['group_configs']['oc_named_group']['name'],'真实飞书群名')

    def test_new_group_without_readable_name_asks_for_manual_name(self):
        value=self.pair('default','oc_unreadable_group')
        before=self.settings_path.read_bytes()
        payload=self.group('oc_unreadable_group');payload['name']='';payload['pairing_code']=value['pairing_code']
        with patch('feishu_groups.chat_name',return_value=None):
            status,error=self.request('POST','/api/feishu/groups',payload)
        self.assertEqual(status,409)
        self.assertEqual(error['error']['code'],'group_name_unavailable')
        self.assertEqual(self.settings_path.read_bytes(),before)

    def test_switch_one_robot_backend_keeps_existing_task_and_other_robot(self):
        other_ref=self.new_app()
        first=self.manager.catalog()['apps'][0]
        connection=self.store.connection(first['connection_id'],reveal=True)
        _,created=self.store.submit(connection,{'message':'之前的任务'},'backend-history-001')
        with patch('claude_runtime.run_structured',return_value={'ready':True}):
            status,catalog=self.request('POST','/api/feishu/apps/backend',
                                        {'app_ref':'default','revision':first['revision'],
                                         'execution_backend':'claude'})
        self.assertEqual(status,200)
        self.assertEqual(next(a for a in catalog['apps'] if a['ref']=='default')['execution_backend'],'claude')
        self.assertEqual(next(a for a in catalog['apps'] if a['ref']==other_ref)['execution_backend'],'codex')
        self.assertEqual(self.store.task(created['task_id'])['execution_backend'],'codex')
        self.assertEqual(self.store.connection(first['connection_id'])['execution_backend'],'claude')
        self.assertEqual(self.settings_path.read_bytes(),self.before)

    def test_disable_one_robot_preserves_other_robot_routes_and_history(self):
        other_ref=self.new_app()
        first=next(a for a in self.manager.catalog()['apps'] if a['ref']=='default')
        other_path=self.manager.resolve(other_ref)[1]
        other_before=other_path.read_bytes()
        original=load(self.settings_path)
        connection=self.store.connection(first['connection_id'],reveal=True)
        _,created=self.store.submit(connection,{'message':'已经开始的任务'},'disable-history-001')
        status,catalog=self.request('POST','/api/feishu/apps/enabled',
                                    {'app_ref':'default','revision':first['revision'],'enabled':False})
        self.assertEqual(status,200)
        disabled=next(a for a in catalog['apps'] if a['ref']=='default')
        self.assertFalse(disabled['enabled'])
        self.assertEqual(next(a for a in catalog['apps'] if a['ref']==other_ref)['enabled'],
                         load(other_path)['enabled'])
        self.assertEqual(other_path.read_bytes(),other_before)
        self.assertEqual(load(self.settings_path),{**original,'enabled':False})
        self.assertEqual(self.store.task(created['task_id'])['id'],created['task_id'])
        stale_status,stale=self.request('POST','/api/feishu/apps/enabled',
                                        {'app_ref':'default','revision':first['revision'],'enabled':True})
        self.assertEqual(stale_status,409)
        self.assertEqual(stale['error']['code'],'settings_changed')
        status,restored=self.request('POST','/api/feishu/apps/enabled',
                                     {'app_ref':'default','revision':disabled['revision'],'enabled':True})
        self.assertEqual(status,200)
        self.assertTrue(next(a for a in restored['apps'] if a['ref']=='default')['enabled'])
        self.assertEqual(self.settings_path.read_bytes(),self.before)

    def test_cannot_enable_unpublished_bot(self):
        value=load(self.settings_path)
        atomic_write(self.settings_path,{**value,'enabled':False,'bot_ready':False})
        target=self.manager.catalog()['apps'][0]
        status,error=self.request('POST','/api/feishu/apps/enabled',
                                  {'app_ref':'default','revision':target['revision'],'enabled':True})
        self.assertEqual(status,409)
        self.assertEqual(error['error']['code'],'bot_not_ready')
        self.assertFalse(load(self.settings_path)['enabled'])

    def test_account_name_changes_only_local_label(self):
        status, body = self.request('POST', '/api/feishu/accounts',
                                    {'account_id': 'unverified:default', 'name': '工作飞书'})
        self.assertEqual(status, 200)
        self.assertEqual(body['apps'][0]['account_name'], '工作飞书')
        self.assertEqual(self.settings_path.read_bytes(), self.before)
        self.assertEqual(load(self.manager.account_labels_path), {'unverified:default': '工作飞书'})
        status, _ = self.request('POST', '/api/feishu/accounts',
                                 {'account_id': 'unverified:other', 'name': '不应保存'})
        self.assertEqual(status, 404)

    def test_account_remove_and_restore_http_routes(self):
        status, catalog=self.request('GET','/api/feishu/catalog')
        self.assertEqual(status,200)
        app=catalog['apps'][0]
        status, removed=self.request('POST','/api/feishu/accounts/remove',
                                    {'account_id':app['account_id'],
                                     'expected':[{'ref':app['ref'],'revision':app['revision']}]})
        self.assertEqual(status,200)
        self.assertEqual(removed['apps'],[])
        self.assertEqual(removed['archived_accounts'][0]['name'],app['name']+' 所在账号')
        status, restored=self.request('POST','/api/feishu/accounts/restore',
                                     {'account_id':app['account_id']})
        self.assertEqual(status,200)
        self.assertEqual(len(restored['apps']),1)
        self.assertEqual(self.settings_path.read_bytes(),self.before)

    def test_account_grouping_keeps_different_tenants_separate(self):
        identity = AccountIdentity()
        entries = [('a', 'cli_1111111111111111', 'secret-a'),
                   ('b', 'cli_2222222222222222', 'secret-b'),
                   ('c', 'cli_3333333333333333', 'secret-c')]
        with patch.object(AccountIdentity, 'fetch', side_effect=lambda app_id, _: 'tenant-b' if '3333' in app_id else 'tenant-a') as fetch:
            self.assertEqual(identity.resolve(entries), {'a': 'tenant-a', 'b': 'tenant-a', 'c': 'tenant-b'})
            self.assertEqual(identity.resolve(entries), {'a': 'tenant-a', 'b': 'tenant-a', 'c': 'tenant-b'})
            self.assertEqual(fetch.call_count, 3)

    def test_remove_and_restore_account_keeps_other_account_and_history(self):
        other_ref=self.new_app()
        with patch.object(self.manager.account_identity, 'resolve', return_value={'default':'tenant-a',other_ref:'tenant-b'}):
            original=load(self.settings_path)
            original_connection=self.store.connection(original['connection_id'])
            completed=self.store.submit(original_connection,{'message':'历史任务'},'account-archive-test')[1]
            self.store.set_task(completed['task_id'],'已完成','历史结果')
            historical=self.store.task(completed['task_id'])
            catalog=self.manager.catalog()
            target=next(a for a in catalog['apps'] if a['ref']=='default')
            expected=[{'ref':target['ref'],'revision':target['revision']}]
            other_path=self.manager.resolve(other_ref)[1]
            other_before=other_path.read_bytes()
            removed=self.manager.dispatch('/api/feishu/accounts/remove','POST',
                                          {'account_id':'tenant-a','expected':expected})
            self.assertEqual([a['ref'] for a in removed['apps']],[other_ref])
            self.assertIn(original['connection_id'],removed['managed_connection_ids'])
            self.assertEqual(removed['archived_accounts'][0]['app_count'],1)
            self.assertFalse(load(self.settings_path)['enabled'])
            self.assertEqual(load(self.settings_path)['account_archived'],'tenant-a')
            self.assertEqual(self.store.connection(original['connection_id'])['status'],'已停用')
            self.assertEqual(other_path.read_bytes(),other_before)
            self.assertEqual(self.store.task(completed['task_id']),historical)
            restored=self.manager.dispatch('/api/feishu/accounts/restore','POST',{'account_id':'tenant-a'})
            self.assertEqual(len(restored['apps']),2)
            self.assertEqual(load(self.settings_path),original)
            self.assertEqual(self.store.connection(original['connection_id'])['status'],original_connection['status'])
            self.assertEqual(other_path.read_bytes(),other_before)
            self.assertEqual(self.store.task(completed['task_id']),historical)

    def test_remove_account_rejects_busy_task_and_stale_list(self):
        with patch.object(self.manager.account_identity, 'resolve', return_value={'default':'tenant-a'}):
            target=self.manager.catalog()['apps'][0]
            data={'account_id':'tenant-a','expected':[{'ref':'default','revision':target['revision']}]}
            with self.assertRaisesRegex(Exception,'已变化'):
                self.manager.remove_account({'account_id':'tenant-a','expected':[{'ref':'default','revision':'stale'}]})
            connection=self.store.connection(load(self.settings_path)['connection_id'])
            self.store.submit(connection,{'message':'进行中任务'},'account-busy-test')
            with self.assertRaisesRegex(Exception,'未结束的任务'):self.manager.remove_account(data)
            self.assertEqual(self.settings_path.read_bytes(),self.before)
            self.assertFalse(self.manager.account_archives_path.exists())

    def test_remove_account_groups_multiple_bots_and_recovers_interrupted_removal(self):
        other_ref=self.new_app()
        with patch.object(self.manager.account_identity, 'resolve', return_value={'default':'tenant-a',other_ref:'tenant-a'}):
            catalog=self.manager.catalog()
            expected=[{'ref':a['ref'],'revision':a['revision']} for a in catalog['apps']]
            removed=self.manager.remove_account({'account_id':'tenant-a','expected':expected})
            self.assertEqual(removed['apps'],[])
            self.assertEqual(removed['archived_accounts'][0]['app_count'],2)
            self.assertTrue(all(load(self.manager.resolve(ref)[1])['account_archived']=='tenant-a'
                                for ref in ('default',other_ref)))
            archives=self.manager.account_archives()
            archives['tenant-a']['state']='removing'
            atomic_write(self.manager.account_archives_path,archives)
            fresh=GroupSettings(self.settings_path,self.inbox_path,self.store)
            self.assertEqual(fresh.account_archives()['tenant-a']['state'],'removed')
            fresh.restore_account({'account_id':'tenant-a'})
            self.assertEqual({a['ref'] for a in fresh.catalog()['apps']},{'default',other_ref})

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

    def test_robot_and_group_can_use_different_existing_folders(self):
        other_ref=self.new_app()
        other_path=self.manager.resolve(other_ref)[1]
        other_before=other_path.read_bytes()
        private=self.root/'private-work';private.mkdir()
        group=self.root/'group-work';group.mkdir()
        private_project=self.store.register_project_path(str(private))
        group_project=self.store.register_project_path(str(group))
        old=load(self.settings_path)
        first=next(a for a in self.manager.catalog()['apps'] if a['ref']=='default')
        connection=self.store.connection(first['connection_id'],reveal=True)
        _,task=self.store.submit(connection,{'message':'历史任务'},'folder-history-001')
        status,catalog=self.request('POST','/api/feishu/apps',
                                    {'ref':'default','revision':first['revision'],'name':first['name'],
                                     'project_id':private_project['id']})
        self.assertEqual(status,200)
        changed=next(a for a in catalog['apps'] if a['ref']=='default')
        self.assertEqual(changed['project_path'],str(private.resolve()))
        self.assertEqual(next(g for g in catalog['groups'] if g['chat_id']=='oc_old')['project_id'],'project-1')
        payload=self.group();payload['revision']=changed['revision'];payload['project_id']=group_project['id']
        status,catalog=self.request('POST','/api/feishu/groups',payload)
        self.assertEqual(status,200)
        self.assertEqual(next(g for g in catalog['groups'] if g['chat_id']=='oc_old')['project_path'],str(group.resolve()))
        self.assertEqual(next(a for a in catalog['apps'] if a['ref']=='default')['project_id'],private_project['id'])
        self.assertEqual(other_path.read_bytes(),other_before)
        self.assertEqual(self.store.task(task['task_id'])['connection_id'],old['connection_id'])

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
