"""Real isolated ledger transitions; no network calls or production DB writes."""
import unittest
from codex_http_bridge import BridgeError
import test_workflow_runtime as fixture
from test_workflow_runtime import definition, DOC, XML


class AutoRevisionTests(unittest.TestCase):
    setUp=fixture.WorkflowTests.setUp
    tearDown=fixture.WorkflowTests.tearDown
    get=fixture.WorkflowTests.get
    act=fixture.WorkflowTests.act
    snapshot=fixture.WorkflowTests.snapshot
    ready=fixture.WorkflowTests.ready
    click=fixture.WorkflowTests.click
    verified=fixture.WorkflowTests.verified

    def blocked_tts(self, uncertain=False, reason='TIME_CONFLICT actual fixture 2.0s > 1.0s'):
        w=definition()
        w['steps'][-1]={'id':'finish','name':'配音','kind':'agent','needs':['xml','doc'],
            'prompt':'tts','produces':{'audio':{'kind':'file'},'ar':{'kind':'audio_alignment','alignment':'xml','output':'audio'}}}
        self.source['message_id']='om_auto_revision';self.inbox.accept(self.source)
        self.rid=self.rt.start(self.source,w,self.config);self.ready();self.verified()
        p=self.root/'en.fcpxml';p.write_text(XML.replace('你好','Hi'))
        self.act('complete',outputs={'xml':{'path':str(p)}})
        effect=self.act('effect_begin',key='tts',parameters={'text':'English'})
        self.act('effect_finish',effect_id=effect['effect_id'],state='uncertain' if uncertain else 'succeeded',result={'request_id':'fixture'})
        self.act('fail',reason=reason)

    def request(self):
        return self.act('request_revision',stage_id='review',reason='Measured fixture requires shorter wording')

    def test_revise_first_without_extra_card_or_fake_decision(self):
        self.blocked_tts();before=self.get()
        self.doc['unresolved_comments']=[{'comment_id':'c1','text':'keep meaning'}]
        self.request();after=self.get()
        self.assertEqual((after['step_id'],after['state']),('revise','running'))
        self.assertEqual(before['version'],after['version'])
        self.assertEqual(before['decision'],after['decision'])
        self.assertEqual(before['effects'],after['effects'])
        self.assertEqual(before['choices'],after['choices'])
        self.assertEqual(before['artifacts']['original'],after['artifacts']['original'])
        self.assertNotIn('xml',after['artifacts'])
        self.assertEqual(after['history'][-2]['event'],'revision_requested')
        self.assertTrue(after['constraints'][0]['applies_to_current_inputs'])
        self.assertEqual(len(self.sender.cards),1)
        self.assertIn('revision_preview',self.inbox.pending()[-1]['prompt'])
        self.doc['document']['content']='Shorter words.'
        self.doc['document']['revision_id']+=1
        self.act('complete',outputs={'doc':{'url':DOC}})
        self.assertEqual(self.get()['state'],'waiting_review')
        self.rt.pump(self.config,self.sender,snapshotter=self.snapshot)
        self.assertEqual(len(self.sender.cards),2)
        self.assertEqual(self.click(mid='om_card_2'),'accepted')
        self.rt.pump(self.config,self.sender,snapshotter=self.snapshot)
        self.assertEqual(self.get()['step_id'],'align')

    def test_non_conflict_unknown_effect_and_bad_target_do_not_route(self):
        self.blocked_tts(uncertain=True)
        with self.assertRaisesRegex(BridgeError,'外部操作'):self.request()
        before=self.get()
        with self.assertRaises(BridgeError):self.act('request_revision',stage_id='write',reason='x')
        self.assertEqual(before,self.get())

    def test_normal_failure_cannot_request_automatic_rewrite(self):
        self.blocked_tts(reason='provider unavailable')
        with self.assertRaisesRegex(BridgeError,'实测时长冲突'):self.request()

    def test_stale_revision_is_rejected(self):
        self.blocked_tts();old=self.get()['revision'];self.request()
        with self.assertRaisesRegex(BridgeError,'任务已变化'):
            self.rt.act({'run_id':self.rid,'revision':old,'action':'request_revision','stage_id':'review','reason':'x'},snapshotter=self.snapshot)

    def test_snapshot_failure_preserves_blocked_state(self):
        self.blocked_tts();before=self.get()
        def unavailable(url):raise BridgeError(502,'unavailable','cannot read comments')
        with self.assertRaises(BridgeError):self.rt.act({'run_id':self.rid,'revision':before['revision'],'action':'request_revision','stage_id':'review','reason':'x'},snapshotter=unavailable)
        self.assertEqual(before,self.get())

    def test_noop_revision_cannot_make_failed_text_approvable(self):
        self.blocked_tts();self.request()
        self.act('complete',outputs={'doc':{'url':DOC}})
        self.rt.pump(self.config,self.sender,snapshotter=self.snapshot)
        self.assertEqual(self.click(mid='om_card_2'),'unresolved_time_conflict')

    def test_effect_becoming_uncertain_during_snapshot_stops_transition(self):
        self.blocked_tts();before=self.get()
        def concurrent_effect(url):
            with self.inbox.db() as db:
                db.execute("UPDATE workflow_effects SET state='uncertain' WHERE run_id=?",(self.rid,))
            return self.snapshot(url)
        with self.assertRaisesRegex(BridgeError,'未确认'):
            self.rt.act({'run_id':self.rid,'revision':before['revision'],'action':'request_revision','stage_id':'review','reason':'x'},snapshotter=concurrent_effect)
        self.assertEqual(self.get()['step_id'],'finish')
        self.assertEqual(self.get()['state'],'blocked')

    def test_bounded_automatic_attempts(self):
        self.blocked_tts()
        with self.inbox.db() as db:
            for i in range(3):self.rt.event(db,self.rt.row(db,self.rid),'revision_requested',{'attempt':i+1})
        before=self.get()
        with self.assertRaisesRegex(BridgeError,'三轮'):self.request()
        self.assertEqual(before,self.get())


if __name__=='__main__':unittest.main()
