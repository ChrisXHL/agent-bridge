const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
function harness(){
 const elements=new Map();
 const $=id=>{if(!elements.has(id)){const classes=new Set(['hidden']);elements.set(id,{textContent:'',dataset:{},className:'',classList:{toggle(c,on){if(on)classes.add(c);else classes.delete(c);},contains:c=>classes.has(c),add:c=>classes.add(c),remove:c=>classes.delete(c)}});}return elements.get(id);};
 const state={page:'tasks',drawer:{kind:'task',id:'task-one'}};
 const context=vm.createContext({$,state,Date,setTimeout:(f)=>setTimeout(f,0),Map,encodeURIComponent,esc:v=>String(v??''),timeText:()=>'',taskStatus:()=>'',conversationAction:()=>'',setHtmlIfChanged(){},
 api:async()=>({messages:[],id:'task-one'}),toast(){throw new Error('Polling must not toast');},
 feedback(id,message='',tone='info'){const n=$(id);delete n.dataset.pollTask;n.textContent=message;n.className=tone;},
 openOverlay(){},renderTasks(){},renderConnections(){},renderFeishu(){}});
 const a=source.indexOf('let refreshPromise=null;'),b=source.indexOf('const renderedHtml=',a);
 vm.runInContext(source.slice(a,b),context);
 const c=source.indexOf('const taskDetailFailures='),d=source.indexOf('function connectionText(',c);
 vm.runInContext(source.slice(c,d),context);
 return {context,$,state};
}
test('a single missed poll stays silent; repeated failures show the endpoint; recovery clears it',async()=>{
 const h=harness();let fail=true;let calls=0;
 h.context.api=async p=>{if(p.startsWith('/api/tasks')&&fail){calls++;throw new Error('读取失败');}return p==='/healthz'?{status:'ok'}:[];};
 h.state.drawer=null;
 assert.equal(await h.context.refresh(),false);
 assert.equal(calls,2,'each refresh retries a failed endpoint once');
 assert.equal(h.$('serviceNotice').classList.contains('hidden'),true);
 await h.context.refresh();
 assert.equal(h.$('serviceNotice').classList.contains('hidden'),true);
 await h.context.refresh();
 assert.equal(h.$('serviceNotice').classList.contains('hidden'),false);
 assert.match(h.$('serviceNoticeText').textContent,/任务列表：读取失败/);
 fail=false;
 assert.equal(await h.context.refresh(),true);
 assert.equal(h.$('serviceNotice').classList.contains('hidden'),true);
});
test('transient failure recovered by the in-refresh retry shows nothing',async()=>{
 const h=harness();let n=0;
 h.context.api=async p=>{if(p==='/api/feishu'&&n++===0)throw new Error('无法连接本机 Bridge 服务');return p==='/healthz'?{status:'ok'}:[];};
 h.state.drawer=null;
 assert.equal(await h.context.refresh(),true);
 assert.equal(h.$('serviceNotice').classList.contains('hidden'),true);
});
test('healthy polling shows no warning and paused state remains visible',()=>{
 const h=harness();h.context.panelRefreshNotice();
 assert.equal(h.$('serviceNotice').classList.contains('hidden'),true);
 h.context.panelRefreshNotice(null,true);
 assert.match(h.$('serviceNoticeText').textContent,/Bridge 已暂停/);
 assert.equal(h.$('retryRefresh').classList.contains('hidden'),true);
});
test('detail polling failure is persistent inline then reports recovery once',async()=>{
 const h=harness();h.context.api=async()=>{throw new Error('详情连接中断');};
 await h.context.showTask('task-one',false);
 assert.match(h.$('drawerFeedback').textContent,/详情连接中断/);
 assert.equal(h.$('drawerFeedback').dataset.pollTask,'task-one');
 h.context.api=async()=>({messages:[]});
 await h.context.showTask('task-one',false);
 assert.match(h.$('drawerFeedback').textContent,/更新已恢复/);
 h.context.feedback('drawerFeedback','决定已提交','success');
 await h.context.showTask('task-one',false);
 assert.equal(h.$('drawerFeedback').textContent,'决定已提交');
});
test('stale detail request cannot write warning into another drawer',async()=>{
 const h=harness();h.state.drawer={kind:'settings',id:'different'};
 h.context.api=async()=>{throw new Error('late error');};
 await h.context.showTask('task-one',false);
 assert.equal(h.$('drawerFeedback').textContent,'');
});
