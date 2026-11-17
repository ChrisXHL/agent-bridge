const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../static/feishu-groups.js'),'utf8');
const start=source.indexOf('function bindFolderField(');
const end=source.indexOf('function applicationOptions(',start);

for(const prefix of ['existing','application','group'])test(`folder selection maintains correct dirty state: ${prefix}`,async()=>{
 const nodes=new Map();
 function node(id){if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',innerHTML:'',classList:{},addEventListener(){}});return nodes.get(id);}
 const context=vm.createContext({node,vm:{groupDirty:false,appDirty:false},state:{projects:[]},projectOptions:()=>'',report(){},operation:fn=>fn(),api:async()=>({cancelled:false,project:{id:'new',name:'picked',path:'/fixture'},projects:[{id:'new',name:'picked',path:'/fixture'}]})});
 vm.runInContext(source.slice(start,end),context);
 context.bindFolderField(prefix,'select','feedback');
 await node(prefix+'PickFolder').onclick();
 assert.equal(node('select').value,'new');
 assert.equal(context.vm.groupDirty,prefix==='group');
 assert.equal(context.vm.appDirty,prefix==='application');
});
