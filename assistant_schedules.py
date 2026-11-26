"""Durable, owner-authorized Feishu assistant schedules."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import shlex
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from codex_http_bridge import BridgeError
from feishu_groups import group_routes
from feishu_settings import (DEFAULT_PROMPT_TEMPLATE, validate_prompt_template,
                             compose_prompt, bounded_task_prompt)
from assistant_prompt import business_template, delivery_context, is_shared
from assistant_self_update import applied_rule, owner_for, request_text
from assistant_source import resolve_source
from feishu_worker import Inbox


def explicit_request(text):
    if not isinstance(text, str) or len(text.strip()) < 2:
        return False
    head = text.strip()
    return bool(re.search(r'定时|预约|提醒|每天|每周|每隔|每\s*[0-9零〇一二两三四五六七八九十百]+\s*(?:分钟|小时)|取消.*任务|查看.*任务|'
                          r'(?:今天|今晚|明天|后天|下周|星期[一二三四五六日天]|周[一二三四五六日天]|\d{1,2}月\d{1,2}日)'
                          r'.*(?:点|时|\d{1,2}:\d{2})', head))


def capability(config, message_id, sender_id):
    secret = config.get('connection_key') or ''
    if not secret:
        return ''
    payload = f"assistant-schedule-v1:{config.get('app_id')}:{message_id}:{sender_id}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def instruction(config, config_path, message_id, sender_id, text, has_media=False,
                bridge_url='http://127.0.0.1:8797'):
    if (config.get('assistant_manager') or sender_id != owner_for(config, config_path, verified_only=is_shared(config))
            or has_media or not explicit_request(text)):
        return ''
    token = capability(config, message_id, sender_id)
    if not token:
        return ''
    script = Path(__file__).resolve().parent / 'scripts' / 'assistant-schedule.py'
    command = shlex.join(['python3', str(script), '--source-id', message_id, '--token', token,
                          '--bridge-url', bridge_url])
    return (f'\n\n[Bridge 可信定时入口；仅创建者] 用户要让本助手以后自动做事、查看或取消定时任务时，'
            f'使用 {command} 操作本助手的定时；不要求用户说“更新助手”。'
            '创建用 --action create --at YYYY-MM-DDTHH:MM --timezone 用户所在时区 '
            "--repeat once|daily|weekly|interval --task '到点执行的完整任务'；"
            '每隔 N 分钟或小时用 --repeat interval --every-minutes 分钟数，例如每 2 小时用 120。'
            '查看用 --action list；取消用 --action cancel --id 定时编号。'
            '一条消息里要设置多个定时（如多类数据各自错开时间）时，逐个调用 create 全部创建，不要让用户分多条消息发送。'
            '创建前把时间换算成未来的首次执行时间；用户没说明时区时，先用本机时区。'
            '--task 只写到点后要做的事，不要把「每天九点」等设置定时的指令放进去。'
            '用户要求发现后转交时，任务中保留下游助手的准确名称和分工；实际协作派发并收取结果，'
            '按来源与版本稳定去重，失败有限重试并保留待处理记录，不能把群里发出消息当成转交完成。'
            '不确定日期、时区或具体任务时，先向用户问清。'
            '只有脚本返回成功后才能说已设置；不要把令牌显示给用户。'
            '定时只由本助手执行，结果回到这条消息所在的私聊或群。')


def install(db):
    db.execute('''CREATE TABLE IF NOT EXISTS assistant_schedules (
        id TEXT PRIMARY KEY, app_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        chat_id TEXT NOT NULL, chat_type TEXT NOT NULL, task_text TEXT NOT NULL,
        timezone TEXT NOT NULL, repeat TEXT NOT NULL, local_at TEXT NOT NULL,
        next_run REAL NOT NULL, state TEXT NOT NULL, source_id TEXT NOT NULL,
        created_at REAL NOT NULL, last_fired REAL, last_error TEXT,
        UNIQUE(source_id, app_id))''')
    db.execute('CREATE INDEX IF NOT EXISTS assistant_schedules_due ON assistant_schedules(state,next_run)')
    if 'interval_minutes' not in {row['name'] for row in db.execute('PRAGMA table_info(assistant_schedules)')}:
        db.execute('ALTER TABLE assistant_schedules ADD COLUMN interval_minutes INTEGER NOT NULL DEFAULT 0')
    if 'last_message_id' not in {row['name'] for row in db.execute('PRAGMA table_info(assistant_schedules)')}:
        db.execute('ALTER TABLE assistant_schedules ADD COLUMN last_message_id TEXT')


def _next_run(local_at, zone_name, repeat, after, interval_minutes=0):
    zone = ZoneInfo(zone_name)
    start = datetime.fromisoformat(local_at)
    if start.tzinfo is not None or start.second or start.microsecond:
        raise BridgeError(400, 'invalid_time', '请填写带日期和分钟的本地时间')
    if repeat == 'once':
        stamp = start.replace(tzinfo=zone).timestamp()
        return stamp if stamp > after else None
    if repeat == 'interval':
        if not isinstance(interval_minutes, int) or isinstance(interval_minutes, bool) or interval_minutes < 1:
            raise BridgeError(400, 'invalid_repeat', '间隔至少 1 分钟')
        anchor = start.replace(tzinfo=zone).timestamp()
        seconds = interval_minutes * 60
        return anchor if anchor > after else anchor + (int((after - anchor) // seconds) + 1) * seconds
    if repeat not in ('daily', 'weekly'):
        raise BridgeError(400, 'invalid_repeat', '仅支持单次、每天、每周或固定间隔')
    step = 1 if repeat == 'daily' else 7
    candidate = start
    for _ in range(500):
        stamp = candidate.replace(tzinfo=zone).timestamp()
        if stamp > after:
            return stamp
        candidate += timedelta(days=step)
    raise BridgeError(400, 'invalid_time', '时间超出支持范围')


def _task_template(config, group, chat_type, chat_id, sender_id):
    group = group if chat_type == 'group' else {}
    fallback = group.get('prompt_template') or config.get('prompt_template', DEFAULT_PROMPT_TEMPLATE)
    base = validate_prompt_template(business_template(config, fallback))
    return (base + applied_rule(config, chat_id if chat_type == 'group' else None)
            + delivery_context(config, chat_type, chat_id, sender_id)
            + '\n\n[Bridge 定时触发] 现在执行以上工作并交付结果。定时已由 Bridge 管理，不要再次创建定时。')


def manage(groups, data):
    required = {'source_id', 'token', 'action', 'at', 'timezone', 'repeat', 'task', 'id'}
    if not isinstance(data, dict) or not required <= set(data) or set(data) - required - {'interval_minutes'}:
        raise BridgeError(400, 'invalid_request', '定时请求格式无效')
    source_id, token, action = data['source_id'], data['token'], data['action']
    if (not isinstance(source_id, str) or not re.fullmatch(r'om_[A-Za-z0-9_-]{3,128}', source_id)
            or not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{64}', token)
            or action not in ('create', 'list', 'cancel')):
        raise BridgeError(400, 'invalid_request', '定时请求无效')
    ref, path, inbox_path, item, config = resolve_source(
        groups, source_id, token, capability, 'schedule_forbidden', '这条消息不能管理助手定时任务')
    route = json.loads(item.get('route_json') or '{}')
    if (not config.get('enabled') or config.get('assistant_archived') or config.get('account_archived')
            or config.get('assistant_manager') or item.get('app_id') != config.get('app_id')
            or item.get('sender_id') != owner_for(config, path, verified_only=is_shared(config))
            or route.get('sender_type', 'user') != 'user'
            or route.get('mode') != 'prompt' or not explicit_request(request_text(item, route))):
        raise BridgeError(403, 'schedule_forbidden', '只有创建者能设置这位助手的定时任务')
    if item.get('chat_type') == 'group':
        routes = {r['chat_id']: r for r in group_routes(config)}
        if item['chat_id'] not in routes or not routes[item['chat_id']].get('enabled', True):
            raise BridgeError(409, 'group_unavailable', '这个群当前没有接入助手')
    elif item.get('chat_type') != 'p2p' or item['sender_id'] not in config.get('allowed_open_ids', []):
        raise BridgeError(403, 'schedule_forbidden', '当前会话不能设置定时')
    inbox = Inbox(inbox_path)
    with inbox.db() as db:
        # Serialize idempotency lookup with insert/cancel, including simultaneous
        # retries from multiple HTTP threads for the same source message.
        db.execute('BEGIN IMMEDIATE')
        install(db)
        # Group management cannot expose or affect private/other-group schedules.
        # The verified owner's authorized private chat retains global management.
        scene_sql = ' AND chat_type=? AND chat_id=?' if item['chat_type'] == 'group' else ''
        scene_args = ('group', item['chat_id']) if item['chat_type'] == 'group' else ()
        if action == 'list':
            rows = db.execute('''SELECT id,task_text,timezone,repeat,interval_minutes,local_at,next_run,state,chat_type,chat_id,last_error
                FROM assistant_schedules WHERE app_id=? AND owner_id=? AND state IN ('active','error')'''
                + scene_sql + ' ORDER BY next_run',
                (config['app_id'], item['sender_id']) + scene_args).fetchall()
            return {'schedules': [dict(row) for row in rows]}
        if action == 'cancel':
            ident = data['id']
            if not isinstance(ident, str) or not re.fullmatch(r'sch_[a-f0-9]{20}', ident):
                raise BridgeError(400, 'invalid_schedule', '定时编号无效')
            changed = db.execute('''UPDATE assistant_schedules SET state='cancelled' WHERE id=? AND app_id=?
                AND owner_id=? AND state IN ('active','error')''' + scene_sql,
                (ident, config['app_id'], item['sender_id']) + scene_args).rowcount
            if not changed:
                raise BridgeError(404, 'schedule_not_found', '没有找到这条正在运行的定时任务')
            return {'cancelled': True, 'id': ident}
        task, at, zone, repeat = (data[k] for k in ('task', 'at', 'timezone', 'repeat'))
        interval_minutes = data.get('interval_minutes', 0)
        if (not isinstance(task, str) or len(task.strip()) < 3
                or not isinstance(at, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d', at)
                or not isinstance(zone, str) or len(zone) > 80 or repeat not in ('once', 'daily', 'weekly', 'interval')
                or not isinstance(interval_minutes, int) or isinstance(interval_minutes, bool)
                or (repeat == 'interval' and interval_minutes < 1)
                or (repeat != 'interval' and interval_minutes != 0)):
            raise BridgeError(400, 'invalid_schedule', '请提供具体任务、首次执行时间和重复方式')
        # One message may create several schedules (e.g. three data sources with
        # staggered times). Idempotency is per (message, schedule spec): a retry
        # of the same create returns the existing row instead of duplicating it.
        spec = (task.strip(), at, zone, repeat, interval_minutes)
        siblings = db.execute('SELECT * FROM assistant_schedules WHERE app_id=? AND (source_id=? OR substr(source_id,1,?)=?)',
                              (config['app_id'], source_id, len(source_id) + 1, source_id + '#')).fetchall()
        existing = next((r for r in siblings if (r['task_text'], r['local_at'], r['timezone'], r['repeat'],
                                                 r['interval_minutes']) == spec), None)
        if existing:
            if existing['state'] != 'active':
                raise BridgeError(409, 'schedule_inactive',
                                  '这条消息创建的同一定时已取消、结束或出错；请发送新消息确认重新设置',
                                  extra={'id': existing['id'], 'state': existing['state']})
            return {'created': True, 'id': existing['id'], 'next_run': existing['next_run'],
                    'local_at': existing['local_at'], 'timezone': existing['timezone'],
                    'repeat': existing['repeat'], 'interval_minutes': existing['interval_minutes'],
                    'state': existing['state'], 'already_created': True}
        stored_source = source_id
        if any(r['source_id'] == source_id for r in siblings):
            stored_source = source_id + '#' + hashlib.sha256(json.dumps(spec, ensure_ascii=False).encode()).hexdigest()[:16]
        try:
            next_run = _next_run(at, zone, repeat, time.time(), interval_minutes)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise BridgeError(400, 'invalid_time', '时间或时区无效') from exc
        if next_run is None:
            raise BridgeError(400, 'invalid_time', '首次执行时间必须在未来')
        group = (config.get('group_configs') or {}).get(item['chat_id'], {}) if item['chat_type'] == 'group' else {}
        template = _task_template(config, group, item['chat_type'], item['chat_id'], item['sender_id'])
        # Validate the business base, then compose trusted context without applying
        # the base's 8000-character limit to generated runtime instructions.
        bounded_task_prompt(compose_prompt(template, task, {'source_message_id': 'sch_' + 'x' * 32,
                            'chat_id': item['chat_id'], 'app_id': config['app_id']}), inbox_path, source_id)
        ident = 'sch_' + uuid.uuid4().hex[:20]
        db.execute('''INSERT INTO assistant_schedules
            (id,app_id,owner_id,chat_id,chat_type,task_text,timezone,repeat,local_at,next_run,state,source_id,created_at,interval_minutes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (ident, config['app_id'], item['sender_id'], item['chat_id'], item['chat_type'], task.strip(),
             zone, repeat, at, next_run, 'active', stored_source, time.time(), interval_minutes))
        return {'created': True, 'id': ident, 'next_run': next_run, 'local_at': at, 'timezone': zone,
                'repeat': repeat, 'interval_minutes': interval_minutes, 'chat_type': item['chat_type'],
                'state': 'active'}


def fire_due(inbox, config):
    """Atomically turn a due occurrence into an ordinary durable inbox item."""
    current = time.time()
    with inbox.db() as db:
        install(db)
        rows = db.execute("SELECT * FROM assistant_schedules WHERE state='active' AND next_run<=? ORDER BY next_run",
                          (current,)).fetchall()
        fired = 0
        for row in rows:
            if fired >= 5:
                break
            row = dict(row)
            if row['app_id'] != config.get('app_id'):
                continue
            group = ((config.get('group_configs') or {}).get(row['chat_id'], {})
                     if row['chat_type'] == 'group' else {})
            if (not config.get('enabled') or config.get('assistant_archived') or config.get('account_archived')
                    or row['owner_id'] != owner_for(config, verified_only=is_shared(config))
                    or (row['chat_type'] == 'p2p' and row['owner_id'] not in config.get('allowed_open_ids', []))
                    or (row['chat_type'] == 'group' and (row['chat_id'] not in config.get('allowed_chat_ids', [])
                                                        or group.get('enabled') is False))):
                # Keep the schedule for when this assistant or group is re-enabled.
                continue
            if row['last_message_id']:
                previous = db.execute('SELECT notified_status FROM inbound WHERE message_id=?',
                                      (row['last_message_id'],)).fetchone()
                if previous and previous['notified_status'] not in ('已完成', '失败', '已停止'):
                    # A one-minute schedule must not pile up while the previous
                    # task is still queued, running, or waiting for delivery.
                    continue
            route = {'mode': 'prompt', 'scheduled': True, 'new_conversation': True,
                     'schedule_id': row['id'], 'previous_message_id': row['last_message_id'],
                     'occurrence': int(row['next_run']),
                     'connection_id': group.get('connection_id') or config.get('connection_id'),
                     'prompt_template': group.get('prompt_template') or config.get('prompt_template', DEFAULT_PROMPT_TEMPLATE)}
            occurrence = int(row['next_run'])
            message_id = 'sch_' + hashlib.sha256(f"{row['id']}:{occurrence}".encode()).hexdigest()[:32]
            try:
                route['prompt_template'] = business_template(config, route['prompt_template'])
                if is_shared(config):
                    route['prompt_policy_version'] = 1
                template = _task_template(config, group, row['chat_type'], row['chat_id'], row['owner_id'])
                saved_rules = applied_rule(config, row['chat_id'] if row['chat_type'] == 'group' else None)
                route['saved_rules_digest'] = hashlib.sha256(saved_rules.encode()).hexdigest()
            except BridgeError as exc:
                db.execute("UPDATE assistant_schedules SET state='error',last_error=? WHERE id=?",
                           (str(exc)[:300], row['id']))
                continue
            if row['repeat'] == 'interval':
                window_start = row['last_fired'] or row['next_run'] - row['interval_minutes'] * 60
                # Trigger time is not a success watermark. Carry an unfinished
                # occurrence's search window forward instead of silently skipping it.
                if row['last_message_id']:
                    prior = db.execute('SELECT * FROM inbound WHERE message_id=?',
                                       (row['last_message_id'],)).fetchone()
                    if prior and previous and previous['notified_status'] != '已完成':
                        try:
                            prior_route = json.loads(prior['route_json'] or '{}')
                            prior_start = prior_route.get('window_start')
                            if (prior_route.get('schedule_id') == row['id']
                                    and prior_route.get('mode') == 'prompt' and prior_route.get('scheduled') is True
                                    and all(prior[k] == row[k] for k in ('app_id', 'chat_id', 'chat_type'))
                                    and prior['sender_id'] == row['owner_id']
                                    and prior['connection_id'] == route['connection_id']
                                    and isinstance(prior_start, (int, float)) and not isinstance(prior_start, bool)
                                    and math.isfinite(prior_start) and 0 <= prior_start <= window_start):
                                window_start = prior_start
                        except (ValueError, TypeError, AttributeError):
                            pass
                route['window_start'] = window_start
                zone = ZoneInfo(row['timezone'])
                start_text = datetime.fromtimestamp(window_start, zone).strftime('%Y-%m-%d %H:%M')
                end_text = datetime.fromtimestamp(current, zone).strftime('%Y-%m-%d %H:%M')
                template += (f'\n[本轮间隔] {start_text} 至 {end_text}（{row["timezone"]}）。'
                             '如任务要求查找新增资料，以这段时间为准；不要把此前已交付的内容当作本轮新发现。')
            try:
                prompt = compose_prompt(template, row['task_text'], {'source_message_id': message_id,
                    'chat_id': row['chat_id'], 'app_id': row['app_id']})
                prompt = bounded_task_prompt(prompt, inbox.path, message_id)
            except BridgeError as exc:
                db.execute("UPDATE assistant_schedules SET state='error',last_error=? WHERE id=?",
                           (str(exc)[:300], row['id']))
                continue
            db.execute('''INSERT OR IGNORE INTO inbound
                (message_id,chat_id,sender_id,text,prompt,created_at,updated_at,connection_id,app_id,route_json,chat_type)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (message_id, row['chat_id'], row['owner_id'], row['task_text'], prompt,
                    current, current, route['connection_id'], row['app_id'], json.dumps(route), row['chat_type']))
            next_run = (_next_run(row['local_at'], row['timezone'], row['repeat'], current, row['interval_minutes'])
                        if row['repeat'] != 'once' else None)
            db.execute('''UPDATE assistant_schedules
                SET state=?,next_run=?,last_fired=?,last_error=NULL,last_message_id=? WHERE id=?''',
                ('active' if next_run else 'finished', next_run or row['next_run'], current, message_id, row['id']))
            fired += 1
