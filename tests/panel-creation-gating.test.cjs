const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/assistants.js'), 'utf8');
const start = source.indexOf('const active=');
const end = source.indexOf('function renderManager()', start);
function harness(jobs, options={}) {
 const elements = new Map();
 const el = id => {
  if (!elements.has(id)) elements.set(id, {value:'创建一位资料助手',disabled:false,textContent:''});
  return elements.get(id);
 };
 const model = {jobs, loaded:true,busy:false,...options};
 const context=vm.createContext({model, el});
 vm.runInContext(source.slice(start,end),context);
 context.validation();
 return {el};
}
test('waiting for group does not block creating another assistant or manager',()=>{
 const h=harness([{kind:'dispatcher',phase:'waiting_group'}]);
 assert.equal(h.el('newAssistant').disabled,false);
 assert.equal(h.el('submitAssistant').disabled,false);
 assert.equal(h.el('enableAssistantManager').disabled,false);
 assert.ok(!source.includes('id="newDispatcher"'));
 assert.ok(!source.includes("el('newDispatcher')"));
});
test('actual registration or configuration blocks duplicate creation',()=>{
 for(const phase of ['planning','registering','waiting_scan','connecting']) {
  const h=harness([{phase}]);
  assert.equal(h.el('newAssistant').disabled,true,phase);
  assert.equal(h.el('submitAssistant').disabled,true,phase);
 }
});
test('terminal attempts do not block new creation',()=>{
 for(const phase of ['ready','failed','needs_check','removed','removing','restoring']) {
  const h=harness([{phase}]);
  assert.equal(h.el('newAssistant').disabled,false,phase);
 }
});
test('loading and submitting still guard duplicate actions',()=>{
 assert.equal(harness([],{loaded:false}).el('newAssistant').disabled,true);
 assert.equal(harness([],{busy:true}).el('newAssistant').disabled,true);
});
