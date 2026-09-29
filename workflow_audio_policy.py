"""Versioned speech policy and persistent, input-specific timing conflicts."""
import copy
import json
from pathlib import Path
from codex_http_bridge import BridgeError
from workflow_contract import fingerprint

SENTENCE = 'sentence/v1'
CUE = 'cue/v1'

def audio_stages(definition):
    return [s for s in definition['steps']
            if any(p['kind'] == 'audio_alignment' for p in s.get('produces', {}).values())
            or s.get('settings', {}).get('音频时长策略') == 'continuous/v1']

def context(definition, evidence, choices):
    # Comments, doc revision counters, card IDs and regenerated report paths do
    # not change the speech problem. A new voice or explicit policy upgrade does.
    documents = {}
    sources = []
    for entry in evidence.values():
        if entry['kind'] == 'feishu_document':
            snapshot = json.loads(Path(entry['path']).read_text())
            doc = snapshot['document']
            if doc.get('document_id'):
                # Reuse the exact parser used to produce TTS text. Titles,
                # inline styling and XML entity spelling do not change speech.
                from workflow_alignment import module
                text = [row['text'] for row in module('subtitle').document_rows(snapshot)]
            else:
                # Legacy plain-text snapshot readers have no block document ID.
                text = doc.get('content', '')
            documents[entry['url']] = fingerprint(text)
        elif entry['kind'] == 'subtitle_manifest':
            sources.append(json.loads(Path(entry['path']).read_text())['source_sha256'])
    policies = {s['id']: {'grouping': s.get('settings', {}).get('配音分段', CUE),
                           'model': s.get('settings', {}).get('合成模型')}
                for s in audio_stages(definition)}
    for s in audio_stages(definition):
        if s.get('settings', {}).get('音频时长策略') == 'continuous/v1':
            policies[s['id']]['timing'] = 'continuous/v1'
    return {'documents': documents, 'sources': sorted(sources),
            'policies': policies, 'voice': choices.get('voice')}

def upgrade(definition):
    value = copy.deepcopy(definition)
    stages = audio_stages(value)
    if len(stages) != 1:
        raise BridgeError(409, 'unsupported_audio_upgrade', '仅支持恰有一个音频验收阶段的流程')
    stage = stages[0]
    if stage.get('settings', {}).get('配音分段', CUE) != CUE:
        raise BridgeError(409, 'audio_policy_already_set', '本任务已升级配音分组，无需重复修改')
    stage.setdefault('settings', {})['配音分段'] = SENTENCE
    stage['settings']['时间轴拼接'] = '48 kHz 单声道；按语句组原起点放置，组间补静音；字幕显示时间不变；不裁音、不变速'
    stage['prompt'] += ('\n配音分组升级：使用 feishu-subtitle-dubbing 与 dashscope-voice-clone-tts 的 audio_timeline.py，'
        'prepare/collect/assemble 均传 --grouping sentence/v1。保持每条批准字幕及映射，以连续语句组配音；'
        '先查看 preflight 风险并优先实测紧张语句。按 choices.voice 绑定素材复用音色。'
        '每组登记 effect_begin/finish 真实回执；成功复用，未知暂停。assemble 实测并拼接，'
        '组超时用 fail 记录 TIME_CONFLICT，改词需回审核；禁止裁音、变速或改批准稿。'
        '提交音频和 audio-alignment/v2 报告；Bridge 重算所有字幕覆盖、分组、时长及成功回执。')
    old_slot_rule='按字幕槽位合成并测量时长，保留间隙；不能截词或移动原字幕边界。'
    stage['acceptance'] = [x for x in stage.get('acceptance',[]) if x != old_slot_rule] + [
        '字幕内容、逐条段落映射和显示时间保留；每条字幕恰好属于一个连续语句组。',
        '组内配音可跨字幕切换点；按组原起点放置，组间不重叠，不裁音、不变速。',
        'preflight 是估算，不能代替真实合成测时；语句组超时记录 TIME_CONFLICT，需要改稿再审核。',
        '克隆与每组合成登记真实回执，成功结果复用，不确定时先核对。',
        'Bridge 重算映射、组文本、采样点及成品；逐词强制对齐、听感和 Final Cut 导入另行如实记录。']
    return value
