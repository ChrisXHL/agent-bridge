"""Real local XML/PCM/ffmpeg and isolated SQLite; no cloud speech or Feishu sends."""
import copy
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import struct
import tempfile
import unittest
import wave

from codex_http_bridge import BridgeError
from workflow_alignment import module, verify_audio_effects, verify_document_bindings
from workflow_contract import validate_workflow
from workflow_evidence import capture, check_files
import test_workflow_runtime as fixtures

X=module('subtitle'); A=module('audio')
XML='''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.6"><resources><effect id="r1" name="title"/></resources><library><event><project><sequence duration="4s"><spine><gap offset="0s" start="3600s" duration="4s">
<!-- retained -->
<title ref="r1" offset="36001/10s" start="3600s" duration="9/10s"><text><text-style ref="ts1">甲</text-style></text><text-style-def id="ts1"><text-style font="A"/></text-style-def></title>
<title ref="r1" offset="3602s" start="3600s" duration="1s"><text><text-style ref="ts2">乙</text-style></text><text-style-def id="ts2"><text-style font="A"/></text-style-def></title>
</gap></spine></sequence></project></event></library></fcpxml>'''
DOC={'url':'https://example.feishu.cn/docx/Fixture','document':{'document_id':'Fixture','revision_id':1,
     'content':'<title id="Fixture">Review</title><p id="b1">Hello &amp; goodbye.</p><p id="b2">Next sentence.</p>'},'unresolved_comments':[]}

class AlignmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.source=self.root/'source.fcpxml';self.source.write_text(XML)
        self.doc=copy.deepcopy(DOC);self.manifest=X.bind(self.source,X.extract(self.source),self.doc)
        self.en=self.root/'en.fcpxml';self.report=X.apply(self.source,self.manifest,self.doc,self.en)
    def tearDown(self):self.tmp.cleanup()
    def save(self,name,value):
        p=self.root/name;p.write_text(json.dumps(value,ensure_ascii=False));return str(p)
    def segments(self, lengths=(24000,12000), grouping='cue/v1'):
        p=A.plan(self.report,grouping);segments=[]
        for i,(u,n) in enumerate(zip(p['units'],lengths)):
            path=self.root/('segment%d.wav'%i)
            with wave.open(str(path),'wb') as w:
                w.setparams((1,2,A.RATE,0,'NONE','not compressed'))
                w.writeframes(b''.join(struct.pack('<h',int(7000*math.sin(j*2*math.pi*(300+i*100)/A.RATE))) for j in range(n)))
            segments.append({'id':u['id'],'path':str(path),'text_sha256':u['text_sha256'],
                             'request_id':'fixture_'+str(i),'voice_id':'fixture_voice','model':'fixture_model'})
        return segments
    def test_original_immutable_precise_timing_and_escaping(self):
        self.assertEqual(self.source.read_text(),XML)
        self.assertEqual(self.report['units'][0]['start'],'1/10s')
        self.assertEqual(self.report['units'][1]['start'],'2s')
        self.assertIn('Hello &amp; goodbye.',self.en.read_text())
        self.assertIn('<!-- retained -->',self.en.read_text())
        self.assertEqual(X.verify(self.source,self.manifest,self.doc,self.en),self.report)
    def test_changed_manifest_or_source_rejected(self):
        bad=copy.deepcopy(self.manifest);bad['units'][0]['timeline_start']='0s'
        with self.assertRaises(X.AlignmentError):X.approved_units(self.source,bad,self.doc)
        self.source.write_text(XML.replace('duration="4s"','duration="5s"'))
        with self.assertRaises(X.AlignmentError):X.validate_manifest(self.source,self.manifest)
    def test_deleted_reordered_split_document_rejected(self):
        for content in ['<p id="b1">Hello.</p>', '<p id="b2">Next.</p><p id="b1">Hello.</p>',
                        '<p id="b1">Hello<br/>again</p><p id="b2">Next.</p>',
                        '<p id="new1">Hello.</p><p id="new2">Next.</p>']:
            with self.subTest(content=content):
                d=copy.deepcopy(self.doc);d['document']['content']=content
                with self.assertRaises(X.AlignmentError):X.approved_units(self.source,self.manifest,d)
    def test_revision_request_preserves_block_identity_and_revision(self):
        request=X.revision_request(self.source,self.manifest,self.doc,'u000001','Updated & checked.')
        self.assertEqual(request['path'],'/open-apis/docx/v1/documents/Fixture/blocks/b1')
        self.assertEqual(request['params'],{'document_revision_id':1})
        self.assertEqual(request['body']['update_text_elements']['elements'][0]['text_run']['content'],'Updated & checked.')
    def test_wrong_text_timing_tail_or_resources_rejected(self):
        original=self.en.read_text()
        for old,new in [('Hello','Other'),('duration="9/10s"','duration="1s"'),
                        ('font="A"','font="B"'),('</text-style></text>','</text-style>BAD</text>')]:
            with self.subTest(old=old):
                self.en.write_text(original.replace(old,new))
                with self.assertRaises(X.AlignmentError):X.verify(self.source,self.manifest,self.doc,self.en)
    def test_unsafe_xml_and_unsupported_retime_fail(self):
        with self.assertRaises(X.AlignmentError):X.safe_xml('<!DOCTYPE fcpxml [<!ENTITY e "x">]><fcpxml>&e;</fcpxml>')
        self.source.write_text(XML.replace('<text>','<timeMap/><text>',1))
        with self.assertRaises(X.AlignmentError):X.extract(self.source)
    def test_multistyle_requires_explicit_exact_fragments(self):
        self.source.write_text(XML.replace('甲</text-style>','甲</text-style><text-style ref="ts1">一</text-style>',1))
        m=X.bind(self.source,X.extract(self.source),self.doc)
        with self.assertRaises(X.AlignmentError):X.apply(self.source,m,self.doc,self.root/'no.fcpxml')
        r=X.apply(self.source,m,self.doc,self.root/'styled.fcpxml',{'u000001':['Hello & ','goodbye.']})
        self.assertEqual(r['status'],'passed')
    def test_fractional_frame_nested_offset(self):
        self.source.write_text(XML.replace('36001/10s','108001001/30000s').replace('duration="9/10s"','duration="1001/30000s"'))
        m=X.extract(self.source)
        self.assertEqual(Fraction(m['units'][0]['timeline_start'][:-1]),Fraction(1001,30000))
    def test_audio_gaps_and_original_slots(self):
        segments=self.segments();audio=self.root/'out.wav';r=A.assemble(self.report,segments,audio)
        with wave.open(str(audio),'rb') as w:pcm=w.readframes(w.getnframes())
        self.assertEqual(len(pcm)//2,4*A.RATE)
        self.assertEqual(pcm[:9600],b'\0'*9600)
        self.assertEqual(pcm[60000:192000],b'\0'*(192000-60000))
        self.assertEqual(A.verify(self.report,r,audio),r)
    def test_mp3_reconstruction_is_deterministic(self):
        audio=self.root/'out.mp3';r=A.assemble(self.report,self.segments(),audio)
        self.assertEqual(A.verify(self.report,r,audio),r)
    def test_prepare_collect_preserves_exact_text(self):
        folder=self.root/'prepared';A.prepare(self.report,folder)
        segments=self.segments()
        for s in segments:
            A.write_json(folder/(s['id']+'.receipt.json'),{**s,'output':s['path'],'audio_sha256':A.sha(s['path'])})
        result=A.collect(self.report,folder)
        self.assertEqual([s['id'] for s in result],['u000001','u000002'])
        self.assertEqual((folder/'u000001.txt').read_text(),self.report['units'][0]['text'])
        Path(segments[0]['path']).write_bytes(b'changed')
        with self.assertRaises(A.AudioError):A.collect(self.report,folder)
    def test_overlong_and_missing_segments_fail_without_output(self):
        segments=self.segments((48001,12000));audio=self.root/'out.wav'
        with self.assertRaisesRegex(A.AudioError,'TIME_CONFLICT.*u000001'):A.assemble(self.report,segments,audio)
        self.assertFalse(audio.exists())
        with self.assertRaises(A.AudioError):A.assemble(self.report,segments[:1],audio)
    def test_overlapping_slots_fail(self):
        r=copy.deepcopy(self.report);r['units'][1]['start']=r['units'][0]['start']
        with self.assertRaises(A.AudioError):A.plan(r)
    def test_visual_overlap_preserved_but_speech_overlap_rejected(self):
        r=copy.deepcopy(self.report);r['units'][1]['start']='9/10s'
        p=A.plan(r)
        self.assertEqual(p['units'][0]['end_frame'],48000)
        self.assertEqual(p['units'][0]['audio_deadline_frame'],43200)
        audio=self.root/'overlap.wav';segments=self.segments()
        report=A.assemble(r,segments,audio)
        self.assertEqual(report['status'],'passed')
        # Still within its own visual subtitle duration, but collides with next speech.
        with wave.open(segments[0]['path'],'wb') as w:
            w.setparams((1,2,A.RATE,0,'NONE','not compressed'));w.writeframes(b'\0'*(41000*2))
        with self.assertRaisesRegex(A.AudioError,'TIME_CONFLICT'):A.assemble(r,segments,self.root/'no.wav')
    def test_forged_pass_and_modified_audio_fail(self):
        audio=self.root/'out.wav';r=A.assemble(self.report,self.segments(),audio)
        bad=copy.deepcopy(r);bad['segments'][0]['decoded_frames']=1
        with self.assertRaises(A.AudioError):A.verify(self.report,bad,audio)
        with audio.open('ab') as f:f.write(b'changed')
        with self.assertRaises(A.AudioError):A.verify(self.report,r,audio)
    def test_bridge_recomputes_and_owns_segment_evidence(self):
        ev={'original':{'path':str(self.source)},'doc':{'path':self.save('doc.json',self.doc)}}
        spec={'kind':'subtitle_manifest','reference':'original','document':'doc'}
        ev['manifest']=capture(spec,{'path':self.save('manifest.json',self.manifest)},self.root/'m',ev)
        ev['xml']={'path':str(self.en)}
        spec={'kind':'subtitle_alignment','reference':'original','document':'doc','manifest':'manifest','output':'xml'}
        ev['alignment']=capture(spec,{'path':self.save('report.json',self.report)},self.root/'a',ev)
        bad=copy.deepcopy(self.report);bad['units'][0]['text']='fake'
        with self.assertRaises(BridgeError):capture(spec,{'path':self.save('bad.json',bad)},self.root/'bad',ev)
        segments=self.segments();audio=self.root/'out.mp3';r=A.assemble(self.report,segments,audio)
        ev['audio']={'path':str(audio)}
        spec={'kind':'audio_alignment','alignment':'alignment','output':'audio'}
        captured=capture(spec,{'path':self.save('audio.json',r)},self.root/'owned',ev)
        for s in segments:Path(s['path']).unlink()
        check_files({'report':captured})
        Path(captured['dependent_files'][0]['path']).write_bytes(b'bad')
        with self.assertRaises(BridgeError):check_files({'report':captured})
    def test_receipts_require_exact_text_request_and_audio_hash(self):
        segments=self.segments();audio=self.root/'out.wav';report=A.assemble(self.report,segments,audio)
        captured={'r':{'kind':'audio_alignment','path':self.save('audio.json',report)}}
        ev={'align':{'path':self.save('align.json',self.report)}}
        stage={'produces':{'r':{'alignment':'align'}}};effects=[]
        for s,u in zip(report['segments'],self.report['units']):
            effects.append({'parameters':json.dumps({'segment_id':s['id'],'text':u['text'],'voice_id':s['voice_id'],'model':s['model']}),
                            'result':json.dumps({'request_id':s['request_id'],'audio_sha256':s['sha256']})})
        verify_audio_effects(captured,ev,stage,effects)
        with self.assertRaises(BridgeError):verify_audio_effects(captured,ev,stage,effects[:1])
    def test_revision_mapping_checked_before_review(self):
        m={'kind':'subtitle_manifest','path':self.save('manifest.json',self.manifest)}
        original={'kind':'file','path':str(self.source),'sha256':X.sha(self.source)}
        doc={'kind':'feishu_document','path':self.save('before.json',self.doc),'sha256':'unused'}
        m['sha256']='unused'
        changed=copy.deepcopy(self.doc);changed['document']['content']='<p id="new">Different</p>'
        with self.assertRaises(BridgeError):verify_document_bindings({'doc':{'kind':'feishu_document','path':self.save('after.json',changed)}},{'m':m,'doc':doc,'original':original})
    def test_contract_dependency_cycles_rejected(self):
        w={'name':'test','steps':[{'id':'start','name':'test','kind':'agent','prompt':'x','needs':[],
             'produces':{'m':{'kind':'subtitle_manifest','reference':'m','document':'doc'},'doc':{'kind':'feishu_document'}}}]}
        with self.assertRaises(BridgeError):validate_workflow(w)

class AlignmentLedgerTests(unittest.TestCase):
    setUp=fixtures.WorkflowTests.setUp
    tearDown=fixtures.WorkflowTests.tearDown
    get=fixtures.WorkflowTests.get
    act=fixtures.WorkflowTests.act
    snapshot=fixtures.WorkflowTests.snapshot
    click=fixtures.WorkflowTests.click
    verified=fixtures.WorkflowTests.verified
    def test_effect_parameters_survive_restart(self):
        r=self.rt.get(self.rid)
        params={'segment_id':'u000001','text':'Hello','voice_id':'fixture','model':'fixture'}
        effect=self.rt.act({'run_id':self.rid,'revision':r['revision'],'action':'effect_begin','key':'tts_u1','parameters':params})
        with self.inbox.db() as db:
            row=db.execute('SELECT * FROM workflow_effect_details WHERE effect_id=?',(effect['effect_id'],)).fetchone()
            self.assertEqual(json.loads(row['parameters']),params)
    def test_full_workflow_requires_receipts_before_delivery(self):
        self.source['message_id']='om_alignment'
        self.inbox.accept(self.source)
        w=json.loads(Path('examples/subtitle-workflow.json').read_text())
        # Isolated fixture model; production remains cosyvoice-v3.5-plus.
        next(s for s in w['steps'] if s['id']=='tts')['settings']['合成模型']='fixture_model'
        self.rid=self.rt.start(self.source,w,self.config)
        self.inbox.mark_submitted('om_alignment','task_alignment')
        self.doc=copy.deepcopy(DOC);self.original.write_text(XML)
        manifest=X.bind(self.original,X.extract(self.original),self.doc)
        m=self.root/'manifest.json';m.write_text(json.dumps(manifest))
        supervision=self.root/'supervision.json';supervision.write_text('{"fixture":true}')
        self.act('complete',outputs={'subtitle_manifest':{'path':str(m)},'review_document':{'url':fixtures.DOC},
                 'original_fcpxml':{'path':str(self.original)},'translation_supervision':{'path':str(supervision)}})
        self.rt.pump(self.config,self.sender,snapshotter=self.snapshot);self.verified()
        self.assertEqual(self.get()['step_id'],'align')
        en=self.root/'english.fcpxml';report=X.apply(self.original,manifest,self.doc,en)
        rp=self.root/'timeline.json';rp.write_text(json.dumps(report))
        self.act('complete',outputs={'timeline_report':{'path':str(rp)},'translated_fcpxml':{'path':str(en)}})
        self.assertEqual(self.get()['step_id'],'tts')
        # Use the real local assembly tool and a generated tone, never a cloud TTS call.
        helper=AlignmentTests();helper.root=self.root;helper.report=report
        grouping=next(s for s in w['steps'] if s['id']=='tts')['settings'].get('配音分段','cue/v1')
        segments=helper.segments(grouping=grouping);audio=self.root/'audio.mp3';ar=A.assemble(report,segments,audio,grouping)
        ap=self.root/'audio-report.json';ap.write_text(json.dumps(ar))
        outputs={'audio_alignment_report':{'path':str(ap)},'audio':{'path':str(audio)}}
        with self.assertRaisesRegex(BridgeError,'回执'):self.act('complete',outputs=outputs)
        self.assertEqual(self.get()['step_id'],'tts')
        for s,u in zip(ar['segments'],A.plan(report,grouping)['units']):
            e=self.act('effect_begin',key='tts_'+s['id'],inputs=['timeline_report'],parameters={
                'segment_id':s['id'],'text':u['text'],'model':s['model'],'voice_id':s['voice_id']})
            self.act('effect_finish',effect_id=e['effect_id'],state='succeeded',result={
                'request_id':s['request_id'],'audio_sha256':s['sha256']})
        self.act('complete',outputs=outputs)
        self.assertEqual(self.get()['step_id'],'deliver')
        self.act('complete',outputs={'delivery':{'message_ids':['om_fixture'],'file_keys':['file_fixture']}})
        self.assertEqual(self.get()['state'],'completed')

if __name__=='__main__':unittest.main()
