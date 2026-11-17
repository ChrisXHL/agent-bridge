"""Apply a configuration-only file to an explicitly named local bot, never guess identity."""
from __future__ import annotations

import copy
import re
from pathlib import Path

from assistant_prompt import is_shared


def apply_template_file(bundle, base, target_ref, inspect=False, api=None, home=None):
    from assistant_intake import API, ROOT, IntakeError, backup, lock, outcome, safe_path
    from assistant_template import _validate_bundle
    assistant = _validate_bundle(bundle)
    if not target_ref:
        return outcome('needs_target', user_action_required=True,
                       next_action='ask_for_target_bot_or_official_creation', warnings=['template_has_no_bot_identity'])
    if not isinstance(target_ref, str) or not re.fullmatch(r'default|app_[a-f0-9]{16}', target_ref):
        raise IntakeError('invalid_target_ref')
    api = api or API(base)
    home = Path(home or Path.home()).resolve()
    instance = api.call('/api/bridge-instance')
    if (not isinstance(instance, dict) or instance.get('product') != 'codex-bridge'
            or Path(instance.get('source_root', '')).resolve() != ROOT.resolve()
            or not isinstance(instance.get('state_dir'), str)):
        raise IntakeError('wrong_bridge_instance')
    state = safe_path(instance['state_dir'])
    if not state.is_dir():
        raise IntakeError('wrong_bridge_instance')

    def read():
        catalog = api.call('/api/feishu/catalog')
        matches = [a for a in catalog.get('apps', []) if a.get('ref') == target_ref]
        if len(matches) != 1:
            raise IntakeError('target_not_found')
        target = matches[0]
        if not target.get('migration_supported'):
            raise IntakeError('special_assistant_cannot_apply_template')
        return catalog, target

    def prepare(target):
        projects = api.call('/api/projects')
        selected = [p for p in projects if p.get('id') == target.get('project_id')]
        if len(selected) != 1:
            raise IntakeError('agent_fix_target_workspace')
        folder = safe_path(selected[0]['path'])
        if not folder.is_dir():
            raise IntakeError('agent_fix_target_workspace')
        # Generated runtime sections contain the target's identity, CLI profile,
        # sharing scope and local paths. Keep the target's section byte-for-byte.
        marker = '\n运行规则：\n'
        source_prompt = assistant['prompt_template']
        target_prompt = target.get('prompt_template', '')
        if marker in source_prompt:
            if source_prompt.count(marker) != 1 or target_prompt.count(marker) != 1:
                raise IntakeError('agent_review_template_runtime')
            prompt = source_prompt.split(marker)[0] + marker + target_prompt.split(marker)[1]
            business = source_prompt.split(marker)[0]
        else:
            # Plain business templates still inherit the target's operational
            # boundary; omission is not permission to discard identity rules.
            business = source_prompt
            if target_prompt.count(marker) != 1:
                raise IntakeError('agent_review_target_runtime')
            prompt = source_prompt + marker + target_prompt.split(marker)[1]
        # Foreign bot identities and machine paths in business rules require
        # semantic Agent review rather than blind text substitution.
        sensitive = re.compile(r'(?:cli|ou|oc)_[A-Za-z0-9_-]+|--profile\s|/(?:Users|home|opt|Volumes|Applications)/')
        if sensitive.search(business) or sensitive.search(assistant['self_update_rule']):
            raise IntakeError('agent_review_template_business_references')
        local = copy.deepcopy(bundle)
        local['assistant']['prompt_template'] = prompt
        return local

    catalog, target = read()
    local = prepare(target)
    result = {'name': target.get('name', '助手'), 'ref': target_ref,
              'enabled': bool(target.get('enabled')), 'execution_backend': target.get('execution_backend')}
    warnings = ['target_runtime_and_identity_preserved']
    if assistant['backend'] != target.get('execution_backend'):
        warnings.append('source_backend_not_inherited')
    same = (target.get('prompt_template') == local['assistant']['prompt_template']
            and target.get('self_update_rule', '') == assistant['self_update_rule'])
    if same:
        return outcome('already_applied', [{**result, 'modified': False}], warnings)
    if inspect:
        return outcome('planned', [{**result, 'stage': 'would_update_rules', 'modified': False}], warnings,
                       next_action='apply_template_to_explicit_target')
    with lock(state):
        current_catalog, current = read()
        if current.get('revision') != target.get('revision'):
            raise IntakeError('target_changed_retry_review')
        backup(state, home)
        groups_before = [g for g in current_catalog.get('groups', []) if g.get('app_ref') == target_ref]
        api.call('/api/feishu/assistant-template/apply', {
            'bundle': local, 'target_ref': target_ref, 'revision': current['revision'],
            'project_id': current['project_id']})
        after_catalog, after = read()
        groups_after = [g for g in after_catalog.get('groups', []) if g.get('app_ref') == target_ref]
        # Shared targets intentionally change every effective group prompt/rule,
        # while all route, workspace, membership and backend fields must survive.
        def stable(groups):
            ignored = {'revision'} | ({'prompt_template', 'self_update_rule'} if is_shared(current) else set())
            return [{k: v for k, v in g.items() if k not in ignored} for g in groups]
        shared_verified = (not is_shared(current) or all(
            g.get('prompt_template') == local['assistant']['prompt_template']
            and g.get('self_update_rule', '') == assistant['self_update_rule'] for g in groups_after))
        if (after.get('prompt_template') != local['assistant']['prompt_template']
                or after.get('self_update_rule', '') != assistant['self_update_rule']
                or any(after.get(k) != current.get(k) for k in
                       ('app_id', 'enabled', 'allowed_open_ids', 'project_id', 'execution_backend', 'prompt_policy_version'))
                or not shared_verified or stable(groups_before) != stable(groups_after)):
            raise IntakeError('template_readback_needs_review')
    return outcome('applied', [{**result, 'modified': True, 'rules_verified': True}], warnings,
                   next_action='verify_new_message_execution_when_authorized')
