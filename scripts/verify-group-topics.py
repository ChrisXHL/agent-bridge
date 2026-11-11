#!/usr/bin/env python3
"""Real Codex discussion, clarification and multi-member follow-up; no real Feishu sends."""
import json
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_group_topics import TopicTests
from feishu_groups import atomic_write
from service import LocalCodexTransport

out=Path(__file__).resolve().parents[1]/'artifacts'/('group-topics-live-'+time.strftime('%Y%m%d-%H%M%S'))
out.mkdir(mode=0o700)
fixture=TopicTests();fixture.setUp()
root_task=None
try:
    # Replace only the isolated fixture's transport. All identities and Feishu delivery remain fake.
    fixture.engine.transport=LocalCodexTransport()
    cfg=fixture.cfg
    cfg['group_configs']['oc_shared']['prompt_template']='''群任务：{{message}}
这是隔离验收，只能访问本任务目录、明确提供的 Bridge 协作工具和子任务；不要访问其他个人资料、调用飞书/云端接口、修改配置或创建应用。
你是本话题负责人。首次要求核验求和时，必须通过 Bridge 协作工具真实委托“研究助手”，不得模拟角色或自己冒充子助手完成。
委托内容：先用 ask 问负责人“是否把 4 纳入 17 与 28 的求和？”，用 question 等候真实答复，再用终端 python 计算，say 汇报简短发现，返回合计。
你收到问题后用 answer 明确答“是，把 4 纳入”；收齐子任务结果再完成。最终只返回 JSON，字段 total 和 delegated，首次 total=实际求和结果，delegated=true。
后续群成员如果明确要求只加一，不再委托，直接按已经收齐的结果计算，返回同样 JSON，此时 delegated=false。
所有 say/ask/answer 均只能使用 Bridge 注入的当前任务协作工具，不得直接操作外部消息。'''
    atomic_write(fixture.o.groups.resolve(fixture.f.source['app_ref'])[1],cfg)
    event=fixture.event();event['event']['message']['content']=json.dumps({'text':'@bot 首次任务：请和研究助手共同核验 17、28、4 的合计。'})
    assert fixture.main.handle_event(event)
    fixture.main.pump_once();root_task=fixture.main.inbox.linked_source('om_group')['task_id']
    fixture.engine.start();fixture.c.topics.start()
    print('ARTIFACT '+str(out),flush=True)
    def until_done(task,seconds=600):
        deadline=time.monotonic()+seconds;last=None
        while time.monotonic()<deadline:
            state=fixture.store.task(task)
            if state['status']!=last:print('STATE '+state['status'],flush=True);last=state['status']
            if state['status'] in ('已完成','失败','已停止','需要你处理'):return state
            time.sleep(2)
        raise TimeoutError('isolated Codex task did not finish')
    first=until_done(root_task)
    def decoded(text):
        text=text.strip().removeprefix('```json').removesuffix('```').strip();return json.loads(text)
    assert first['status']=='已完成',first.get('error_code')
    one=decoded(first['result']);assert one=={'total':49,'delegated':True},one
    # Drain topic discussion before final result. No real Feishu sender exists in this fixture.
    fixture.c.topics.reconcile();fixture.c.topics.deliver();fixture.main.pump_once()
    follow=fixture.event('om_group_followup',sender='ou_second_member',parent='om_group',mention=False)
    follow['event']['message']['content']=json.dumps({'text':'我来补充：基于刚才的合计只加一，不再委托其他助手。'})
    assert fixture.main.handle_event(follow);fixture.main.pump_once()
    second_task=fixture.main.inbox.linked_source('om_group_followup')['task_id'];assert second_task==root_task
    second=until_done(root_task,300);assert second['status']=='已完成',second.get('error_code')
    two=decoded(second['result']);assert two=={'total':50,'delegated':False},two
    assert second['thread_id']==first['thread_id']
    fixture.c.topics.reconcile();fixture.c.topics.deliver();fixture.main.pump_once()
    with fixture.store.db() as db:
        calls=[dict(r) for r in db.execute('SELECT child_task,target_name FROM assistant_calls WHERE root_task=?',(root_task,))]
        questions=[dict(r) for r in db.execute('SELECT question,answer FROM group_questions')]
        discussions=[dict(r) for r in db.execute('SELECT actor_id,event_key,body,state FROM group_discussions ORDER BY created_at')]
    assert len(calls)==1 and questions and questions[0]['answer'] and len(discussions)>=4
    assert all(x['state']=='sent' for x in discussions)
    result={'real_codex_execution':True,'feishu_events_and_delivery_simulated':True,'real_feishu_messages_sent':0,
       'first_result':one,'second_result':two,'same_task':second_task==root_task,'same_codex_thread':True,
       'real_child_tasks':len(calls),'clarification_round_trips':len(questions),'discussion_messages':len(discussions),
       'discussions':discussions,'all_replies_in_original_topic':all(x['root']=='om_group' for x in fixture.sent)}
    assert result['all_replies_in_original_topic']
    atomic_write(out/'verification.json',result);print(json.dumps({k:v for k,v in result.items() if k!='discussions'},ensure_ascii=False),flush=True)
except Exception as exc:
    evidence={'error_type':type(exc).__name__,'error':str(exc),'real_feishu_messages_sent':0}
    if root_task:
        t=fixture.store.task(root_task);evidence['root_status']=t['status'];evidence['root_result']=t.get('result')
    atomic_write(out/'failure.json',evidence)
    raise
finally:
    fixture.c.topics.close();fixture.engine.close();fixture.tearDown()
