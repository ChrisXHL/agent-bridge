const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/app.js'), 'utf8');
const start = source.indexOf('function taskRow(');
const end = source.indexOf('function renderTasks()', start);
const context = vm.createContext({esc: v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),taskBadge:()=>'',timeText:()=>'',conversationAction:()=>''});
vm.runInContext(source.slice(start,end),context);
test('group shows group name with assistant and no workspace arrow',()=>{
 const html=context.taskRow({id:'a',source:'old',source_label:'研发群（资料助手）',project_name:'秘密工作目录',title:'任务'});
 assert.ok(html.includes('研发群（资料助手）'));
 assert.ok(!html.includes('秘密工作目录'));
 assert.ok(!html.includes('→'));
});
test('private chat shows only assistant and escapes display names',()=>{
 const html=context.taskRow({id:'b',source:'old',source_label:'资料<助手>',project_name:'workspace',title:'任务'});
 assert.ok(html.includes('资料&lt;助手&gt;'));
 assert.ok(!html.includes('>old<'));
 assert.ok(!html.includes('→'));
});
test('unidentified external task falls back to source without inventing a group',()=>{
 const html=context.taskRow({id:'c',source:'外部接口',project_name:'workspace',title:'任务'});
 assert.ok(html.includes('外部接口'));
 assert.ok(!html.includes('workspace'));
});
