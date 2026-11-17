"""Agent-facing, fail-closed intake of local assistant configuration files."""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from urllib import request
from urllib.parse import urlsplit

from assistant_prompt import is_shared

ROOT = Path(__file__).resolve().parent
MAX_BYTES = 1024 * 1024
FORMAT = 'codex-bridge-feishu-migration'


class IntakeError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def outcome(stage, results=None, warnings=None, user_action_required=False, next_action='none'):
    return {'stage': stage, 'results': results or [], 'user_action_required': user_action_required,
            'next_action': next_action, 'warnings': warnings or []}


def safe_path(path):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in [path, *path.parents]):
        raise IntakeError('unsafe_symlink_path')
    return path


def private_json(path, value):
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, name = tempfile.mkstemp(prefix='.intake-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(name, path)
    except Exception:
        # Never delete a computed path: leave a private temp file for diagnosis.
        raise IntakeError('receipt_write_failed') from None


def bridge_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1')
                or not parsed.port or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ('', '/')):
            raise ValueError()
    except (ValueError, TypeError):
        raise IntakeError('invalid_loopback_url') from None
    return value.rstrip('/')


class API:
    def __init__(self, base):
        self.base = bridge_url(base)
        # Local credentials must never follow a redirect to an external host.
        class NoRedirect(request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.opener = request.build_opener(request.ProxyHandler({}), NoRedirect())

    def call(self, path, data=None):
        req = request.Request(self.base + path,
            data=json.dumps(data, ensure_ascii=False).encode() if data is not None else None,
            headers={'Content-Type': 'application/json', 'Origin': self.base},
            method='POST' if data is not None else 'GET')
        try:
            with self.opener.open(req, timeout=60) as response:
                raw = response.read(4 * MAX_BYTES + 1)
                if len(raw) > 4 * MAX_BYTES:
                    raise ValueError()
                return json.loads(raw)
        except Exception:
            raise IntakeError('local_api_failed') from None


def read_bundle(path):
    try:
        path = Path(path).expanduser()
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise ValueError()
        raw = path.read_bytes()
        if len(raw) > MAX_BYTES:
            raise ValueError()
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        bundle = json.loads(raw, object_pairs_hook=pairs)
        if not isinstance(bundle, dict):
            raise ValueError()
        # API export responses wrap the portable bundle once.
        if 'bundle' in bundle and set(bundle) <= {'bundle', 'count', 'warnings'}:
            bundle = bundle['bundle']
        if not isinstance(bundle, dict):
            raise ValueError()
        return bundle, hashlib.sha256(json.dumps(bundle, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    except Exception:
        raise IntakeError('invalid_file') from None


def validate(bundle):
    if bundle.get('format') == 'codex-bridge-assistant-template':
        from assistant_template import _validate_bundle
        try:
            _validate_bundle(bundle)
        except Exception:
            raise IntakeError('invalid_template') from None
        return 'template'
    if bundle.get('format') != FORMAT or type(bundle.get('version')) is not int:
        raise IntakeError('unknown_format')
    if bundle['version'] == 1:
        return 'encrypted'
    if bundle['version'] != 2 or set(bundle) != {'format', 'version', 'apps'}:
        raise IntakeError('invalid_bundle')
    apps = bundle['apps']
    if not isinstance(apps, list) or not 1 <= len(apps) <= 100:
        raise IntakeError('invalid_bundle')
    from feishu_settings import validate_prompt_template
    seen = set()
    try:
        for app in apps:
            required = {'app_id', 'app_secret', 'kind', 'name', 'prompt_template', 'allowed_open_ids',
                        'project_path', 'backend', 'groups'}
            if (not isinstance(app, dict) or not required <= set(app)
                    or set(app) - required - {'self_update_rule', 'self_update_owner_open_id', 'prompt_policy_version'}
                    or ('prompt_policy_version' in app and not is_shared(app))
                    or not isinstance(app['app_id'], str) or not re.fullmatch(r'cli_[a-z0-9]{16}', app['app_id'])
                    or app['app_id'] in seen or app['kind'] != 'standard'
                    or not isinstance(app['app_secret'], str) or not 1 <= len(app['app_secret']) <= 512
                    or any(ord(c) < 32 for c in app['app_secret'])
                    or not isinstance(app['name'], str) or not 1 <= len(app['name'].strip()) <= 60
                    or any(ord(c) < 32 for c in app['name'])
                    or app['backend'] not in ('codex', 'claude')
                    or not isinstance(app['allowed_open_ids'], list) or len(app['allowed_open_ids']) > 100
                    or any(not isinstance(x, str) or not re.fullmatch(r'ou_[A-Za-z0-9_-]{1,125}', x)
                           for x in app['allowed_open_ids'])
                    or not isinstance(app['project_path'], str) or len(app['project_path']) > 4096
                    or not isinstance(app['groups'], list) or len(app['groups']) > 100):
                raise ValueError()
            seen.add(app['app_id'])
            validate_prompt_template(app['prompt_template'])
            chats = set()
            for group in app['groups']:
                if (not isinstance(group, dict) or not {'chat_id', 'name', 'prompt_template', 'enabled',
                        'topic_collaboration', 'project_path', 'backend'} <= set(group)
                        or set(group) - {'chat_id', 'name', 'prompt_template', 'enabled',
                            'topic_collaboration', 'project_path', 'backend', 'self_update_rule'}
                        or not isinstance(group['chat_id'], str)
                        or not re.fullmatch(r'oc_[A-Za-z0-9_-]{1,125}', group['chat_id'])
                        or group['chat_id'] in chats or group['backend'] not in ('codex', 'claude')
                        or not isinstance(group['name'], str) or not 1 <= len(group['name']) <= 60
                        or not isinstance(group['project_path'], str) or len(group['project_path']) > 4096
                        or type(group['enabled']) is not bool or type(group['topic_collaboration']) is not bool):
                    raise ValueError()
                chats.add(group['chat_id'])
                validate_prompt_template(group['prompt_template'])
            for record in [app, *app['groups']]:
                if not isinstance(record.get('self_update_rule', ''), str) :
                    raise ValueError()
            owner = app.get('self_update_owner_open_id', '')
            if not isinstance(owner, str) or owner and not re.fullmatch(r'ou_[A-Za-z0-9_-]{1,125}', owner):
                raise ValueError()
    except Exception:
        raise IntakeError('invalid_bundle') from None
    return 'migration'


def profiles(home):
    path = safe_path(home / '.lark-cli' / 'config.json')
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        apps = data['apps']
        if not isinstance(apps, list) or any(not isinstance(x, dict) for x in apps):
            raise ValueError()
        return apps
    except Exception:
        raise IntakeError('cli_profiles_need_review') from None


def profile_for(app, home):
    apps = profiles(home)
    matches = [x for x in apps if x.get('appId') == app['app_id']]
    if len(matches) > 1:
        raise IntakeError('cli_profile_ambiguous')
    if matches:
        prior_secret = matches[0].get('appSecret')
        # lark-cli may store the secret as a keychain reference ({source, id});
        # only a plain-text secret can be compared here.
        if isinstance(prior_secret, str) and prior_secret != app['app_secret']:
            raise IntakeError('cli_profile_credentials_changed')
        name = matches[0].get('name') or 'default'
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', name):
            raise IntakeError('cli_profile_needs_review')
        return name, False
    name = 'bridge-import-' + app['app_id']
    if any(x.get('name') == name for x in apps):
        raise IntakeError('cli_profile_conflict')
    return name, True


def run_cli(argv, secret=None):
    try:
        result = subprocess.run(argv, input=(secret + '\n').encode() if secret is not None else None,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        return result.returncode == 0
    except Exception:
        return False


def backend_ready(backend):
    binary = shutil.which(backend)
    if not binary and backend == 'codex':
        from codex_runtime import resolve_codex_binary
        candidate = resolve_codex_binary()
        binary = candidate if Path(candidate).is_file() else None
    if not binary:
        return False
    args = ['login', 'status'] if backend == 'codex' else ['auth', 'status']
    return run_cli([binary, *args])


def adapt_prompt(text, app_id, cli, profile):
    prefix = shlex.join([cli, '--profile', profile, '--as', 'bot'])
    # Only the known generated runtime line, never arbitrary business prose.
    pattern = re.compile(r'(?m)^(- 飞书操作使用本应用 ' + re.escape(app_id)
        + r'，命令前缀为 )(.+?)(。操作说明：)([^\n]+?/skills/codex-bridge/references/feishu-operations\.md)(。.*)$')
    operations = str(ROOT / 'skills/codex-bridge/references/feishu-operations.md')
    def replace(match):
        try:
            old = shlex.split(match[2])
        except ValueError:
            return match[0]
        if len(old) != 5 or Path(old[0]).name != 'lark-cli' or old[1] != '--profile' or old[3:] != ['--as', 'bot']:
            return match[0]
        return match[1] + prefix + match[3] + operations + match[5]
    adapted = pattern.sub(replace, text)
    # Unknown external absolute paths require Agent review, not blind replacement.
    unknown = []
    for path in re.findall(r'/(?:Users|home|tmp|opt|Applications|Volumes)/[^\s，。；;"\'`<>]+', adapted):
        if path not in (cli, operations):
            unknown.append(path)
    return adapted, bool(unknown)


def workspace_map(app, home):
    safe = re.sub(r'[^\w\-]+', '-', app['name'], flags=re.UNICODE).strip('-')[:40] or 'assistant'
    base = home / 'AgentBridge' / (safe + '-' + app['app_id'][-8:])
    result = {}
    for index, source in enumerate(dict.fromkeys([app['project_path'], *(g['project_path'] for g in app['groups'])])):
        # Source paths are migration hints, never authorization to reuse an
        # unrelated local directory (including another assistant's workspace).
        result[source] = base if index == 0 else base / ('group-workspace-' + str(index))
    return result


def backup(state, home):
    destination = safe_path(home / 'Library' / 'Application Support' / 'BridgeIntakeBackups' / uuid.uuid4().hex)
    if destination.resolve().is_relative_to(state.resolve()) or destination.resolve().is_relative_to(ROOT.resolve()):
        raise IntakeError('unsafe_backup_location')
    destination.mkdir(parents=True, mode=0o700)
    destination.chmod(0o700)
    for source in state.rglob('*'):
        if not source.is_file() or source.is_symlink() or source.suffix not in ('.json', '.sqlite3'):
            continue
        target = destination / source.relative_to(state)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.parent.chmod(0o700)
        if source.suffix == '.sqlite3':
            with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as origin:
                with closing(sqlite3.connect(target)) as copied:
                    origin.backup(copied)
        else:
            target.write_bytes(source.read_bytes())
        target.chmod(0o600)
    return destination


@contextmanager
def lock(state):
    path = safe_path(state / 'intake.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise IntakeError('intake_busy') from None
        yield
    finally:
        os.close(fd)


def intake(file, base='http://127.0.0.1:8797', inspect=False, source_stopped=False, target_ref=None,
           api=None, home=None):
    bundle, fingerprint = read_bundle(file)
    kind = validate(bundle)
    if kind == 'encrypted':
        return outcome('needs_password', user_action_required=True, next_action='obtain_legacy_password_privately')
    if kind == 'template':
        from assistant_intake_template import apply_template_file
        return apply_template_file(bundle, base, target_ref, inspect, api=api, home=home)
    bridge_url(base)
    api, home = api or API(base), Path(home or Path.home()).resolve()
    instance = api.call('/api/bridge-instance')
    if (not isinstance(instance, dict) or instance.get('product') != 'codex-bridge'
            or Path(instance.get('source_root', '')).resolve() != ROOT.resolve()
            or not isinstance(instance.get('state_dir'), str) or not Path(instance['state_dir']).is_dir()):
        raise IntakeError('wrong_bridge_instance')
    state = safe_path(instance['state_dir']).resolve()
    catalog = api.call('/api/feishu/catalog')
    project_list = api.call('/api/projects')
    if not isinstance(catalog, dict) or not isinstance(catalog.get('apps'), list) or not isinstance(project_list, list):
        raise IntakeError('invalid_local_catalog')
    existing = {a['app_id']: a for a in catalog['apps'] if isinstance(a, dict) and a.get('app_id')}
    def present(app):
        current = existing[app['app_id']]
        return {'name': app['name'], 'app_id': app['app_id'], 'stage': 'already_present',
                'enabled': bool(current.get('enabled')), 'modified': False}
    # Inspect and ordinary duplicate intake cause no backup, CLI invocation, lock,
    # receipt creation or POST. Only a matching owned receipt may resume work.
    receipt_path = safe_path(state / 'assistant-intake' / (fingerprint + '.json'))
    receipt = {}
    if receipt_path.exists():
        try:
            receipt = json.loads(receipt_path.read_text())
        except Exception:
            raise IntakeError('receipt_needs_review') from None
    entries = receipt.get('apps', {}) if isinstance(receipt, dict) else {}
    pending = [a for a in bundle['apps'] if a['app_id'] not in existing or
               (a['app_id'] in entries and entries[a['app_id']].get('ref') == existing[a['app_id']].get('ref')
                and not existing[a['app_id']].get('enabled') and
                (not entries[a['app_id']].get('verified') or source_stopped))]
    if inspect:
        return outcome('planned', [present(a) if a['app_id'] in existing else
            {'name': a['name'], 'app_id': a['app_id'], 'stage': 'would_prepare', 'enabled': False}
            for a in bundle['apps']], next_action='run_intake' if pending else 'none')
    if not pending:
        disabled = any(not existing[a['app_id']].get('enabled') for a in bundle['apps'])
        return outcome('already_present', [present(a) for a in bundle['apps']],
                       next_action='agent_manage_existing' if disabled else 'none')
    with lock(state):
        # Refresh under lock, never trust a stale deduplication snapshot.
        catalog = api.call('/api/feishu/catalog')
        existing = {a['app_id']: a for a in catalog['apps']}
        backup(state, home)
        cli = shutil.which('lark-cli')
        if not cli:
            raise IntakeError('agent_fix_missing_lark_cli')
        results, warnings = [], []
        receipt.setdefault('apps', {})
        for app in bundle['apps']:
            ident = app['app_id']
            saved = receipt['apps'].get(ident, {})
            current = existing.get(ident)
            owned = current and saved.get('ref') == current.get('ref') and bool(saved.get('ref'))
            if current and (not owned or current.get('enabled') or saved.get('verified') and not source_stopped):
                results.append(present(app))
                continue
            try:
                if owned and saved.get('revision') and current['revision'] != saved['revision']:
                    raise IntakeError('managed_settings_changed')
                profile, create_profile = profile_for(app, home)
                mapped = workspace_map(app, home)
                for folder in mapped.values():
                    safe_path(folder)
                    if folder.exists() and not owned:
                        raise IntakeError('workspace_already_exists_needs_review')
                local = copy.deepcopy(app)
                unknown = False
                for record in [local, *local['groups']]:
                    record['prompt_template'], review = adapt_prompt(record['prompt_template'], ident, cli, profile)
                    record['self_update_rule'], rule_review = adapt_prompt(record.get('self_update_rule', ''), ident, cli, profile)
                    unknown |= review or rule_review
                if is_shared(local):
                    # Group updates below adapt only routing/workspaces. Never
                    # replay an obsolete embedded group prompt as a root edit.
                    for group in local['groups']:
                        group['prompt_template'] = local['prompt_template']
                        group['self_update_rule'] = local.get('self_update_rule', '')
                backends = {local['backend'], *(g['backend'] for g in local['groups'])}
                ready = all(backend_ready(b) for b in backends)
                if create_profile and not run_cli([cli, 'config', 'init', '--name', profile,
                        '--app-id', ident, '--app-secret-stdin', '--brand', 'feishu', '--lang', 'zh'], app['app_secret']):
                    raise IntakeError('agent_fix_cli_profile')
                project_ids = {}
                for source, folder in mapped.items():
                    safe_path(folder)
                    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                    if not folder.is_dir() or not os.access(folder, os.R_OK | os.W_OK | os.X_OK):
                        raise IntakeError('agent_fix_workspace')
                    registered = api.call('/api/projects', {'path': str(folder)})
                    project_ids[source] = registered['id']
                if not current:
                    # Persist intent, not ownership. A crash before response means a
                    # later existing app is preserved rather than claimed by guessing.
                    receipt['apps'][ident] = {'phase': 'importing'}
                    private_json(receipt_path, receipt)
                    try:
                        imported = api.call('/api/feishu/migration/import', {'bundle': {
                            'format': FORMAT, 'version': 2, 'apps': [local]},
                            'project_id': project_ids[app['project_path']]})
                    except IntakeError:
                        after = api.call('/api/feishu/catalog')
                        if any(a.get('app_id') == ident for a in after.get('apps', [])):
                            # Request outcome is uncertain: preserve the new app,
                            # but do not claim ownership without its returned ref.
                            raise IntakeError('import_outcome_uncertain_preserved') from None
                        raise
                    ref = imported['created'][0]['ref']
                    imported_catalog = api.call('/api/feishu/catalog')
                    imported_app = next(a for a in imported_catalog['apps'] if a['ref'] == ref and a['app_id'] == ident)
                    saved = {'ref': ref, 'phase': 'adapting', 'verified': False, 'revision': imported_app['revision']}
                    receipt['apps'][ident] = saved
                    private_json(receipt_path, receipt)
                else:
                    ref = current['ref']
                catalog = api.call('/api/feishu/catalog')
                current = next(a for a in catalog['apps'] if a['app_id'] == ident and a['ref'] == ref)
                if saved.get('revision') and current['revision'] != saved['revision']:
                    raise IntakeError('managed_settings_changed')
                if current.get('enabled'):
                    results.append({'name': app['name'], 'app_id': ident, 'stage': 'already_present',
                                    'enabled': True, 'modified': False})
                    continue
                for group in local['groups']:
                    response = api.call('/api/feishu/groups', {'app_ref': ref, 'revision': current['revision'],
                        'chat_id': group['chat_id'], 'name': group['name'],
                        'project_id': project_ids[group['project_path']], 'prompt_template': group['prompt_template'],
                        'enabled': group['enabled'], 'topic_collaboration': group['topic_collaboration']})
                    current = next(a for a in response['apps'] if a['ref'] == ref)
                    saved['revision'] = current['revision']
                    private_json(receipt_path, receipt)
                catalog = api.call('/api/feishu/catalog')
                current = next(a for a in catalog['apps'] if a['ref'] == ref)
                connections = {c['id']: c for c in api.call('/api/connections')}
                actual_groups = [g for g in catalog.get('groups', []) if g.get('app_ref') == ref]
                verified = (not unknown and ready and current.get('bot_ready') is True
                    and current.get('execution_backend') == app['backend']
                    and is_shared(current) == is_shared(local)
                    and current.get('prompt_template') == local['prompt_template']
                    and current.get('self_update_rule', '') == local.get('self_update_rule', '')
                    and current.get('allowed_open_ids') == app['allowed_open_ids']
                    and current.get('project_path') == str(mapped[app['project_path']])
                    and {g.get('chat_id') for g in actual_groups} == {g['chat_id'] for g in app['groups']}
                    and all(any(g.get('chat_id') == source['chat_id']
                                and g.get('project_path') == str(mapped[source['project_path']])
                                and g.get('prompt_template') == source['prompt_template']
                                and g.get('self_update_rule', '') == source.get('self_update_rule', '')
                                and g.get('enabled') == source['enabled']
                                and g.get('topic_collaboration') == source['topic_collaboration']
                                and connections.get(g.get('connection_id'), {}).get('execution_backend') == source['backend']
                                for g in actual_groups) for source in local['groups']))
                saved.update(phase='prepared', verified=verified, revision=current['revision'])
                private_json(receipt_path, receipt)
                enabled = False
                if source_stopped and verified:
                    response = api.call('/api/feishu/apps/enabled',
                        {'app_ref': ref, 'revision': current['revision'], 'enabled': True})
                    enabled = next(a for a in response['apps'] if a['ref'] == ref).get('enabled') is True
                if unknown:
                    warnings.append('legacy_paths_need_agent_review')
                if not ready:
                    warnings.append('execution_backend_needs_local_login_or_install')
                results.append({'name': app['name'], 'app_id': ident,
                    'stage': 'enabled' if enabled else 'prepared' if verified else 'needs_agent_review',
                    'enabled': enabled, 'verified': verified})
            except (IntakeError, KeyError, OSError, ValueError) as exc:
                code = exc.code if isinstance(exc, IntakeError) else 'agent_fix_import_adaptation'
                warnings.append(code)
                results.append({'name': app['name'], 'app_id': ident, 'stage': 'needs_agent_review', 'enabled': False})
        needs_review = any(x['stage'] == 'needs_agent_review' for x in results)
        return outcome('needs_agent_review' if needs_review else 'ready' if all(x.get('enabled') for x in results)
                       else 'prepared', results, sorted(set(warnings)),
                       user_action_required=not needs_review and not source_stopped and any(x.get('verified') for x in results),
                       next_action='agent_fix_then_retry' if needs_review else 'confirm_source_stopped' if not source_stopped
                       and any(x.get('verified') for x in results) else 'verify_new_message_delivery' if source_stopped else 'none')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Prepare an assistant file locally; existing bots are preserved.')
    parser.add_argument('file')
    parser.add_argument('--bridge-url', default='http://127.0.0.1:8797')
    parser.add_argument('--inspect', action='store_true')
    parser.add_argument('--source-stopped', action='store_true', help='Confirm the old machine has stopped this bot.')
    parser.add_argument('--target-ref')
    args = parser.parse_args(argv)
    try:
        result = intake(args.file, args.bridge_url, args.inspect, args.source_stopped, args.target_ref)
    except IntakeError as exc:
        result = outcome('needs_agent_review', warnings=[exc.code], next_action='agent_fix_then_retry')
    except Exception:
        result = outcome('needs_agent_review', warnings=['agent_fix_intake'], next_action='agent_fix_then_retry')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['stage'] in ('already_present', 'planned', 'prepared', 'ready', 'applied', 'already_applied') else 1


if __name__ == '__main__':
    raise SystemExit(main())
