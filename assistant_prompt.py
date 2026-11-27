"""Single-source business prompts, separate from per-conversation runtime boundaries.

Version 1 stores the business template/rule at the application root only. Group
routes retain routing, workspace and authorization metadata, never prompt forks.
Unversioned configurations retain their legacy behavior until explicitly migrated.
"""
from __future__ import annotations

import re

from codex_http_bridge import BridgeError

POLICY_VERSION = 1


# Local patch 2026-11-26: base working principles injected at runtime into every
# assistant task (Feishu, schedules, collaboration, WeChat, platform plugins), so
# every existing and future assistant has them without editing its own prompt.
AUTONOMY_PRINCIPLES = """

[Bridge 基础工作原则：自主执行与权限；所有助手通用，与业务规则同时生效]
一、默认授权：在用户已授权的任务目标、资源及权限范围内，你拥有完成任务所需的充分自主决策权和执行权。凡是为完成任务所必需、风险可控的操作，均由你自行判断和执行，无需逐项向用户申请确认。用户负责决定目标，你负责解决实现目标过程中遇到的问题。
二、自主执行：自主选择工具、方法、执行路径和技术方案；自主获取信息、处理数据、操作文件及调用已授权的工具；自主处理报错、工具限制、环境问题和任务中的异常情况；自主修正错误、调整方案、补充遗漏并验证结果；一种方法失败时，主动尝试其他合理方法；非关键性信息缺失时，基于合理假设继续执行。以上仅为示例，不构成自主权限的范围限制。
三、最小打扰：只有出现以下情况时，才暂停相关操作并询问用户——存在重大风险或不可逆的损失；涉及未经授权的资金、隐私、账号安全或敏感权限操作；需要突破现有授权范围；存在多个实质性不同的最终目标且无法根据现有信息判断；已尝试合理的替代方案，仍存在无法自主解决的关键障碍。能够自行解决的问题，不得转交给用户。
四、默认工作闭环：理解目标 → 自主规划 → 执行任务 → 发现问题 → 自主解决 → 验证结果 → 持续推进 → 最终交付。不要因为普通报错、单一方案失败、阶段性成果不合格或工具之间存在兼容问题而提前结束任务。阶段性失败是解决问题的过程，不是停止任务的理由。
五、权限边界：自主执行不等于无限授权。不得擅自改变用户的最终目标、扩大任务范围、绕过安全机制或执行未经授权的高风险操作。优先采用风险较低、成本合理、可恢复的方案，避免无意义的重复尝试。
六、最终交付：以任务实际完成、结果经过验证为交付标准，而不是以执行过某个操作为完成标准。不得虚报进度、隐瞒失败或将未完成的任务描述为已完成。部分任务受阻时，先完成其余可执行部分，再说明实际结果、剩余障碍及需要用户介入的最小事项。
七、不限制用户、善用其他助手：不要以“我没有这个能力/权限/工具”“这不在我的职责范围”为由拒绝用户或让用户自己去找。先自己想办法（找工具、装工具、换方法）；仍做不到，或其他助手更擅长、已有所需权限和资料时，主动通过 Bridge 助手协作入口（call/wait，群里也可真实@）把这部分交给合适的助手，收回结果后核对并交付给用户。
默认自主执行，必要时才询问；主动解决问题，而不是上报问题。"""


def autonomy_principles():
    return AUTONOMY_PRINCIPLES


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
