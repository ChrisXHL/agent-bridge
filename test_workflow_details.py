import json
import unittest
import test_workflow_runtime as fixtures
from workflow_contract import validate_workflow
from codex_http_bridge import BridgeError

class WorkflowDetailsTests(unittest.TestCase):
    def test_details_are_sent_to_executor_and_frozen(self):
        t=fixtures.WorkflowTests();t.setUp()
        try:
            w=fixtures.definition();s=w['steps'][0]
            s.update(skills=['42how-feishu-cli','strict-supervisor:supervise'],acceptance=['监督结论 passed 并保存报告'],settings={'格式':'逐行英文'})
            source={**t.source,'message_id':'om_details'};t.inbox.accept(source)
            rid=t.rt.start(source,w,t.config)
            prompt=t.inbox.cards.source('om_details')['prompt']
            self.assertIn('strict-supervisor:supervise',prompt)
            self.assertIn('监督结论 passed',prompt);self.assertIn('逐行英文',prompt)
            w['steps'][0]['skills']=[]
            self.assertEqual(len(t.rt.get(rid)['stage']['skills']),2)
        finally:t.tearDown()
    def test_malformed_details_are_rejected(self):
        for field,value in [('skills',['bad skill']),('skills',['same','same']),('acceptance','passed'),('settings',{'x':{}})]:
            w=fixtures.definition();w['steps'][0][field]=value
            with self.assertRaises(BridgeError):validate_workflow(w,fixtures.default_cards())
