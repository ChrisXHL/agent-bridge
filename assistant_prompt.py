"""Single-source business prompts, separate from per-conversation runtime boundaries.

Version 1 stores the business template/rule at the application root only. Group
routes retain routing, workspace and authorization metadata, never prompt forks.
Unversioned configurations retain their legacy behavior until explicitly migrated.
"""
from __future__ import annotations

import re

from codex_http_bridge import BridgeError

POLICY_VERSION = 1


def is_shared(config):
    return type(config.get('prompt_policy_version')) is int and config['prompt_policy_version'] == POLICY_VERSION


def business_template(config, fallback=None):
    from feishu_settings import DEFAULT_PROMPT_TEMPLATE, validate_prompt_template
    value = config.get('prompt_template', DEFAULT_PROMPT_TEMPLATE) if is_shared(config) else (
        fallback if fallback is not None else config.get('prompt_template', DEFAULT_PROMPT_TEMPLATE))
    return validate_prompt_template(value)


def shared_rule(config):
    return str(config.get('self_update_rule') or '').strip()


def sync_shared(config, template=None):
    """Normalize shared writes in place; no transport identity or routing changes."""
    if not is_shared(config):
        return config
    from feishu_settings import validate_prompt_template
    config['prompt_template'] = validate_prompt_template(
        template if template is not None else config['prompt_template'])
    for group in (config.get('group_configs') or {}).values():
        group.pop('prompt_template', None)
        group.pop('self_update_rule', None)
    return config


def delivery_context(config, chat_type, chat_id, sender_id):
    if not is_shared(config):
        return ''
    if chat_type == 'group':
        recipient = ('本条来自群聊。只回复可信来源中的当前群；新建交付资料仅向该群添加 view 权限'
                     '（member-type openchat，chat_id 为 ' + chat_id + '）。'
                     '不得给创建者私聊、其他群或个人自动转发，不读取或导入私聊记录。'
                     # Local patch 2026-11-25: an agent sent its chart with
                     # --reply-in-thread, which turned the user's group message
                     # into a topic and pulled Bridge's final reply in with it.
                     '自己往群里发图片、文件或消息时，直接回复原消息，不要加 --reply-in-thread、不要开话题；'
                     '原消息本身就在话题里时，正常回复会自动留在该话题。')
    elif chat_type == 'p2p':
        recipient = ('本条来自已授权用户私聊。只回复当前用户；新建交付资料仅向当前发言人添加 view 权限'
                     '（member-type openid，open_id 为 ' + sender_id + '）。'
                     '不得向任何群分享私聊内容或新建资料，不导入群聊天记录。')
    else:
        raise BridgeError(400, 'invalid_chat_type', '无法核实当前会话交付边界')
    return ('\n\n[Bridge 当前会话运行边界；独立于共享业务 Prompt]\n' + recipient
            + '统一的仅是业务规则，不是历史、文件、授权或工作文件夹。'
              '用户明确要求编辑才添加 edit，不自动扩大旧资料权限。'
              '此边界优先于模板中历史遗留的群聊或私聊接收者描述。')


def neutral_template(template):
    """Remove only known generated recipient lines, preserving custom business text.

    This is for deliberate legacy migration/new generated prompts, not automatic
    rewriting of arbitrary user-authored permissions or instructions.
    """
    from feishu_settings import validate_prompt_template
    lines = []
    for line in validate_prompt_template(template).splitlines(keepends=True):
        stripped = line.strip()
        if (stripped.startswith('- 本任务来自已绑定的群。用 Bridge 末尾可信来源中的 chat_id，')
                or re.match(r'^- 本连接仅允许创建者私聊。将本次新建交付资料的 view 权限授予创建者 ou_[A-Za-z0-9_-]+（member-type openid）。', stripped)
                or stripped in (
                    '本次消息来自已接入的普通群。只在该群回复；不得把群内资料带到私聊或其他群。',
                    '本次消息来自已授权用户的私聊。只回复此用户；不得把私聊资料带到其他会话。')):
            # Current-channel sharing is injected at execution, never selected by
            # a prompt copied from another conversation.
            continue
        lines.append(line)
    return validate_prompt_template(''.join(lines))


def migrate_config(config, *, source_chat_id=None):
    """Return a new shared config, refusing ambiguous legacy group business rules.

    The caller must back up and inspect this local configuration first. No state
    is written and no tasks, routing or credentials are changed by this function.
    """
    import copy
    if is_shared(config):
        return sync_shared(copy.deepcopy(config))
    groups = config.get('group_configs') or {}
    if source_chat_id is not None:
        if source_chat_id not in config.get('allowed_chat_ids', []) or source_chat_id not in groups:
            raise BridgeError(400, 'prompt_source_missing', '指定的群业务 Prompt 不存在')
        source = groups[source_chat_id]
        template = neutral_template(source.get('prompt_template', config['prompt_template']))
        rule = str(source.get('self_update_rule') or config.get('self_update_rule') or '').strip()
    else:
        candidates = [(neutral_template(groups.get(chat, {}).get('prompt_template', config['prompt_template'])),
                       str(groups.get(chat, {}).get('self_update_rule') or config.get('self_update_rule') or '').strip())
                      for chat in config.get('allowed_chat_ids', [])]
        distinct = set(candidates)
        if len(distinct) > 1:
            raise BridgeError(409, 'prompt_source_ambiguous', '多个群的业务规则不同，需确定主版本后再统一')
        if candidates:
            template, rule = candidates[0]
        else:
            template = neutral_template(config['prompt_template'])
            rule = shared_rule(config)
    # Legacy execution combined the owner's global rule with the group's rule.
    # Keep the former as baseline business content rather than silently deleting
    # it. The selected group addition remains the current, higher-priority rule.
    old_global = shared_rule(config)
    if groups and old_global and rule != old_global:
        template += ('\n\n[迁移保留的创建者通用业务规则；冲突时以当前共享追加规则为准]\n' + old_global)
        from feishu_settings import validate_prompt_template
        validate_prompt_template(template)
    updated = copy.deepcopy(config)
    updated.update(prompt_policy_version=POLICY_VERSION, prompt_template=template, self_update_rule=rule)
    updated.pop('unified_prompt_chat_id', None)
    return sync_shared(updated)
