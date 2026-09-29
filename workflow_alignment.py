"""Fixed local validators for alignment evidence. Never execute a task-supplied script."""
from functools import lru_cache
import importlib.util
import json
from pathlib import Path
import shutil
from codex_http_bridge import BridgeError

@lru_cache(maxsize=2)
def module(name):
    files = {'subtitle': ('fcpxml-subtitle-aligner', 'fcpxml_align.py'),
             'audio': ('dashscope-voice-clone-tts', 'audio_timeline.py')}
    skill, script = files[name]
    path = Path.home()/'.codex/skills'/skill/'scripts'/script
    if not path.is_file():
        raise BridgeError(503, 'alignment_tool_missing', '缺少已安装的校验工具：'+skill)
    spec = importlib.util.spec_from_file_location('bridge_'+name+'_alignment', path)
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result

def capture_alignment(spec, value, folder, inputs):
    try:
        if not isinstance(value,dict) or set(value)!={'path'}:
            raise ValueError('对齐产物需要绝对 path')
        source = Path(value['path'])
        if not source.is_absolute() or not source.is_file():raise ValueError('对齐产物文件不存在')
        claimed = json.loads(source.read_text())
        def path(field):return inputs[spec[field]]['path']
        def data(field):return json.loads(Path(path(field)).read_text())
        kind = spec['kind']
        if kind == 'subtitle_manifest':
            m = module('subtitle')
            m.approved_units(path('reference'), claimed, data('document'))
            if claimed.get('bound_revision_id') != data('document')['document']['revision_id']:
                raise ValueError('初始字幕清单必须绑定本轮文档版本')
            canonical = claimed
        elif kind == 'subtitle_alignment':
            canonical = module('subtitle').verify(path('reference'), data('manifest'), data('document'), path('output'))
            if canonical != claimed:raise ValueError('字幕报告与独立逐条核验结果不同')
        else:
            if inputs[spec['alignment']].get('kind') != 'subtitle_alignment':
                raise ValueError('音频验收必须使用已验证的字幕对齐产物')
            m = module('audio')
            # Own the segments before recomputing. Historical evidence must not depend
            # on mutable files in an agent workspace.
            segment_dir = folder/'segments'; segment_dir.mkdir()
            for i, s in enumerate(claimed.get('segments', [])):
                if m.sha(s['path']) != s.get('sha256'):raise ValueError('分段音频哈希变化')
                src = Path(s['path'])
                if not src.is_absolute():raise ValueError('分段音频须为绝对路径')
                dst = segment_dir/(str(i)+src.suffix)
                shutil.copyfile(src, dst);dst.chmod(0o600)
                s['path'] = str(dst.resolve())
            canonical = m.verify(data('alignment'), claimed, path('output'))
        target = folder/'verified-alignment.json'
        target.write_text(json.dumps(canonical, ensure_ascii=False, indent=2));target.chmod(0o600)
        return {'kind':kind, 'path':str(target), 'original_path':str(source),
                'sha256':module('subtitle').sha(target),'bytes':target.stat().st_size,
                'validation':'recomputed', 'units':len(canonical.get('units',canonical.get('segments',[]))),
                'dependent_files':[{'path':s['path'],'sha256':s['sha256']} for s in canonical.get('segments',[])]}
    except BridgeError:raise
    except Exception as exc:
        raise BridgeError(409,'alignment_failed','对齐验收未通过：'+str(exc)) from exc

def verify_document_bindings(captured, evidence):
    """Catch destructive edits before sending another human-review card."""
    try:
        for entry in evidence.values():
            if entry['kind'] != 'subtitle_manifest':continue
            manifest=json.loads(Path(entry['path']).read_text())
            for name, doc in captured.items():
                if doc['kind']!='feishu_document' or name not in evidence:continue
                before=json.loads(Path(evidence[name]['path']).read_text())
                if before['document']['document_id']!=manifest['document_id']:continue
                original=next((e for e in evidence.values() if e['sha256']==manifest['source_sha256']),None)
                if not original:raise ValueError('绑定清单对应的原件丢失')
                module('subtitle').approved_units(original['path'],manifest,json.loads(Path(doc['path']).read_text()))
    except Exception as exc:
        raise BridgeError(409,'document_mapping_changed','修订后字幕映射变化：'+str(exc)) from exc

def verify_audio_effects(captured, evidence, stage, effects):
    """Receipts must be present in this run's successful effect ledger, not just a report."""
    for name, entry in captured.items():
        if entry['kind'] != 'audio_alignment':continue
        report=json.loads(Path(entry['path']).read_text())
        spec=stage['produces'][name]
        alignment=json.loads(Path({**evidence,**captured}[spec['alignment']]['path']).read_text())
        grouping=stage.get('settings',{}).get('配音分段','cue/v1')
        if report.get('grouping','cue/v1')!=grouping:
            raise BridgeError(409,'audio_policy_mismatch','配音分组与本任务冻结契约不同，需显式升级')
        units={u['id']:u for u in module('audio').plan(alignment,grouping)['units']}
        for s in report['segments']:
            expected_model=stage.get('settings',{}).get('合成模型')
            if expected_model and s['model']!=expected_model:
                raise BridgeError(409,'audio_model_mismatch','合成模型与本阶段已配置的模型不同')
            matched=False
            for effect in effects:
                params=json.loads(effect['parameters']);result=json.loads(effect['result'] or '{}')
                if (params.get('segment_id')==s['id'] and params.get('text')==units[s['id']]['text']
                    and params.get('voice_id')==s['voice_id'] and params.get('model')==s['model']
                    and result.get('request_id')==s['request_id'] and result.get('audio_sha256')==s['sha256']):
                    matched=True;break
            if not matched:
                raise BridgeError(409,'audio_receipt_missing',s['id']+' 缺少本任务成功的合成回执、精确文本及音频哈希')
