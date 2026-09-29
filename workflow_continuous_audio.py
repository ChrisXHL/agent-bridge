"""Explicit user-requested normal-speed audio; never claim original-time alignment."""
import copy
import json
import time
from codex_http_bridge import BridgeError
from workflow_audio_policy import audio_stages
from workflow_contract import fingerprint, validate_workflow
from workflow_evidence import check_files

POLICY = 'continuous/v1'

def resume(runtime, args, snapshotter):
    reason = args.get('reason')
    if not isinstance(reason, str) or not 1 <= len(reason) <= 1500:
        raise BridgeError(400, 'invalid_reason', '记录用户明确要求不用时长核验、直接合成的指令')
    with runtime.inbox.db() as db:
        run = runtime.guard(db, args, ('running', 'blocked'))
        original = json.loads(run['definition']); speech = audio_stages(original)
        if len(speech) != 1 or speech[0].get('settings', {}).get('音频时长策略') == POLICY:
            raise BridgeError(409, 'unsupported_audio_policy', '需要尚未切换的单一配音阶段')
        target = speech[0]
        allowed = {target['id']} | {s.get('revise') for s in original['steps'] if s['kind'] == 'review'}
        if run['step_id'] not in allowed:
            raise BridgeError(409, 'invalid_audio_stage', '仅当前配音或修订阶段可以直接合成')
        if db.execute("SELECT 1 FROM workflow_effects WHERE run_id=? AND state IN ('started','uncertain')", (run['id'],)).fetchone():
            raise BridgeError(409, 'effect_unconfirmed', '先核对未确认的外部操作，不重复合成')
        evidence = runtime.evidence(db, run); current = json.loads(run['artifacts']); approved = None
        for row in db.execute("SELECT data FROM workflow_events WHERE run_id=? AND event='decision_verified' ORDER BY id DESC", (run['id'],)):
            decision = json.loads(row[0])
            if decision.get('action') == 'approve': approved = decision; break
        if not approved: raise BridgeError(409, 'no_approved_document', '没有已确认文稿')
        documents = []
        for key in target.get('needs', []):
            entry = evidence.get(key)
            if entry and entry['kind'] == 'feishu_document':
                row = db.execute('SELECT evidence FROM workflow_artifacts WHERE id=? AND run_id=?', (approved['artifacts'].get(key), run['id'])).fetchone()
                if not row or json.loads(row[0])['content_hash'] != entry['content_hash']:
                    raise BridgeError(409, 'approved_version_changed', '文稿已修改，不能按旧确认直接合成')
                documents.append(entry)
        if not documents: raise BridgeError(409, 'no_approved_document', '配音缺少已确认文档')
        # Restore prior aligned inputs only when their frozen dependencies match.
        for key in target.get('needs', []):
            if key in current: continue
            row = db.execute('SELECT * FROM workflow_artifacts WHERE run_id=? AND name=? ORDER BY epoch DESC LIMIT 1', (run['id'], key)).fetchone()
            if not row: raise BridgeError(409, 'missing_inputs', '缺少已生成的配音输入：' + key)
            entry = json.loads(row['evidence'])
            if entry['kind'] not in ('fcpxml', 'subtitle_alignment'):
                raise BridgeError(409, 'missing_inputs', '只能恢复此前经过对齐的输入')
            dependencies = json.loads(row['inputs'])
            for name, expected in evidence.items():
                if name not in target['needs'] or name not in dependencies: continue
                prior = db.execute('SELECT evidence FROM workflow_artifacts WHERE id=? AND run_id=?', (dependencies[name], run['id'])).fetchone()
                if not prior or json.loads(prior[0])['sha256'] != expected['sha256']:
                    raise BridgeError(409, 'aligned_inputs_changed', '先前对齐结果对应其他原件或文稿')
            current[key] = row['id']; evidence[key] = entry
    check_files(evidence)
    for entry in documents:
        if fingerprint(snapshotter(entry['url'])) != entry['content_hash']:
            raise BridgeError(409, 'approved_version_changed', '当前文稿或评论已变化')
    updated = copy.deepcopy(original); step = next(s for s in updated['steps'] if s['id'] == target['id'])
    step.setdefault('settings', {})['音频时长策略'] = POLICY
    step['settings']['时间轴拼接'] = '正常语速按文本顺序连续拼接，保留原组间正间隔，不裁音或变速；不承诺原字幕时长贴合'
    for name, spec in step.get('produces', {}).items():
        if spec['kind'] == 'audio_alignment': step['produces'][name] = {'kind': 'file'}
    step['prompt'] = ('用户明确要求不用时长核验、直接进行。按已确认稿、所选音色和模型正常合成。'
        '复用真实成功回执，未完成组 effect_begin/finish 登记。按语句组顺序连续拼接，'
        '保留原组间正间隔，不裁音、不变速。超时只记录，不触发改稿或再次审核。'
        '提交音频与 audio-synthesis/v1 实测报告；报告沿用 audio_alignment_report 产物键，'
        '但必须写 alignment_status=not_enforced，不能声明原时间轴对齐通过。')
    step['acceptance'] = ['完整合成已确认稿，不改词，复用所选音色和真实回执。',
        '普通连续配音；超时不阻断，不改稿、不裁音、不变速。',
        '报告保存实际音频身份、顺序和时长，明确未按原字幕时长贴合。']
    for following in updated['steps']:
        if any(spec['kind'] == 'delivery' for spec in following.get('produces', {}).values()):
            following['prompt'] += '\n本任务用户已改为普通连续配音；发送已生成音频和原时间英文FCPXML，注明音频未按原字幕时间贴合。不恢复时长阻断、改稿或重复审核。'
    validate_workflow(updated, list(json.loads(run['forms']).values()), [run['sender_id']])
    with runtime.inbox.db() as db:
        db.execute('BEGIN IMMEDIATE'); latest = runtime.guard(db, args, ('running', 'blocked'))
        if db.execute("SELECT 1 FROM workflow_effects WHERE run_id=? AND state IN ('started','uncertain')", (run['id'],)).fetchone():
            raise BridgeError(409, 'effect_unconfirmed', '出现未确认外部操作，先核对')
        version = fingerprint(updated)
        db.execute('UPDATE workflow_runs SET definition=?,version=?,artifacts=?,updated_at=? WHERE id=?',
            (json.dumps(updated, ensure_ascii=False), version, json.dumps(current), time.time(), run['id']))
        runtime.event(db, latest, 'continuous_audio_requested', {'reason':reason,
            'from_version':run['version'], 'to_version':version, 'previous_definition':original,
            'policy':POLICY, 'source_and_document_unchanged':True, 'approval_not_synthesized':True})
        runtime.advance(db, runtime.row(db, run['id']), target['id'])
    return runtime.get(run['id'])
