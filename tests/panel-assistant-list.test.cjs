const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../static/assistants.js'),'utf8');
const start=source.indexOf('function render(){');
const end=source.indexOf('async function refreshJobs()',start);
function render(jobs){
 const elements=new Map();
 const el=id=>{if(!elements.has(id))elements.set(id,{textContent:'',disabled:false});return elements.get(id);};
 const output={};
 const context=vm.createContext({model:{jobs,policy:{mode:'open'},busy:false,showRemoved:false},el,labels:{waiting_group:'等你添加到群',planning:'正在准备'},active:j=>['planning','registering','waiting_scan','connecting'].includes(j.phase),
  esc:v=>String(v??''),badge:()=>'',renderManager(){},validation(){},
  setHtmlIfChanged(id,html){output[id]=html;}});
 vm.runInContext(source.slice(start,end),context);context.render();return output.assistantJobs;
}
test('waiting dispatcher lives inside existing assistant details without onboarding steps',()=>{
 const html=render([{id:'dispatcher-one',name:'任务管家',kind:'dispatcher',phase:'waiting_group',message:'等待加群',limitations:''}]);
 const details=html.indexOf('<details');
 const card=html.indexOf('id="assistant-dispatcher-one"');
 assert.ok(details>=0&&card>details);
 assert.ok(html.includes('已创建的助手（1）'));
 assert.ok(!html.includes('assistant-progress'));
 assert.ok(!html.includes('创建任务管家'));
});
test('real unfinished registration remains visible above existing assistants',()=>{
 const html=render([{id:'pending',name:'新助手',phase:'planning',message:'正在准备'},
 {id:'dispatcher',name:'任务管家',phase:'waiting_group',kind:'dispatcher',message:'等待加群'}]);
 assert.ok(html.indexOf('id="assistant-pending"')<html.indexOf('<details'));
 assert.ok(html.indexOf('id="assistant-dispatcher"')>html.indexOf('<details'));
});
