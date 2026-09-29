"""Allowlisted evidence readers. AI supplies intent/paths, never executable validators."""
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit
from codex_http_bridge import BridgeError
from workflow_contract import fingerprint

def file_hash(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()

def cli(arguments):
    p=subprocess.run(['lark-cli',*arguments,'--as','user','--profile','default'],
        env=dict(os.environ,LARKSUITE_CLI_NO_UPDATE_NOTIFIER='1',LARKSUITE_CLI_NO_SKILLS_NOTIFIER='1'),
        capture_output=True,text=True,timeout=45)
    try:value=json.loads(p.stdout or p.stderr)
    except ValueError:raise BridgeError(502,'evidence_unavailable','飞书响应无法解析') from None
    if p.returncode or not value.get('ok'):
        raise BridgeError(502,'evidence_unavailable',value.get('error',{}).get('message','无法读取飞书证据'))
    return value['data']

def document_snapshot(url):
    p=urlsplit(url)
    if p.scheme!='https' or p.netloc!='example.feishu.cn' or not re.fullmatch(r'/docx/[A-Za-z0-9]+/?',p.path) or any(c in url for c in '\n\r<>[]()'):
        raise BridgeError(400,'invalid_document','当前文档校验器需要 42HOW 的真实 docx 链接')
    # Read body twice around the full comments fetch. Reject concurrent edits.
    first=cli(['docs','+fetch','--doc',url,'--detail','with-ids'])['document']
    comments=[];page=None;seen=set()
    while True:
        args=['drive','+list-comments','--url',url,'--need-relation']
        if page:args+=['--page-token',page]
        data=cli(args)
        for item in data.get('items',[]):
            if item.get('has_more'):
                replies=[];rp=None;rs=set()
                while True:
                    args=['drive','+list-replies','--url',url,'--comment-id',item['comment_id']]
                    if rp:args+=['--page-token',rp]
                    rd=cli(args);replies.extend(rd.get('items',[]))
                    if not rd.get('has_more'):break
                    rp=rd.get('page_token')
                    if not rp or rp in rs:raise BridgeError(502,'pagination_error','评论回复分页不完整')
                    rs.add(rp)
                item['reply_list']={'replies':replies};item['has_more']=False
            comments.append(item)
        if not data.get('has_more'):break
        page=data.get('page_token')
        if not page or page in seen:raise BridgeError(502,'pagination_error','评论分页不完整')
        seen.add(page)
    last=cli(['docs','+fetch','--doc',url,'--detail','with-ids'])['document']
    if fingerprint(first)!=fingerprint(last):raise BridgeError(409,'document_changed','读取过程中正文变化，请重新捕获审核版本')
    comments.sort(key=lambda x:x['comment_id'])
    for c in comments:
        c.get('reply_list',{}).get('replies',[]).sort(key=lambda x:x.get('reply_id',''))
    return {'url':url,'document':last,'unresolved_comments':comments}

def xml_signature(path):
    data=Path(path).read_bytes()
    if b'<!ENTITY' in data.upper() or b'SYSTEM ' in data.upper() or b'PUBLIC ' in data.upper():
        raise BridgeError(400,'unsafe_xml','FCPXML 不允许外部实体或外部 DTD')
    try:root=ET.fromstring(data)
    except ET.ParseError:raise BridgeError(400,'invalid_xml','FCPXML 无法解析') from None
    if root.tag!='fcpxml':raise BridgeError(400,'invalid_xml','文件根节点不是 fcpxml')
    # Preserve structure and all attributes except the display name of translated titles.
    def node(n,in_subtitle=False):
        in_subtitle=in_subtitle or n.tag=='text'
        attrs={k:v for k,v in n.attrib.items() if not(n.tag=='title' and k=='name')}
        return [n.tag,attrs,None if in_subtitle else (n.text or '').strip(),
                [node(c,in_subtitle) for c in n]]
    return node(root)

def capture(spec, value, folder, inputs, *, snapshotter=document_snapshot, receipt_reader=None):
    if not isinstance(value,dict):raise BridgeError(400,'invalid_artifact','产物需为对象')
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    kind=spec['kind']
    if kind in ('subtitle_manifest','subtitle_alignment','audio_alignment'):
        from workflow_alignment import capture_alignment
        return capture_alignment(spec,value,folder,inputs)
    if kind in ('file','fcpxml'):
        if set(value)!={'path'} or not isinstance(value['path'],str):raise BridgeError(400,'invalid_artifact','文件产物需要绝对 path')
        source=Path(value['path'])
        if not source.is_absolute() or not source.is_file() or source.stat().st_size==0:raise BridgeError(400,'missing_artifact','产物文件不存在或为空')
        if kind=='fcpxml':
            reference=inputs.get(spec['reference'])
            if not reference or xml_signature(source)!=xml_signature(reference['path']):
                raise BridgeError(409,'timeline_mismatch','FCPXML 结构、资源、样式或时间属性与原文件不一致')
        target=folder/source.name
        shutil.copyfile(source,target);target.chmod(0o600)
        return {'kind':kind,'path':str(target),'original_path':str(source),'sha256':file_hash(target),'bytes':target.stat().st_size}
    if kind=='feishu_document':
        if set(value)!={'url'} or not isinstance(value['url'],str):raise BridgeError(400,'invalid_artifact','文档产物需要真实 URL')
        data=snapshotter(value['url']);target=folder/'document-snapshot.json'
        target.write_text(json.dumps(data,ensure_ascii=False,indent=2));target.chmod(0o600)
        return {'kind':kind,'url':value['url'],'path':str(target),'sha256':file_hash(target),'content_hash':fingerprint(data),
                'unresolved_comment_count':len(data.get('unresolved_comments',[]))}
    if kind=='delivery':
        if set(value)!={'message_ids','file_keys'} or not all(isinstance(value[k],list) and value[k] and all(isinstance(x,str) and x for x in value[k]) for k in value):
            raise BridgeError(400,'invalid_receipt','交付产物需要实际 message_ids 和 file_keys')
        if not receipt_reader:raise BridgeError(503,'receipt_unavailable','缺少飞书消息读回能力')
        evidence=receipt_reader(value)
        target=folder/'delivery-receipt.json';target.write_text(json.dumps(evidence,ensure_ascii=False,indent=2));target.chmod(0o600)
        return {'kind':kind,'path':str(target),'sha256':file_hash(target),**value}
    raise BridgeError(400,'invalid_artifact','未知产物类型')

def check_files(artifacts):
    for key,a in artifacts.items():
        if not Path(a['path']).is_file() or file_hash(a['path'])!=a['sha256']:
            raise BridgeError(409,'artifact_changed','任务账本里的文件已变化或丢失：'+key)
        for f in a.get('dependent_files',[]):
            if not Path(f['path']).is_file() or file_hash(f['path'])!=f['sha256']:
                raise BridgeError(409,'artifact_changed','任务账本里的分段音频已变化或丢失：'+key)
