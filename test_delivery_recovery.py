"""Delivery acknowledgement loss, restart safety and private recovery routing."""
import json
import unittest
from unittest.mock import patch
from service import Engine, LocalCodexTransport, Store
from codex_http_bridge import BridgeError
from feishu_worker import result_message
from test_service import FakeTransport
import test_feishu_conversations as conversations
from test_feishu import event


class AcceptedButUnverified(FakeTransport):
    def send_with_receipt(self, thread_id, message, receipt_id, on_accepted):
        turn_id = self.send(thread_id, message)
        self.turns[thread_id][0]['id'] = turn_id
        on_accepted(turn_id)
        raise BridgeError(503, 'delivery_uncertain', 'readback delayed')


class RecoveryTests(unittest.TestCase):
    setUp = conversations.ConversationTests.setUp
    tearDown = conversations.ConversationTests.tearDown
    receive = conversations.ConversationTests.receive
    complete = conversations.ConversationTests.complete

    def uncertain(self, text='请处理这个视频'):
        self.transport = AcceptedButUnverified()
        self.engine = Engine(self.store, self.transport)
        task = self.receive('om_original', text)
        self.engine.tick()
        task = self.store.task(task['id'])
        self.assertEqual(task['error_code'], 'delivery_uncertain')
        self.assertEqual(task['messages'][0]['turn_id'], 'turn-1')
        self.assertEqual(task['messages'][0]['delivery'], 'uncertain')
        return task

    def test_late_readback_recovers_original_turn_without_resend(self):
        task = self.uncertain()
        self.transport.complete(task['thread_id'], '真实结果')
        self.engine.tick()
        done = self.store.task(task['id'])
        self.assertEqual(done['status'], '已完成')
        self.assertEqual(done['result'], '真实结果')
        self.assertEqual(done['messages'][0]['delivery'], 'delivered')
        self.assertEqual(len(self.transport.messages[task['thread_id']]), 1)
        self.adapter.pump_once()
        self.assertIn('真实结果', self.sender.sent[-1][-1])

    def test_unrelated_later_turn_cannot_finish_uncertain_delivery(self):
        task = self.uncertain()
        self.transport.turns[task['thread_id']] = [
            {'id': 'unrelated', 'status': 'completed', 'items': [
                {'type':'agentMessage','text':'其他任务完成'}]}]
        for _ in range(3): self.engine.tick()
        self.assertEqual(self.store.task(task['id'])['error_code'], 'delivery_uncertain')
        self.assertEqual(len(self.transport.messages[task['thread_id']]), 1)

    def test_lost_start_response_can_recover_by_persisted_client_id(self):
        import uuid
        task=self.uncertain()
        mid=task['messages'][0]['id']
        with self.store.db() as db:db.execute('UPDATE messages SET turn_id=NULL WHERE id=?',(mid,))
        self.transport.turns[task['thread_id']][0]['items'][0]['clientId']=str(uuid.uuid5(uuid.NAMESPACE_URL,mid))
        self.transport.complete(task['thread_id'],'启动响应丢失后找回原任务')
        self.engine.tick()
        self.assertEqual(self.store.task(task['id'])['status'],'已完成')
        self.assertEqual(len(self.transport.messages[task['thread_id']]),1)

    def test_recovery_uses_exact_prepared_payload_and_expected_turn(self):
        task = self.uncertain()
        payload = task['messages'][0]['content'] + '\n真实协作入口'
        with self.store.db() as db:
            db.execute('UPDATE delivery_payloads SET content=? WHERE message_id=?',
                       (payload, task['messages'][0]['id']))
        self.transport.turns[task['thread_id']][0]['items'][0]['content'][0]['text'] = payload
        self.transport.complete(task['thread_id'], '属于原请求的结果')
        self.transport.turns[task['thread_id']].append(
            {'id':'other','status':'completed','items':[{'type':'agentMessage','text':'别人的结果'}]})
        self.engine.tick()
        self.assertEqual(self.store.task(task['id'])['result'], '属于原请求的结果')

    def test_completed_task_with_historical_uncertain_receipt_stays_closed_after_restart(self):
        task = self.uncertain()
        self.store.set_task(task['id'], '已完成', '人工核验交付', None)
        before = self.store.task(task['id'])
        reopened = Store(self.store.db_path,self.store.projects_path,self.store.codex_state_path)
        self.assertEqual(reopened.task(task['id']),before)

    def test_restart_retains_accepted_turn_and_recovers_without_replay(self):
        task = self.uncertain()
        with self.store.db() as db:
            db.execute("UPDATE messages SET delivery='sending' WHERE task_id=?",(task['id'],))
        restarted = Store(self.store.db_path,self.store.projects_path,self.store.codex_state_path)
        engine = Engine(restarted,self.transport)
        self.transport.complete(task['thread_id'],'重启后真实结果')
        engine.tick()
        self.assertEqual(restarted.task(task['id'])['status'],'已完成')
        self.assertEqual(len(self.transport.messages[task['thread_id']]),1)

    def test_confirm_is_acknowledged_before_busy_gate_without_new_task(self):
        task = self.uncertain()
        calls = []
        self.client.recover = lambda task_id: calls.append(task_id)
        self.adapter.handle_event(event('om_confirm',text='确认'))
        self.adapter.pump_once()
        receipt = self.inbox.linked_source('om_confirm')
        self.assertEqual(receipt['task_id'],task['id'])
        self.assertEqual(receipt['notified_status'],'recovery_received')
        self.assertEqual(calls,[task['id']])
        self.assertEqual(len(self.store.tasks()),1)
        self.assertIn('不需要再发确认',self.sender.sent[-1][-1])
        self.adapter.pump_once()
        self.assertEqual(calls,[task['id']])

    def test_recovery_endpoint_is_authenticated_and_connection_scoped(self):
        import http.client
        import threading
        from http.server import ThreadingHTTPServer
        from service import make_handler
        task=self.uncertain()
        other=self.store.create_connection('another account','p')
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.store,self.engine,'http://127.0.0.1:0'))
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            def post(token):
                c=http.client.HTTPConnection('127.0.0.1',server.server_port)
                headers={'Content-Type':'application/json'}
                if token:headers['Authorization']='Bearer '+token
                c.request('POST','/v1/tasks/'+task['id']+'/recover','{}',headers)
                r=c.getresponse();data=r.read();c.close();return r.status,json.loads(data)
            self.assertEqual(post(None)[0],401)
            self.assertEqual(post(other['api_key'])[0],404)
            status,body=post(self.conn['api_key'])
            self.assertEqual(status,200)
            self.assertEqual(body['recovery'],'checking')
            self.assertEqual(len(self.transport.messages[task['thread_id']]),1)
        finally:server.shutdown();server.server_close()

    def test_generic_confirmation_never_approves_real_command(self):
        task = self.uncertain()
        self.store.set_task(task['id'],'需要你处理','真实审批','approval_required')
        calls=[];self.client.recover=lambda tid:calls.append(tid)
        self.adapter.handle_event(event('om_confirm',text='确定'))
        self.adapter.pump_once()
        self.assertIsNone(self.inbox.linked_source('om_confirm')['task_id'])
        self.assertEqual(calls,[])

    def test_other_sender_cannot_recover_private_task(self):
        task=self.uncertain()
        calls=[];self.client.recover=lambda tid:calls.append(tid)
        self.receive('om_other','确认',sender='ou_second')
        self.assertEqual(calls,[])
        self.assertNotEqual(self.inbox.linked_source('om_other')['task_id'],task['id'])

    def test_technical_failure_is_not_worded_as_approval(self):
        text=result_message('需要你处理','old technical text','delivery_uncertain')
        self.assertNotIn('需要你确认',text)
        self.assertIn('不需要重复确认',text)
        self.assertIn('确认具体操作',result_message('需要你处理','command','approval_required'))


class DesktopQueueTests(unittest.TestCase):
    setUp = conversations.ConversationTests.setUp
    tearDown = conversations.ConversationTests.tearDown
    receive = conversations.ConversationTests.receive
    complete = conversations.ConversationTests.complete

    def test_desktop_writer_receives_one_durable_queued_message_and_real_turn_is_read_back(self):
        import uuid
        from codex_http_bridge import RpcError
        first=self.complete(self.receive('om_first','原会话'),'原结果')
        task=self.receive('om_desktop','修复测试')
        queued=[]
        def owned(tid):raise RpcError(-32600,'thread has an active writer')
        self.transport.resume_thread=owned
        self.transport.enqueue_desktop=lambda tid,text,mid:queued.append((tid,text,mid))
        self.engine.tick()
        current=self.store.task(task['id'])
        self.assertEqual(current['error_code'],'codex_desktop_queued')
        self.assertEqual(current['messages'][0]['delivery'],'desktop_queued')
        for _ in range(2):self.engine.tick()
        self.assertEqual(len(queued),1)
        tid,text,mid=queued[0]
        self.transport.turns[tid]=[{'id':'desktop-real-turn','status':'completed','items':[
            {'type':'userMessage','clientId':str(uuid.uuid5(uuid.NAMESPACE_URL,mid)),'content':[{'type':'text','text':text}]},
            {'type':'agentMessage','phase':'final_answer','text':'真实桌面回传'}]}]
        self.engine.tick()
        self.assertEqual(self.store.task(task['id'])['result'],'真实桌面回传')
        self.assertEqual(self.store.task(task['id'])['messages'][0]['turn_id'],'desktop-real-turn')
        self.assertEqual(self.store.task(first['id'])['result'],'原结果')

    def test_historical_unsent_message_is_not_automatically_replayed_to_desktop(self):
        from codex_http_bridge import RpcError
        first=self.complete(self.receive('om_first'),'done')
        task=self.receive('om_old','旧排队请求')
        with self.store.db() as db:db.execute("UPDATE settings SET value='9999999999' WHERE key='desktop_queue_after'")
        def owned(tid):raise RpcError(-32600,'active writer')
        self.transport.resume_thread=owned
        queued=[];self.transport.enqueue_desktop=lambda *a:queued.append(a)
        self.engine.tick()
        self.assertEqual(queued,[])
        self.assertEqual(self.store.task(task['id'])['messages'][0]['delivery'],'queued')


class RpcFixture:
    def __init__(self, thread): self.thread=thread;self.requests=[]
    def request(self, method, params):
        self.requests.append((method,params))
        return {'turn':{'id':'accepted-turn'}} if method=='turn/start' else {'thread':self.thread}


class TransportTests(unittest.TestCase):
    def test_acceptance_saved_before_readback_timeout_and_old_identical_text_is_not_proof(self):
        rpc=RpcFixture({'turns':[{'id':'old-turn','items':[{'type':'userMessage','content':[{'type':'text','text':'same'}]}]}]})
        saved=[];transport=LocalCodexTransport(rpc,verify_seconds=0)
        with self.assertRaises(BridgeError) as raised:
            transport.send_with_receipt('thread','same','receipt-1',saved.append)
        self.assertEqual(saved,['accepted-turn'])
        self.assertEqual(raised.exception.extra['turn_id'],'accepted-turn')
        self.assertEqual(len([x for x in rpc.requests if x[0]=='turn/start']),1)
        self.assertIn('clientUserMessageId',rpc.requests[0][1])

    def test_same_accepted_turn_with_actual_text_passes(self):
        rpc=RpcFixture({'turns':[{'id':'accepted-turn','items':[{'type':'userMessage','content':[{'type':'text','text':'same'}]}]}]})
        saved=[]
        self.assertEqual(LocalCodexTransport(rpc,0).send_with_receipt('thread','same','receipt',saved.append),'accepted-turn')
        self.assertEqual(saved,['accepted-turn'])

if __name__=='__main__':unittest.main()
