const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../static/feishu-groups.js'),'utf8');

test('switching group application rerenders shared impact and retains add mode',()=>{
 const nodes=new Map();
 function node(id){if(!nodes.has(id))nodes.set(id,{value:'',innerHTML:'',checked:true,disabled:false,classList:{},setAttribute(){}});return nodes.get(id);}
 const apps=[{ref:'legacy',revision:'1',prompt_template:'OLD {{message}}'},
             {ref:'shared',revision:'2',prompt_template:'SHARED {{message}}',prompt_policy_version:1}];
 const state={connections:[],projects:[]};
 const model={catalog:{apps,capabilities:{group_topics:false}}};
 const context=vm.createContext({node,vm:model,state,app:ref=>apps.find(a=>a.ref===ref),safe:x=>String(x??''),
   view(){},applicationOptions:()=>'',projectOptions:()=>'',folderField:()=>'',bindFolderField(){},
   leave(){},report(){},pairView(){},validateGroup(){},applicationEditor(){},operation(){}});
 const start=source.indexOf('function groupEditor('),end=source.indexOf('function pairView(',start);
 vm.runInContext(source.slice(start,end),context);
 context.groupEditor(null);
 assert.match(node('groupEditor').innerHTML,/旧版独立配置/);
 node('groupApplication').value='shared';node('groupApplication').onchange();
 assert.match(node('groupEditor').innerHTML,/全部群聊和私聊共用/);
 assert.match(node('groupEditor').innerHTML,/SHARED/);
 assert.equal(model.group.existing,false);assert.equal(model.group.app_ref,'shared');
 assert.doesNotMatch(node('groupEditor').innerHTML,/旧版配置只影响本群/);
 node('groupApplication').value='legacy';node('groupApplication').onchange();
 assert.match(node('groupEditor').innerHTML,/旧版配置只影响本群/);
 assert.equal(model.group.existing,false);
});
