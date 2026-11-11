#!/usr/bin/env python3
"""Real Codex attachment readback, isolated Feishu events/downloads/replies."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json,struct,subprocess,threading,time,wave,zlib
from http.server import ThreadingHTTPServer
from service import Store,Engine,LocalCodexTransport,make_handler
from feishu_groups import atomic_write
from feishu_worker import Inbox,FeishuAdapter,BridgeClient

root=Path(__file__).resolve().parents[1]/'artifacts'/('feishu-media-live-'+time.strftime('%Y%m%d-%H%M%S'))
root.mkdir(parents=True,mode=0o700);project=root/'work';project.mkdir()
atomic_write(root/'projects.json',{'media-test':str(project)})
store=Store(root/'bridge.sqlite3',root/'projects.json',root/'no-codex-state');connection=store.create_connection('媒体隔离验收','media-test')
def chunk(t,b):return struct.pack('!I',len(b))+t+b+struct.pack('!I',zlib.crc32(t+b)&0xffffffff)
w,h=160,100;pixels=b''.join(b'\0'+b'\xff\0\0'*(w//2)+b'\0\0\xff'*(w//2) for _ in range(h))
picture=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',w,h,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(pixels))+chunk(b'IEND',b'')
clip=root/'clip.mp4';subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=lime:s=160x90:d=1','-c:v','libx264','-pix_fmt','yuv420p',str(clip)],check=True)
audio=root/'voice.wav'
with wave.open(str(audio),'wb') as f:f.setnchannels(1);f.setsampwidth(2);f.setframerate(8000);f.writeframes(b'\0\0'*4000)
payloads={'img_fixture':picture,'file_video':clip.read_bytes(),'file_csv':b'value\n17\n28\n4\n','file_audio':audio.read_bytes()}
class Sender:
 def __init__(self):self.downloads=[];self.replies=[]
 def download_resource(self,mid,key,kind,output,limit):self.downloads.append((mid,key,kind));output.write(payloads[key])
 def reply(self,mid,status,body):self.replies.append((mid,status));return 'om_reply_'+mid+'_'+status
sender=Sender();engine=Engine(store,LocalCodexTransport(),interval=1)
server=ThreadingHTTPServer(('127.0.0.1',0),None);base='http://127.0.0.1:'+str(server.server_port);server.RequestHandlerClass=make_handler(store,engine,base)
threading.Thread(target=server.serve_forever,daemon=True).start()
config={'app_id':'cli_media_test','enabled':True,'connection_id':connection['id'],'allowed_open_ids':['ou_test'],'allowed_chat_ids':[]}
box=Inbox(root/'inbox.sqlite3');adapter=FeishuAdapter(config,box,BridgeClient(base,connection['api_key']),sender)
def receive(mid,kind,content,instruction):
 config['prompt_template']='用户消息：{{message}}\n\n'+instruction+'\n只读取本次明确提供的附件。不要调用外部服务、发送消息或修改配置，结果由 Bridge 返回。'
 event={'header':{'app_id':config['app_id']},'event':{'sender':{'sender_type':'user','sender_id':{'open_id':'ou_test'}},'message':{'message_id':mid,'chat_id':'oc_test','chat_type':'p2p','message_type':kind,'content':json.dumps(content)}}}
 assert adapter.handle_event(event)
receive('om_visual','post',{'title':'检查附件','content':[[{'tag':'text','text':'核实这张图片和视频的实际内容。'}],[{'tag':'img','image_key':'img_fixture'}],[{'tag':'media','file_key':'file_video','file_name':'clip.mp4'}]]},'必须实际使用查看图片工具打开图片，返回图片左右两半的颜色。必须实际用 ffprobe 读取视频宽高和时长。只返回 JSON，字段 left、right 使用英语基本颜色词；另含 width、height、duration。')
receive('om_file','file',{'file_key':'file_csv','file_name':'records.csv'},'实际打开读取 CSV 附件，求 value 列总和及数据行数。只返回 JSON，字段 total 和 records。')
receive('om_audio','audio',{'file_key':'file_audio','file_name':'voice.wav'},'实际读取音频附件并用音频工具核验采样率、声道数和时长，不需要转写。只返回 JSON，字段 sample_rate、channels、duration。')
adapter.pump_once();tasks={mid:box.linked_source(mid)['task_id'] for mid in ('om_visual','om_file','om_audio')}
print('ARTIFACT '+str(root),flush=True);engine.start();last=None;deadline=time.monotonic()+900
try:
 while time.monotonic()<deadline:
  state={mid:store.task(tid)['status'] for mid,tid in tasks.items()}
  if state!=last:print(json.dumps(state,ensure_ascii=False),flush=True);last=state
  if all(s in ('已完成','失败','已停止') for s in state.values()):break
  time.sleep(3)
 results={mid:{'status':store.task(tid)['status'],'result':store.task(tid)['result'],'thread_id':store.task(tid)['thread_id']} for mid,tid in tasks.items()}
 verification={'tasks':results,'downloads':len(sender.downloads),'real_codex_execution':True,'feishu_events_and_downloads_simulated':True,'real_feishu_delivery':False}
 atomic_write(root/'verification.json',verification);print(json.dumps(verification,ensure_ascii=False),flush=True)
 assert all(x['status']=='已完成' for x in results.values())
 def decoded(mid):return json.loads(results[mid]['result'].strip().removeprefix('```json').removesuffix('```').strip())
 v=decoded('om_visual');f=decoded('om_file');a=decoded('om_audio')
 assert v['left']=='red' and v['right']=='blue' and v['width']==160 and v['height']==90 and abs(float(v['duration'])-1)<0.1
 assert f=={'total':49,'records':3}
 assert int(a['sample_rate'])==8000 and a['channels']==1 and abs(float(a['duration'])-0.5)<0.01
finally:engine.close();server.shutdown();server.server_close()
