"""Name-based conversation configuration; the model interprets intent, this module validates it."""
import copy
import hashlib
import json
from codex_http_bridge import BridgeError
from feishu_settings import CONFIG_LOCK, load, public, save
from card_config import validate_cards
from workflow_contract import summarize


def context(path, store):
    cfg = public(load(path))
    connections = [{k: c[k] for k in ('id', 'source', 'project_id', 'project_name', 'status')}
                   for c in store.connections()]
    groups = []
    for chat in cfg['allowed_chat_ids']:
        route = next((r for r in cfg['routes'] if r['chat_id'] == chat), None)
        groups.append({'chat_id': chat, 'name': route['name'] if route else None,
                       'configured': bool(route),
                       'workflow': route or {'connection_id': cfg['connection_id'],
                           'prompt_template': cfg['prompt_template'],
                           'review_prompt_template': cfg['review_prompt_template'],
                           'card_types': cfg['default_card_types'], 'enabled': True}})
    result = {'groups': groups, 'projects': connections, 'cards': cfg['cards'],
              'default_card_types': cfg['default_card_types'], 'enabled': cfg['enabled'],
              'app_id': cfg['app_id']}
    result['revision'] = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return result


def resolve(items, value, keys, kind):
    matches = [x for x in items if any(x.get(k) == value for k in keys)]
    if len(matches) != 1:
        raise BridgeError(409, 'ambiguous_target' if matches else 'target_not_found',
                          kind + ('有多个同名对象，请使用已读取的唯一标识' if matches else '未找到，请先读取现有配置；不能猜测目标'))
    return matches[0]


def configure(path, store, args):
    allowed = {'revision', 'group', 'name', 'project', 'instructions', 'card', 'new_card',
               'on_approve', 'action', 'dry_run', 'review_instructions', 'workflow'}
    if not isinstance(args, dict) or set(args) - allowed or not {'revision', 'group'} <= set(args):
        raise BridgeError(400, 'invalid_workflow', '配置参数无效')
    if not isinstance(args.get('dry_run', True), bool):
        raise BridgeError(400, 'invalid_workflow', 'dry_run 必须为布尔值')
    for key in allowed - {'new_card', 'dry_run', 'workflow'}:
        if key in args and (not isinstance(args[key], str) or not args[key].strip()):
            raise BridgeError(400, 'invalid_workflow', key + ' 必须为非空文本')
    with CONFIG_LOCK:
        before = context(path, store)
        if args.get('revision') != before['revision']:
            raise BridgeError(409, 'config_changed', '配置已变化，请重新读取并核对本次修改')
        group = resolve(before['groups'], args.get('group'), ('chat_id', 'name'), '群')
        route = copy.deepcopy(group['workflow'])
        route.update(chat_id=group['chat_id'], name=args.get('name') or group['name'] or '')
        if 'workflow' in args:
            route['workflow']=args['workflow']
        if not route['name']:
            raise BridgeError(400, 'group_name_required', '这个群尚未命名，请提供用户认可的群名称')
        if 'project' in args:
            connection = resolve(before['projects'], args['project'], ('id', 'project_id', 'project_name'), '项目连接')
            route['connection_id'] = connection['id']
        if 'instructions' in args:
            instruction = args['instructions']
            route['prompt_template'] = instruction if '{{message}}' in instruction else '飞书任务：{{message}}\n\n' + instruction
        if 'review_instructions' in args:
            instruction = args['review_instructions']
            route['review_prompt_template'] = instruction if '{{message}}' in instruction else instruction + '\n\n本次回传：{{message}}'
        cards = copy.deepcopy(before['cards'])
        if 'new_card' in args:
            new = validate_cards([args['new_card']])[0]
            old = next((c for c in cards if c['id'] == new['id']), None)
            if old and old != new:
                raise BridgeError(409, 'shared_card_conflict', '此标识已有卡片；请用新标识，避免覆盖其他群的配置')
            if not old:
                cards.append(new)
        selected = args.get('card') or (args.get('new_card') or {}).get('id')
        if 'on_approve' in args and not selected:
            if len(route.get('card_types', [])) != 1:
                raise BridgeError(409, 'card_required', '请明确要修改哪张卡片的回传行为')
            selected = route['card_types'][0]
        changes = []
        if selected:
            card = copy.deepcopy(resolve(cards, selected, ('id', 'name'), '卡片'))
            original_id = card['id']
            if 'on_approve' in args:
                actions = card['actions']
                if 'action' in args:
                    action = resolve(actions, args['action'], ('name', 'label'), '按钮')
                elif len(actions) == 1:
                    action = actions[0]
                else:
                    raise BridgeError(409, 'action_required', '这张卡片有多个按钮，请明确要修改哪个按钮')
                # Stable group-local identity; repeated changes never touch a shared card.
                suffix = hashlib.sha256((group['chat_id'] + ':' + card['id']).encode()).hexdigest()[:16]
                if not (card['id'].startswith('workflow_') and card['id'] in route.get('card_types', [])):
                    card['id'] = 'workflow_' + suffix
                card['name'] = card['name'].split(' · 群：')[0] + ' · 群：' + route['name']
                data = ['\n\n本次卡片数据：']
                data += [key + '：{{variables.' + key + '}}' for key in card['variables']]
                data += [key + '：{{form.' + key + '}}' for key in action['fields']]
                action['prompt_template'] = args['on_approve'] + '\n'.join(data)
                cards = [c for c in cards if c['id'] != card['id']] + [card]
                changes.append('仅此群的“' + action['label'] + '”后续处理：' + args['on_approve'])
                route['card_types'] = [c for c in route.get('card_types', []) if c != original_id]
                # Keep literal instructions aligned when the previous tool ID was explicit.
                route['prompt_template'] = route['prompt_template'].replace(original_id, card['id'])
            route['card_types'] = list(dict.fromkeys([*route.get('card_types', []), card['id']]))
            changes.append('使用卡片：' + card['name'])
            # The normal task prompt supplies original message identity and command fallback.
            # Runtime already supplies card IDs and transport instructions; keep business prompts editable.
            marker = '\n\n[流程卡片约定]'
            route['prompt_template'] = route['prompt_template'].split(marker)[0]
        cfg = public(load(path))
        routes = [r for r in cfg['routes'] if r['chat_id'] != group['chat_id']] + [route]
        payload = {'cards': cards, 'routes': routes}
        after = save(path, payload, store, validate_only=True)
        project = next((p['project_name'] for p in before['projects'] if p['id'] == route['connection_id']), None)
        dry = args.get('dry_run', True)
        if not dry:
            save(path, payload, store)
        return {'status': 'preview' if dry else 'saved', 'group': route['name'], 'project': project,
                'changes': changes, 'workflow': route,
                'cards': [c for c in after['cards'] if c['id'] in route['card_types']],
                'revision': before['revision'] if dry else context(path, store)['revision'],
                'stages': summarize(route['workflow']) if route.get('workflow') else None,
                'scope': '只影响此群的新任务；已发卡片和其他群保留原规则',
                'external_validation': '配置校验通过；新增飞书模板仍需实际发布并关联应用，保存不会发送消息'}
