import io
import json
import stat
import unittest
from pathlib import Path
from unittest.mock import Mock
import test_feishu as fixtures
from feishu_worker import Inbox,FeishuAdapter,FeishuSender
from feishu_media import MediaError,parse,MAX_RESOURCE_BYTES

class MediaTests(unittest.TestCase):
 def setUp(self):
  self.f=fixtures.FeishuBridgeTests();self.f.setUp()
  self.adapter=self.f.adapter;self.box=self.f.inbox;self.bridge=self.f.bridge;self.sender=self.f.sender;self.downloads=[]
  def download(mid,key,kind,output,limit):self.downloads.append((mid,key,kind));output.write(('actual-bytes-'+key).encode())
  self.sender.download_resource=download
 def tearDown(self):self.f.tearDown()
 def event(self,kind='image',content=None,mid='om_media',**kw):
  e=fixtures.event(mid,message_type=kind,**kw);e['event']['message']['content']=json.dumps(content if content is not None else {'image_key':'img_fixture'})
  return e
 def media(self,mid='om_media'):return self.adapter.media.record(mid)
 def manifest(self,mid='om_media'):return json.loads(self.media(mid)['manifest'])
 def test_image_downloaded_only_after_authorization_and_then_submitted(self):
  self.assertTrue(self.adapter.handle_event(self.event()));self.assertEqual(self.downloads,[]);self.assertEqual(self.bridge.submissions,[])
  self.adapter.pump_once();m=self.manifest()[0];p=Path(m['path']);self.assertEqual(p.read_bytes(),b'actual-bytes-img_fixture');self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o600)
  self.assertEqual(m['kind'],'image');self.assertIn(m['path'],self.bridge.submissions[0][0]);self.assertIn('实际打开查看',self.bridge.submissions[0][0]);self.assertTrue(self.box.linked_source('om_media')['task_id'])
 def test_file_audio_and_video_resources_preserve_type_and_name(self):
  for kind,name in [('file','报告.pdf'),('audio','录音.opus'),('media','片段.mp4')]:
   self.assertTrue(self.adapter.handle_event(self.event(kind,{'file_key':'file_'+kind,'file_name':name},mid='om_'+kind)))
  for _ in range(3):
   self.adapter.pump_once()
   for tid in self.bridge.tasks:self.bridge.tasks[tid]={'status':'已完成','result':'已读取'}
  self.assertEqual(len(self.bridge.submissions),3)
  for kind in ('file','audio','media'):
   self.assertEqual(self.manifest('om_'+kind)[0]['kind'],kind);self.assertEqual(self.downloads[('file','audio','media').index(kind)][2],'file')
 def test_rich_post_preserves_caption_links_and_multiple_images(self):
  c={'zh_cn':{'title':'标题','content':[[{'tag':'text','text':'看看这些图 '},{'tag':'a','href':'https://example.com/reference','text':'来源'}],[{'tag':'img','image_key':'img_a'}],[{'tag':'img','image_key':'img_b'}]]}}
  self.assertTrue(self.adapter.handle_event(self.event('post',c)));self.adapter.pump_once();text=self.bridge.submissions[0][0]
  self.assertIn('看看这些图',text);self.assertIn('https://example.com/reference',text);self.assertEqual(len(self.manifest()),2)
 def test_flattened_receive_post_and_duplicate_resource(self):
  c={'title':'分析','content':[[{'tag':'text','text':'内容'},{'tag':'img','image_key':'img_a'},{'tag':'img','image_key':'img_a'}]]}
  text,resources=parse('post',c);self.assertIn('内容',text);self.assertEqual(len(resources),1)
 def test_real_client_post_downloads_word_attachment_alongside_caption(self):
  c={'title':'','content':[[{'tag':'text','text':'根据文档帮我分析','style':[]}]],
     'content_v2':[[{'tag':'text','text':'根据文档帮我分析','style':[]}]],
     'files':[{'file_key':'file_v3_doc_fixture','file_name':'数据情况.docx','is_folder':False}]}
  self.assertTrue(self.adapter.handle_event(self.event('post',c)))
  self.adapter.pump_once()
  self.assertEqual(self.downloads,[('om_media','file_v3_doc_fixture','file')])
  m=self.manifest()[0]
  self.assertEqual(m['name'],'数据情况.docx');self.assertTrue(Path(m['path']).is_file())
  self.assertIn('根据文档帮我分析',self.bridge.submissions[0][0]);self.assertIn(m['path'],self.bridge.submissions[0][0])
 def test_post_file_variants_deduplicate_and_keep_images(self):
  f={'file_key':'file_pdf','file_name':'材料.pdf'}
  for c in ({'files':[f]}, {'content_v2':[[{'tag':'text','text':'阅读附件'}]],'files':[f]},
            {'files':[f],'zh_cn':{'content':[[{'tag':'img','image_key':'img_ref'}]],'files':[f]}}):
   with self.subTest(c=c):
    text,resources=parse('post',c)
    self.assertEqual(sum(r['key']=='file_pdf' for r in resources),1)
    self.assertTrue(text)
 def test_post_files_do_not_bypass_limits_or_silently_drop_folders(self):
  for files,code in (([{'file_key':'file_'+str(i)} for i in range(11)],'too_many_media'),
                     ([{'file_key':'file_folder','is_folder':True}],'unsupported_folder'),
                     ([{'file_name':'missing-key.docx'}],'invalid_media'),('not-a-list','invalid_media')):
   with self.subTest(code=code):
    with self.assertRaises(MediaError) as error:parse('post',{'files':files})
    self.assertEqual(error.exception.code,code)
 def test_unauthorized_user_group_and_app_never_download_or_store(self):
  for e in [self.event(sender='ou_stranger'),self.event(chat_type='group',chat='oc_unlisted'),self.event(chat_type='group',chat='oc_team')]:self.assertFalse(self.adapter.handle_event(e))
  e=self.event();e['header']={'app_id':'cli_other'};self.assertFalse(self.adapter.handle_event(e));self.adapter.pump_once();self.assertEqual(self.downloads,[]);self.assertEqual(self.box.pending(),[])
 def test_group_post_with_mention_and_image_is_allowed(self):
  c={'title':'','content':[[{'tag':'at','user_id':'ou_bot'},{'tag':'text','text':'请看图'}],[{'tag':'img','image_key':'img_group'}]]}
  e=self.event('post',c,chat_type='group',chat='oc_team',mentions=[{'id':{'open_id':'ou_bot'},'key':'@_user_1'}])
  self.assertTrue(self.adapter.handle_event(e));self.adapter.pump_once();self.assertEqual(len(self.downloads),1)
 def test_group_attachment_reply_only_to_same_sender_task(self):
  self.adapter.handle_event(self.f.group_event('om_original'));self.adapter.pump_once();tid=self.box.linked_source('om_original')['task_id'];self.bridge.tasks[tid]={'status':'已完成','result':'请发素材'}
  e=self.event('file',{'file_key':'file_group','file_name':'a.txt'},parent='om_original',chat_type='group',chat='oc_team')
  e['event']['sender']['sender_id']['open_id']='ou_member'
  self.assertTrue(self.adapter.handle_event(e));self.adapter.pump_once();self.assertEqual(self.bridge.submissions[-1][1],tid)
  e=self.event('file',{'file_key':'file_stolen'},mid='om_stolen',parent='om_original',chat_type='group',chat='oc_team',sender='ou_other');self.assertFalse(self.adapter.handle_event(e))
 def test_duplicate_event_and_restart_reuse_frozen_download_and_prompt(self):
  e=self.event();self.assertTrue(self.adapter.handle_event(e));self.assertFalse(self.adapter.handle_event(e));self.bridge.fail_next=True;self.adapter.pump_once();prompt=self.media()['prepared_prompt']
  fresh=FeishuAdapter(self.f.config,Inbox(self.box.path),self.bridge,self.sender);fresh.pump_once();self.assertEqual(len(self.downloads),1);self.assertEqual(self.bridge.submissions[0][0],prompt)
 def test_download_permission_failure_has_reply_and_no_codex_task(self):
  def denied(*_):raise MediaError('resource_permission','机器人缺少附件下载权限（im:resource）。')
  self.sender.download_resource=denied;self.adapter.handle_event(self.event());self.adapter.pump_once();self.adapter.pump_once()
  self.assertEqual(self.media()['state'],'failed');self.assertEqual(len(self.sender.sent),2);self.assertIn('im:resource',self.sender.sent[-1][2]);self.assertEqual(self.bridge.submissions,[]);self.assertEqual(self.box.pending(),[])
 def test_transient_download_retries_three_times_without_leaking_error(self):
  self.sender.download_resource=Mock(side_effect=RuntimeError('TOKEN_SECRET_DO_NOT_SHOW'));self.adapter.handle_event(self.event())
  for _ in range(3):
   with self.box.db() as db:db.execute('UPDATE inbound_media SET next_attempt=0')
   self.adapter.pump_once()
  self.assertEqual(self.sender.download_resource.call_count,3);self.assertEqual(self.media()['state'],'failed');self.assertNotIn('TOKEN_SECRET',json.dumps(self.sender.sent));self.assertEqual(self.bridge.submissions,[])
 def test_malformed_unsupported_and_too_many_media_return_clear_notice(self):
  messages=[self.event('file',{'text':'missing-key'}),self.event('sticker',{'file_key':'file_x'},mid='om_sticker'),self.event('post',{'content':[[{'tag':'img','image_key':'img_'+str(i)}] for i in range(11)]},mid='om_many')]
  for e in messages:self.assertTrue(self.adapter.handle_event(e))
  self.adapter.pump_once();self.assertEqual(len(self.sender.sent),6);self.assertEqual(self.downloads,[]);self.assertEqual(self.bridge.submissions,[])
 def test_untrusted_filename_cannot_escape_attachment_directory(self):
  self.adapter.handle_event(self.event('file',{'file_key':'file_a','file_name':'../../outside/evil\n.txt'}));self.adapter.pump_once();p=Path(self.manifest()[0]['path']);self.assertEqual(p.parent.parent.name,'attachments');self.assertNotIn('\n',p.name);self.assertEqual(p.name,'01-evil_.txt')
 def test_symlink_output_is_rejected(self):
  import hashlib
  folder=Path(self.box.path).parent/'attachments'/hashlib.sha256(b'cli_test:om_media').hexdigest();folder.mkdir(parents=True);target=Path(self.box.path).parent/'sensitive';target.write_text('keep');(folder/'01-image.png').symlink_to(target)
  self.adapter.handle_event(self.event());self.adapter.pump_once();self.assertEqual(target.read_text(),'keep');self.assertEqual(self.media()['state'],'failed');self.assertEqual(self.downloads,[])
 def test_media_does_not_impersonate_manager_or_pairing_confirmation(self):
  self.f.config['assistant_manager']={'admin_open_id':'ou_owner'};self.adapter.handle_event(self.event());self.adapter.pump_once();self.assertIn('对应的业务助手',self.sender.sent[-1][2]);self.assertEqual(self.downloads,[]);self.assertEqual(self.bridge.submissions,[])
 def test_reply_after_failed_media_does_not_wait_forever(self):
  self.adapter.handle_event(self.event('file',{}));self.adapter.pump_once()
  self.adapter.handle_event(fixtures.event('om_retry',text='重新说明要求',parent='om_media'));self.adapter.pump_once();self.assertEqual(len(self.bridge.submissions),1)
 def test_failure_reply_retry_is_stable(self):
  self.adapter.handle_event(self.event('file',{}));original=self.sender.reply;attempts=[]
  def reply(mid,status,body):
   attempts.append((mid,status,body))
   if len(attempts)==1:raise RuntimeError('temporary')
   return original(mid,status,body)
  self.sender.reply=reply;self.adapter.pump_once();self.adapter.pump_once();self.assertEqual(attempts[0],attempts[1]);self.assertEqual(self.box.pending(),[])
 def test_sdk_download_uses_message_resource_endpoint_and_checks_limit(self):
  sender=FeishuSender('fake','fake');sender.client=Mock();response=Mock(success=lambda:True,file=io.BytesIO(b'bytes'));sender.client.im.v1.message_resource.get.return_value=response
  output=io.BytesIO();sender.download_resource('om_source','file_key','file',output,10);req=sender.client.im.v1.message_resource.get.call_args.args[0]
  self.assertEqual((req.message_id,req.file_key,req.type),('om_source','file_key','file'));self.assertEqual(output.getvalue(),b'bytes')
  response.file=io.BytesIO(b'too-large')
  with self.assertRaises(MediaError):sender.download_resource('om_source','file_key','file',io.BytesIO(),2)
 def test_sdk_permission_error_identifies_required_message_read_scope(self):
  sender=FeishuSender('cli_our_bot','fake');sender.client=Mock()
  for code in (99991672,234009):
   with self.subTest(code=code):
    sender.client.im.v1.message_resource.get.return_value=Mock(success=lambda:False,code=code)
    with self.assertRaises(MediaError) as error:sender.download_resource('om_source','file_key','file',io.BytesIO(),MAX_RESOURCE_BYTES)
    self.assertIn('获取与发送单聊、群组消息',error.exception.detail);self.assertIn('（im:message）',error.exception.detail)
    self.assertIn('发布生效',error.exception.detail);self.assertIn('https://open.feishu.cn/app/cli_our_bot/auth',error.exception.detail)

if __name__=='__main__':unittest.main()
