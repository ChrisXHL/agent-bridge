"""Sentence speech, unchanged display cues, and durable review conflict gates.

All audio is a local fixture; no provider calls or Feishu messages.
"""
import copy
import json
import tempfile
import unittest
import wave
from pathlib import Path

from codex_http_bridge import BridgeError
from workflow_alignment import module, verify_audio_effects
from workflow_audio_policy import upgrade
from workflow_contract import validate_workflow
import test_workflow_runtime as fixtures
from test_workflow_runtime import definition, XML

A=module('audio')

class SentenceAudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.alignment={'schema':'subtitle-alignment/v1','status':'passed','sequence_duration':'4s','units':[
            {'id':'u000008','text':'So what kind of enjoyment can BMW bring to','start':'0s','duration':'38/15s'},
            {'id':'u000009','text':'driving assistance?','start':'38/15s','duration':'7/15s'},
            {'id':'u000010','text':'Next.','start':'3s','duration':'1s'}]}
    def tearDown(self):self.tmp.cleanup()
    def segments(self, grouping='sentence/v1', lengths=(2.9, .5)):
        result=[]
        for u,seconds in zip(A.plan(self.alignment,grouping)['units'],lengths):
            p=self.root/(u['id']+'.wav')
            with wave.open(str(p),'wb') as w:
                w.setparams((1,2,A.RATE,0,'NONE','not compressed'))
                w.writeframes(b'\x01\x01'*round(seconds*A.RATE))
            result.append({'id':u['id'],'path':str(p),'text_sha256':u['text_sha256'],
                'voice_id':'fixture_voice','model':'fixture_model','request_id':'fixture_'+u['id']})
        return result
    def test_fragment_joins_sentence_without_changing_source_units(self):
        original=copy.deepcopy(self.alignment);p=A.plan(self.alignment,'sentence/v1')
        self.assertEqual(self.alignment,original)
        self.assertEqual(p['units'][0]['unit_ids'],['u000008','u000009'])
        self.assertEqual(p['units'][0]['text'],'So what kind of enjoyment can BMW bring to driving assistance?')
        self.assertEqual(p['units'][0]['audio_deadline_frame'],3*A.RATE)
        self.assertEqual([x for g in p['units'] for x in g['unit_ids']],['u000008','u000009','u000010'])
        self.assertEqual(p['source_units'],A.plan(self.alignment)['units'])
    def test_group_can_cross_cue_change_but_not_next_group(self):
        audio=self.root/'out.wav';report=A.assemble(self.alignment,self.segments(),audio,'sentence/v1')
        self.assertEqual(A.verify(self.alignment,report,audio),report)
        self.assertEqual(report['word_alignment'],'not_performed')
        with self.assertRaisesRegex(A.AudioError,'TIME_CONFLICT'):
            A.assemble(self.alignment,self.segments(lengths=(3.001,.5)),self.root/'bad.wav','sentence/v1')
        self.assertFalse((self.root/'bad.wav').exists())
    def test_cue_mode_still_rejects_overlong_fragment(self):
        with self.assertRaisesRegex(A.AudioError,'TIME_CONFLICT.*u000009'):
            A.assemble(self.alignment,self.segments('cue/v1',(2,1.416,.5)),self.root/'bad.wav')
    def test_coverage_text_and_grouping_tampering_rejected(self):
        audio=self.root/'out.wav';report=A.assemble(self.alignment,self.segments(),audio,'sentence/v1')
        for field in ('coverage','source','policy'):
            bad=copy.deepcopy(report)
            if field=='coverage':bad['segments'][0]['unit_ids']=['u000009']
            elif field=='source':bad['source_units'][0]['text']='Changed'
            else:bad['grouping']='cue/v1'
            with self.subTest(field=field),self.assertRaises(A.AudioError):A.verify(self.alignment,bad,audio)
    def test_pause_and_sentence_boundaries_preserved(self):
        self.alignment['units'][0]['duration']='1s'
        p=A.plan(self.alignment,'sentence/v1');self.assertEqual(len(p['units']),3)
        self.assertEqual(p['units'][0]['break_reason'],'gap')
        self.assertEqual(p['units'][0]['audio_deadline_frame'],A.RATE)
    def test_prepare_collect_requires_same_policy(self):
        folder=self.root/'prepared';p=A.prepare(self.alignment,folder,'sentence/v1')
        self.assertEqual(json.loads((folder/'preflight.json').read_text())['status'],'unmeasured')
        for s in self.segments():
            A.write_json(folder/(s['id']+'.receipt.json'),{**s,'output':s['path'],'audio_sha256':A.sha(s['path'])})
        self.assertEqual(len(A.collect(self.alignment,folder,'sentence/v1')),2)
        with self.assertRaisesRegex(A.AudioError,'计划'):A.collect(self.alignment,folder)
        self.assertEqual((folder/(p['units'][0]['id']+'.txt')).read_text(),p['units'][0]['text'])
    def test_group_receipts_and_frozen_policy_are_enforced(self):
        audio=self.root/'out.wav';r=A.assemble(self.alignment,self.segments(),audio,'sentence/v1')
        rp=self.root/'report.json';rp.write_text(json.dumps(r));ap=self.root/'align.json';ap.write_text(json.dumps(self.alignment))
        captured={'r':{'kind':'audio_alignment','path':str(rp)}};ev={'align':{'path':str(ap)}}
        stage={'settings':{'配音分段':'sentence/v1'},'produces':{'r':{'alignment':'align'}}}
        effects=[]
        for s,u in zip(r['segments'],A.plan(self.alignment,'sentence/v1')['units']):
            effects.append({'parameters':json.dumps({'segment_id':s['id'],'text':u['text'],'voice_id':s['voice_id'],'model':s['model']}),
                            'result':json.dumps({'request_id':s['request_id'],'audio_sha256':s['sha256']})})
        verify_audio_effects(captured,ev,stage,effects)
        with self.assertRaises(BridgeError):verify_audio_effects(captured,ev,stage,effects[:1])
        stage['settings']={}
        with self.assertRaisesRegex(BridgeError,'契约'):verify_audio_effects(captured,ev,stage,effects)

class ConflictGateTests(unittest.TestCase):
    setUp=fixtures.WorkflowTests.setUp
    tearDown=fixtures.WorkflowTests.tearDown
    get=fixtures.WorkflowTests.get
    act=fixtures.WorkflowTests.act
    snapshot=fixtures.WorkflowTests.snapshot
    ready=fixtures.WorkflowTests.ready
    click=fixtures.WorkflowTests.click
    verified=fixtures.WorkflowTests.verified
    def conflict_review(self):
        self.ready();self.verified()
        self.act('fail',reason='TIME_CONFLICT fixture 1.416s > 0.467s')
        self.act('reopen_review',stage_id='review');self.rt.pump(self.config,self.sender,snapshotter=self.snapshot)
    def test_same_inputs_cannot_approve_and_card_offers_revision(self):
        self.conflict_review()
        self.assertEqual(self.click(mid='om_card_2'),'unresolved_time_conflict')
        card=json.dumps(self.sender.cards[-1],ensure_ascii=False)
        self.assertNotIn('workflow_approve',card);self.assertIn('workflow_revise',card)
        self.assertEqual(self.get()['state'],'waiting_review')
        self.assertEqual(self.click('revise',mid='om_card_2'),'accepted')
        self.rt.pump(self.config,self.sender,snapshotter=self.snapshot)
        self.assertEqual(self.get()['step_id'],'revise')
    def test_comments_revision_and_new_cards_do_not_clear_conflict(self):
        self.conflict_review();self.doc['document']['revision_id']=2
        self.doc['unresolved_comments']=[{'comment_id':'c','text':'approved again'}]
        self.act('reopen_review',stage_id='review');self.rt.pump(self.config,self.sender,snapshotter=self.snapshot)
        self.assertEqual(self.click(mid='om_card_3'),'unresolved_time_conflict')
        self.assertTrue(self.get()['constraints'][0]['applies_to_current_inputs'])
    def test_title_style_and_entity_changes_do_not_change_speech_context(self):
        self.doc['document'].update(document_id='Fixture',content='<title id="Fixture">Review</title><p id="b1">A &amp; B.</p>')
        self.conflict_review()
        for i,content in enumerate([
            '<title id="Fixture">Renamed review</title><p id="b1">A &amp; B.</p>',
            '<title id="Fixture">Review</title><p id="b1"><b>A</b> &amp; <i>B.</i></p>',
            '<title id="Fixture">Review</title><p id="b1">A &#38; B.</p>']):
            self.doc['document']['content']=content
            self.doc['document']['revision_id']+=1
            self.act('reopen_review',stage_id='review');self.rt.pump(self.config,self.sender,snapshotter=self.snapshot)
            self.assertEqual(self.click(mid='om_card_'+str(i+3)),'unresolved_time_conflict')
            self.assertTrue(self.get()['constraints'][0]['applies_to_current_inputs'])
        self.doc['document']['content']='<title id="Fixture">Review</title><p id="b1">Changed words.</p>'
        self.act('reopen_review',stage_id='review');self.rt.pump(self.config,self.sender,snapshotter=self.snapshot)
        self.assertEqual(self.click(mid='om_card_6'),'accepted')
    def test_revised_text_can_be_approved_and_reverting_reactivates_conflict(self):
        self.conflict_review();self.doc['document']['content']='Shorter English'
        self.assertEqual(self.click('revise',mid='om_card_2'),'accepted');self.rt.pump(self.config,self.sender,snapshotter=self.snapshot)
        self.act('complete',outputs={'doc':{'url':'https://example.feishu.cn/docx/Fixture'}})
        self.rt.pump(self.config,self.sender,snapshotter=self.snapshot)
        self.assertEqual(self.click(mid='om_card_3'),'accepted');self.rt.pump(self.config,self.sender,snapshotter=self.snapshot)
        self.assertEqual(self.get()['step_id'],'align')
        self.doc['document']['content']='English'
        self.act('reopen_review',stage_id='review');self.rt.pump(self.config,self.sender,snapshotter=self.snapshot)
        self.assertEqual(self.click(mid='om_card_4'),'unresolved_time_conflict')
    def test_retry_cannot_execute_same_failed_inputs(self):
        self.ready();self.verified();self.act('fail',reason='TIME_CONFLICT fixture');self.act('retry')
        with self.assertRaisesRegex(BridgeError,'时长冲突'):self.act('effect_begin',key='tts',inputs=['doc'])
        p=self.root/'en.fcpxml';p.write_text(XML.replace('你好','Hi'))
        with self.assertRaisesRegex(BridgeError,'时长冲突'):self.act('complete',outputs={'xml':{'path':str(p)}})
    def test_upgrade_is_scoped_audited_and_keeps_successful_effects(self):
        w=definition();w['steps'][-1]={'id':'finish','name':'配音','kind':'agent','needs':['xml','doc'],'prompt':'tts',
            'produces':{'audio':{'kind':'file'},'ar':{'kind':'audio_alignment','alignment':'xml','output':'audio'}}}
        self.source['message_id']='om_upgrade';self.inbox.accept(self.source)
        self.rid=self.rt.start(self.source,w,self.config);self.ready();self.verified()
        p=self.root/'en.fcpxml';p.write_text(XML.replace('你好','Hi'));self.act('complete',outputs={'xml':{'path':str(p)}})
        effect=self.act('effect_begin',key='tts',parameters={'model':'fixture'})
        self.act('effect_finish',effect_id=effect['effect_id'],state='succeeded',result={'request_id':'fixture'})
        self.act('fail',reason='TIME_CONFLICT fixture');before=self.get()
        self.act('upgrade_audio_grouping',reason='User explicitly requested sentence grouping')
        after=self.get()
        self.assertNotEqual(before['version'],after['version'])
        self.assertEqual(before['artifacts'],after['artifacts']);self.assertEqual(before['effects'],after['effects'])
        self.assertFalse(after['constraints'][0]['applies_to_current_inputs'])
        self.assertEqual(after['history'][-1]['event'],'audio_policy_upgraded')
        self.assertEqual(after['state'],'blocked')
        with self.assertRaises(BridgeError):self.act('upgrade_audio_grouping',reason='repeat')
        self.act('retry');self.assertEqual(self.get()['state'],'running')
    def test_upgrade_does_not_mutate_other_stages(self):
        w=json.loads(Path('examples/subtitle-workflow.json').read_text())
        tts=next(s for s in w['steps'] if s['id']=='tts');tts['settings'].pop('配音分段',None)
        result=upgrade(w);validate_workflow(result,self.config['cards'])
        for original,changed in zip(w['steps'],result['steps']):
            if original['id']!='tts':self.assertEqual(original,changed)

if __name__=='__main__':unittest.main()
