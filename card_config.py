"""Declarative CardKit templates and validated callback forms. No executable templates."""
from __future__ import annotations
import copy
import json
import re
from urllib.parse import urlsplit
from codex_http_bridge import BridgeError
from feishu_review import TEMPLATE_ID, TEMPLATE_VERSION, EXAMPLE_VOICE_MATERIAL

IDENT = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,63}$')
TOKEN = re.compile(r'\{\{(variables|form)\.([A-Za-z][A-Za-z0-9_]*)\}\}')

def fail(message):
    raise BridgeError(400, 'invalid_card_config', message)

def default_cards():
    choice = {'voice':'example_voice','material_url':EXAMPLE_VOICE_MATERIAL}
    return [{'id':'voice_review','name':'文档审核与音色确认','template_id':TEMPLATE_ID,
             'version':TEMPLATE_VERSION,
             'variables':{'review_doc_link':{'type':'url','required':True}},
             'actions':[{'name':'approve_voice_material','label':'审核通过',
                         'fields':{'voice':{'type':'choice','required':True,
                                  'options':[{'value':json.dumps(choice,ensure_ascii=False,separators=(',',':')),'data':choice}]}},
                         'prompt_template':'飞书文档已由任务发起人审核通过，请继续原任务。\n审核文档：{{variables.review_doc_link}}\n选定音色与绑定素材：{{form.voice}}\n请使用上述文档和音色素材执行原任务的后续处理，并说明结果。'}]}]

def valid_url(value):
    try:
        p=urlsplit(value)
        return p.scheme in ('https','http') and bool(p.hostname) and not p.username and not p.password
    except (ValueError,TypeError):
        return False

def validate_fields(fields, *, choices=False):
    if not isinstance(fields,dict) or len(fields)>30: fail('字段配置必须是对象，最多 30 个字段')
    for name,spec in fields.items():
        if not isinstance(name,str) or not IDENT.fullmatch(name) or not isinstance(spec,dict): fail('字段名或配置无效')
        if set(spec)-{'type','required','options'}: fail('字段配置包含未知属性')
        if spec.get('type') not in (['text','url','number','boolean','object']+(['choice'] if choices else [])): fail('字段类型无效')
        if not isinstance(spec.get('required',False),bool): fail('required 必须为布尔值')
        if spec['type']=='choice':
            opts=spec.get('options')
            if not isinstance(opts,list) or not 1<=len(opts)<=100: fail('选择字段需配置 1 到 100 个选项')
            values=[]
            for opt in opts:
                if not isinstance(opt,dict) or set(opt)!={'value','data'} or not isinstance(opt['value'],str): fail('选项需包含 value 和 data')
                if opt['value'] in values: fail('选项值不能重复')
                values.append(opt['value'])
        elif 'options' in spec: fail('只有 choice 字段可配置 options')

def validate_cards(cards):
    if not isinstance(cards,list) or len(cards)>50: fail('最多配置 50 种卡片')
    ids=set()
    for card in cards:
        if not isinstance(card,dict) or set(card)!={'id','name','template_id','version','variables','actions'}: fail('卡片配置字段无效')
        cid=card['id']
        if not isinstance(cid,str) or not IDENT.fullmatch(cid) or cid in ids: fail('卡片标识无效或重复')
        ids.add(cid)
        for k in ['name','template_id','version']:
            if not isinstance(card[k],str) or not 1<=len(card[k])<=120: fail(k+' 必须填写')
        validate_fields(card['variables'])
        if not isinstance(card['actions'],list) or len(card['actions'])>20: fail('最多配置 20 个回传按钮')
        names=set()
        for act in card['actions']:
            if not isinstance(act,dict) or set(act)!={'name','label','fields','prompt_template'}: fail('按钮配置字段无效')
            if not isinstance(act['name'],str) or not IDENT.fullmatch(act['name']) or act['name'] in names: fail('按钮名称无效或重复')
            names.add(act['name'])
            if not isinstance(act['label'],str) or not 1<=len(act['label'])<=100: fail('按钮说明必须填写')
            validate_fields(act['fields'],choices=True)
            prompt=act['prompt_template']
            if not isinstance(prompt,str) or not 1<=len(prompt)<=8000: fail('回传指令需为 1 到 8000 字')
            for kind,name in TOKEN.findall(prompt):
                if name not in (card['variables'] if kind=='variables' else act['fields']): fail('回传指令引用了未定义字段：'+name)
            if '{{' in TOKEN.sub('',prompt): fail('回传指令占位符需为 {{variables.字段}} 或 {{form.字段}}')
    if len(json.dumps(cards,ensure_ascii=False).encode())>100000: fail('卡片配置过长')
    return copy.deepcopy(cards)

def values_for(schema, values):
    if not isinstance(values,dict) or set(values)-set(schema): fail('传入字段不匹配卡片配置')
    out={}
    for name,spec in schema.items():
        v=values.get(name)
        if v is None:
            if spec.get('required'): fail('缺少必填字段：'+name)
            continue
        typ=spec['type']
        ok=False
        if typ in ('text','url'): ok=isinstance(v,str) and len(v)<=8000 and (typ!='url' or valid_url(v))
        elif typ=='number': ok=isinstance(v,(int,float)) and not isinstance(v,bool) and __import__('math').isfinite(v)
        elif typ=='boolean': ok=isinstance(v,bool)
        elif typ=='object': ok=isinstance(v,(dict,list))
        elif typ=='choice':
            for opt in spec['options']:
                # Feishu may reserialize JSON option strings; compare parsed objects only for JSON values.
                matches=v==opt['value']
                if not matches and isinstance(v,str):
                    try: matches=json.loads(v)==json.loads(opt['value'])
                    except (ValueError,TypeError): pass
                if matches: v=copy.deepcopy(opt['data']);ok=True;break
        if not ok: fail('字段值无效：'+name)
        out[name]=v
    if len(json.dumps(out,ensure_ascii=False).encode())>12000: fail('卡片字段内容过长')
    return out

def card_content(card, variables):
    values=values_for(card['variables'],variables)
    values={k:({'url':v} if card['variables'][k]['type']=='url' else v) for k,v in values.items()}
    return json.dumps({'type':'template','data':{'template_id':card['template_id'],
                     'template_version_name':card['version'],'template_variable':values}},ensure_ascii=False)

def callback_prompt(card, action_name, variables, form):
    act=next((x for x in card['actions'] if x['name']==action_name),None)
    if not act: fail('未配置这个回传按钮')
    form=values_for(act['fields'],form)
    def sub(m):
        v=(variables if m[1]=='variables' else form).get(m[2],'')
        return v if isinstance(v,str) else json.dumps(v,ensure_ascii=False)
    prompt=TOKEN.sub(sub,act['prompt_template'])
    if len(prompt)>12000 or len(json.dumps({'message':prompt},ensure_ascii=False).encode())>14000: fail('合成的回传指令过长')
    return prompt
