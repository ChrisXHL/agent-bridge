/* Multiple group configurations, with credentials kept at application level. */
(() => {
 const node=id=>document.getElementById(id), safe=esc;
 const vm={catalog:{apps:[],groups:[]},busy:false,group:null,app:null,groupDirty:false,appDirty:false};
 const area=node('groupsWorkspace');
 area.innerHTML=`<div class="groups-toolbar"><div><h2>群配置</h2><p>每个群有自己的应用、项目和 Prompt。</p></div><button class="btn primary" id="addGroup">＋ 添加群配置</button></div>
 <div id="groupsNotice" class="inline-feedback hidden" role="status" aria-live="polite"></div>
 <div id="groupList"></div><section id="groupEditor" class="group-editor hidden"></section><section id="applicationEditor" class="group-editor hidden"></section>
 <details id="applicationsPanel" class="application-panel"><summary>飞书应用 <span id="appCount"></span></summary><p class="config-help">同一应用可用于多个群，凭据只保存一次。不同主体请分别添加应用。</p><div id="applicationList"></div><button class="btn" id="addApplication">＋ 添加飞书应用</button></details>`;
 const report=(id,message='',error=false)=>{const n=node(id);if(n){n.textContent=message;n.className='inline-feedback'+(message?(error?' error':' success'):' hidden');}};
 function app(ref){return vm.catalog.apps.find(x=>x.ref===ref);}
 function projectOptions(selected){return '<option value="">请选择 Codex 项目</option>'+state.projects.map(p=>`<option value="${safe(p.id)}" ${p.id===selected?'selected':''}>${safe(p.name)}</option>`).join('');}
 function applicationOptions(selected){return '<option value="">请选择飞书应用</option>'+vm.catalog.apps.map(a=>`<option value="${safe(a.ref)}" ${a.ref===selected?'selected':''}>${safe(a.name)} · ${safe(a.app_id)}</option>`).join('');}
 function lists(){
  setHtmlIfChanged('groupList',vm.catalog.groups.map((g,i)=>{const a=app(g.app_ref);return `<article class="group-card"><div class="group-card-top"><div><h3>${safe(g.name)}</h3><p>${safe(g.app_name)} → ${safe(g.project_name)}</p></div><span class="status ${g.enabled&&a?.enabled?'done':'wait'}">${g.enabled&&a?.enabled?'已启用':'已停用'}</span></div><p class="group-prompt-excerpt">${safe(g.prompt_template.split('\n').filter(Boolean).slice(0,2).join(' · '))}</p><div class="group-card-bottom"><span>${g.enabled&&a?.enabled?'群内成员 @机器人即可使用':'启用接收后，群内成员可使用'}</span><button class="btn" data-edit-group="${i}">编辑本群配置</button></div></article>`}).join('')||'<section class="empty-card"><h2>接入第一个群</h2><p>选择飞书应用，在目标群完成配对，然后设置项目和 Prompt。</p></section>');
  node('appCount').textContent=`（${vm.catalog.apps.length}）`;
  setHtmlIfChanged('applicationList',vm.catalog.apps.map((a,i)=>`<div class="application-row"><div><strong>${safe(a.name)}</strong><p>${safe(a.app_id)} · ${safe(a.runtime)}</p></div><button class="btn ghost" data-edit-app="${i}">应用设置</button></div>`).join('')||'<p class="config-help">还没有应用，先添加一次应用凭据。</p>');
 }
 async function refreshCatalog(){try{vm.catalog=await api('/api/feishu/catalog');area.classList.remove('hidden');node('feishuPage').classList.add('multi-groups');lists();pairView();report('groupsNotice');}catch(e){report('groupsNotice',e.message,true);}}
 async function operation(fn){if(vm.busy)return;vm.busy=true;area.querySelectorAll('button').forEach(b=>b.disabled=true);try{await fn();}finally{vm.busy=false;area.querySelectorAll('button').forEach(b=>b.disabled=false);validateGroup();validateApp();}}
 function view(which){node('groupList').classList.toggle('hidden',!!which);node('groupEditor').classList.toggle('hidden',which!=='group');node('applicationEditor').classList.toggle('hidden',which!=='app');node('applicationsPanel').classList.toggle('hidden',!!which);node('addGroup').classList.toggle('hidden',!!which);}
 function leave(){if((vm.groupDirty||vm.appDirty)&&!confirm('有未保存的修改，确定放弃？'))return;vm.group=null;vm.app=null;vm.groupDirty=false;vm.appDirty=false;view(null);}
 function groupEditor(group){
  vm.group=group?{...group,existing:true}:{app_ref:vm.catalog.apps[0]?.ref||'',chat_id:'',name:'',project_id:'',prompt_template:'飞书任务：{{message}}\n\n请执行任务，并说明结果。',enabled:true,existing:false};
  const g=vm.group,a=app(g.app_ref);g.revision=a?.revision;
  if(!group){g.project_id=state.connections.find(c=>c.id===a?.connection_id)?.project_id||'';}
  vm.groupDirty=false;view('group');node('groupEditor').innerHTML=`<button class="mini-link group-back" id="groupBack">← 返回群配置</button><h2>${g.existing?'编辑群配置':'添加群配置'}</h2><form id="groupForm">
  <div class="field"><label for="groupName">配置名称</label><input class="input" id="groupName" maxlength="60" value="${safe(g.name)}" placeholder="例如：翻译群" required></div>
  <div class="field"><label for="groupApplication">飞书应用</label><select class="input" id="groupApplication" ${g.existing?'disabled':''} required>${applicationOptions(g.app_ref)}</select>${g.existing?'':'<button class="mini-link new-app-link" type="button" id="newAppFromGroup">使用其他应用？添加应用</button>'}</div>
  <div id="groupPairing"></div>
  <div class="field"><label for="groupProject">交给哪个 Codex 项目</label><select class="input" id="groupProject" required>${projectOptions(g.project_id)}</select></div>
  <div class="field"><label for="groupPrompt">本群 Prompt</label><textarea class="input" id="groupPrompt" rows="10" spellcheck="false" aria-describedby="groupPromptHelp groupFeedback">${safe(g.prompt_template)}</textarea><p class="config-help" id="groupPromptHelp">保留一个 <code>{{message}}</code>，自动填入本群消息。保存只影响新任务。</p></div>
  <label class="check-label"><input type="checkbox" id="groupEnabled" ${g.enabled?'checked':''}> 启用这个群的消息接收</label>
  <div class="group-save-row"><button class="btn primary" id="saveGroup" type="submit">${g.existing?'保存本群设置':'确认添加这个群'}</button><span>不会修改其他群或历史任务</span></div><div id="groupFeedback" class="inline-feedback hidden" role="status" aria-live="polite"></div></form>`;
  node('groupBack').onclick=leave;
  node('groupForm').oninput=()=>{vm.groupDirty=true;report('groupFeedback');validateGroup();};
  node('groupApplication').onchange=()=>{g.app_ref=node('groupApplication').value;g.chat_id='';g.revision=app(g.app_ref)?.revision;const selected=app(g.app_ref);node('groupProject').value=state.connections.find(c=>c.id===selected?.connection_id)?.project_id||'';pairView();validateGroup();};
  if(node('newAppFromGroup'))node('newAppFromGroup').onclick=()=>applicationEditor(null,true);
  node('groupForm').onsubmit=e=>{e.preventDefault();if(!validateGroup())return;operation(async()=>{try{
   const selected=app(g.app_ref);const payload={app_ref:g.app_ref,revision:g.revision,chat_id:g.chat_id,name:node('groupName').value,project_id:node('groupProject').value,prompt_template:node('groupPrompt').value,enabled:node('groupEnabled').checked};
   if(!g.existing)payload.pairing_code=selected?.pairing_code;
   vm.catalog=await api('/api/feishu/groups',{method:'POST',body:JSON.stringify(payload)});vm.groupDirty=false;vm.group=null;view(null);lists();report('groupsNotice','本群设置已保存。');
  }catch(err){report('groupFeedback',err.message,true);}});};pairView();validateGroup();
 }
 function pairView(){
  const g=vm.group;if(!g||!node('groupPairing'))return;
  if(g.existing){setHtmlIfChanged('groupPairing',`<p class="bound-group">已绑定群：<code>${safe(g.chat_id)}</code></p>`);return;}
  const a=app(g.app_ref);if(!a){setHtmlIfChanged('groupPairing','<p class="config-help">先选择或添加飞书应用。</p>');return;}
  const active=a.pairing_code&&a.pairing_expires_at>Date.now()/1000,c=a.pairing_candidate;
  // Changes made by another editor must not silently update the revision of this draft.
  let html='';
  if(!a.bot_ready)html='<p class="config-help">请在飞书后台启用机器人，配置长连接接收消息事件并发布应用，然后检查状态。</p><button class="btn" type="button" id="groupCheck">检查机器人状态</button>';
  else if(active){
   if(c?.chat_type==='group'){g.chat_id=c.chat_id;html=`<div class="bound-group">已识别目标群：<code>${safe(c.chat_id)}</code><p>请核对群标识并填写配置名称。点击下方“确认添加这个群”后，全群成员可用。</p></div>`;}
   else {g.chat_id='';html=`<div class="bound-group">在目标群 <strong>@${safe(a.bot_name||'机器人')} ${safe(a.pairing_code)}</strong><p>${c?'收到的是私聊消息，请改在目标群发送。':'发送后，这里会自动显示识别结果。'} 配对码有效期至 ${new Date(a.pairing_expires_at*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}。</p></div>`;}
   html+='<button class="btn" type="button" id="groupPair">重新生成配对码</button>';
  }else{g.chat_id='';html='<p class="config-help">把机器人加入目标群，再用配对码识别这个群。</p><button class="btn" type="button" id="groupPair">生成群配对码</button>';}
  if(!a.enabled)html+='<p class="config-help">应用已停用；生成配对码会启动本应用的消息接收。</p>';
  html+='<details class="group-help"><summary>飞书后台如何配置？</summary><p>启用机器人；在“事件与回调”选择长连接，订阅 im.message.receive_v1，开通群内 @机器人消息和机器人回复权限，再发布应用。同一应用不要在其他接收器重复建立长连接。</p></details>';
  setHtmlIfChanged('groupPairing',html);
  const action=kind=>operation(async()=>{try{vm.catalog=await api('/api/feishu/app-pairing',{method:'POST',body:JSON.stringify({app_ref:g.app_ref,action:kind})});g.revision=app(g.app_ref)?.revision;lists();pairView();}catch(e){report('groupFeedback',e.message,true);}});
  if(node('groupPair'))node('groupPair').onclick=()=>action('start');if(node('groupCheck'))node('groupCheck').onclick=()=>action('check');validateGroup();
 }
 function validateGroup(){if(!vm.group||!node('saveGroup'))return false;const v=node('groupPrompt').value;const valid=v.trim()&&v.length<=8000&&v.split('{{message}}').length===2;node('groupPrompt').setAttribute('aria-invalid',String(!valid));const ready=!!(valid&&node('groupName').value.trim()&&node('groupProject').value&&vm.group.chat_id&&vm.group.app_ref);node('saveGroup').disabled=vm.busy||!ready;if(!valid)report('groupFeedback','Prompt 需为 1 到 8000 字，且恰好保留一个 {{message}}。',true);return ready;}
 function applicationEditor(existing,fromGroup=false){
  const a=existing||{name:'',app_id:'',prompt_template:'飞书任务：{{message}}\n\n请执行任务，并说明结果。',allowed_open_ids:[],enabled:false};vm.app={...a,fromGroup};vm.appDirty=false;view('app');
  const project=state.connections.find(c=>c.id===a.connection_id)?.project_id||state.projects[0]?.id||'';
  node('applicationEditor').innerHTML=`<button class="mini-link group-back" id="appBack">← ${fromGroup?'返回群配置编辑':'返回群配置'}</button><h2>${a.ref?'应用设置':'添加飞书应用'}</h2><p class="config-help">同一应用用于多个群时，直接选择已有应用，无需重复填写凭据。</p><form id="applicationForm">
  <div class="field"><label for="appLabel">应用名称</label><input class="input" id="appLabel" maxlength="60" value="${safe(a.name)}" placeholder="例如：公司 A 工作助手" required></div>
  <div class="field"><label for="applicationId">App ID</label><input class="input" id="applicationId" value="${safe(a.app_id)}" ${a.ref?'readonly':''} placeholder="cli_…" required></div>
  <div class="field"><label for="applicationSecret">App Secret</label><input class="input" type="password" autocomplete="new-password" id="applicationSecret" placeholder="${a.has_secret?'留空保留已有密钥':'从飞书开发者后台复制'}" ${a.has_secret?'':'required'}></div>
  ${a.ref?`<label class="check-label"><input type="checkbox" id="applicationEnabled" ${a.enabled?'checked':''}> 启用本应用的消息接收（影响本应用全部群）</label>`:''}
  <div class="field"><label for="applicationProject">默认 Codex 项目</label><select class="input" id="applicationProject" required>${projectOptions(project)}</select><p class="config-help">每个群还可以单独选择项目。</p></div>
  <details class="private-settings"><summary>私聊设置（可选）</summary><p class="config-help">默认只接收已添加的群。需要私聊使用时，再添加允许的用户和 Prompt。</p><div class="field"><label for="applicationUsers">允许私聊的用户 Open ID</label><textarea class="input" id="applicationUsers" rows="2" placeholder="每行一个 ou_…；留空则不接收私聊">${safe(a.allowed_open_ids.join('\n'))}</textarea></div><div class="field"><label for="applicationPrompt">私聊 Prompt</label><textarea class="input" id="applicationPrompt" rows="5">${safe(a.prompt_template)}</textarea><p class="config-help">保留一个 {{message}}。</p></div></details>
  <div class="group-save-row"><button class="btn primary" id="saveApplication">${a.ref?'保存应用设置':'验证并添加应用'}</button></div><div id="applicationFeedback" class="inline-feedback hidden" role="status" aria-live="polite"></div></form>`;
  node('appBack').onclick=()=>{if(vm.appDirty&&!confirm('放弃未保存的应用设置？'))return;vm.appDirty=false;vm.app=null;if(fromGroup){view('group');pairView();}else leave();};
  node('applicationForm').oninput=()=>{vm.appDirty=true;report('applicationFeedback');validateApp();};
  node('applicationForm').onsubmit=e=>{e.preventDefault();if(!validateApp())return;operation(async()=>{try{
   const payload={name:node('appLabel').value,app_id:node('applicationId').value.trim(),app_secret:node('applicationSecret').value.trim(),project_id:node('applicationProject').value,allowed_open_ids:node('applicationUsers').value.split(/[\n,]/).map(x=>x.trim()).filter(Boolean),prompt_template:node('applicationPrompt').value};
   if(a.ref)Object.assign(payload,{ref:a.ref,revision:a.revision,enabled:node('applicationEnabled').checked});
   report('applicationFeedback','正在验证并保存…');vm.catalog=await api('/api/feishu/apps',{method:'POST',body:JSON.stringify(payload)});
   const ref=vm.catalog.saved_app_ref;vm.appDirty=false;vm.app=null;lists();
   if(fromGroup){node('groupApplication').innerHTML=applicationOptions(ref);vm.group.app_ref=ref;vm.group.revision=app(ref)?.revision;vm.group.chat_id='';view('group');pairView();}
   else{view(null);report('groupsNotice','应用已保存。点击“添加群配置”接入群；新应用将在开始配对时启动接收。');}
  }catch(err){report('applicationFeedback',err.message,true);}});};validateApp();
 }
 function validateApp(){if(!vm.app||!node('saveApplication'))return false;const text=node('applicationPrompt').value;const valid=text.trim()&&text.length<=8000&&text.split('{{message}}').length===2;node('applicationPrompt').setAttribute('aria-invalid',String(!valid));node('saveApplication').disabled=vm.busy||!valid;if(!valid){node('applicationPrompt').closest('details').open=true;report('applicationFeedback','私聊 Prompt 需为 1 到 8000 字，且恰好包含一个 {{message}}。',true);}return !!valid;}
 area.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.editGroup!==undefined)groupEditor(vm.catalog.groups[Number(b.dataset.editGroup)]);if(b.dataset.editApp!==undefined)applicationEditor(vm.catalog.apps[Number(b.dataset.editApp)]);});
 node('addGroup').onclick=()=>groupEditor(null);node('addApplication').onclick=()=>applicationEditor(null);
 window.addEventListener('beforeunload',e=>{if(vm.groupDirty||vm.appDirty){e.preventDefault();e.returnValue='';}});
 refresh().then(refreshCatalog);setInterval(()=>{if(!vm.busy)refreshCatalog();},5000);
})();
