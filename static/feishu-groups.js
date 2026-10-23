/* Multiple group configurations, with credentials kept at application level. */
(() => {
 const node=id=>document.getElementById(id), safe=esc;
 const vm={catalog:{apps:[],groups:[]},busy:false,group:null,app:null,groupDirty:false,appDirty:false};
 const area=node('groupsWorkspace');
 area.innerHTML=`<div class="groups-toolbar"><div><h2>群配置</h2><p>最简单的方式：把助手拉进群，由创建者在群里 @它说第一句需求，就会自动接入。这里可以修改或删除已接入的群。</p></div><button class="btn primary" id="addGroup">＋ 添加群配置</button></div>
 <div id="groupsNotice" class="inline-feedback hidden" role="status" aria-live="polite"></div>
 <div id="groupList"></div><section id="groupEditor" class="group-editor hidden"></section><section id="applicationEditor" class="group-editor hidden"></section>
 <details id="applicationsPanel" class="application-panel"><summary>飞书应用 <span id="appCount"></span></summary><p class="config-help">同一应用可用于多个群，凭据只保存一次。不同主体请分别添加应用。</p><div id="applicationList"></div><button class="btn" id="addApplication">接入已有机器人</button></details>`;
 const report=(id,message='',error=false)=>{const n=node(id);if(n){n.textContent=message;n.className='inline-feedback'+(message?(error?' error':' success'):' hidden');}};
 function app(ref){return vm.catalog.apps.find(x=>x.ref===ref);}
 function projectOptions(selected){return '<option value="">请选择工作目录</option>'+state.projects.map(p=>`<option value="${safe(p.id)}" ${p.id===selected?'selected':''}>${safe(p.name)}</option>`).join('');}
 // The assistant manager only serves its creator's private chat; it can never own a group.
 function groupApps(){return vm.catalog.apps.filter(a=>!a.assistant_manager);}
 function defaultProject(ref){const a=app(ref);return vm.catalog.groups.find(x=>x.app_ref===ref&&x.project_id)?.project_id||state.connections.find(c=>c.id===a?.connection_id)?.project_id||'';}
 function applicationOptions(selected){return '<option value="">请选择飞书应用</option>'+groupApps().map(a=>`<option value="${safe(a.ref)}" ${a.ref===selected?'selected':''}>${safe(a.name)} · ${safe(a.app_id)}</option>`).join('');}
 function lists(){
  setHtmlIfChanged('groupList',vm.catalog.groups.map((g,i)=>{const a=app(g.app_ref);return `<article class="group-card"><div class="group-card-top"><div><h3>${safe(g.chat_title||g.name)}</h3><p>${safe(g.app_name)} → ${safe(g.project_name)}${g.chat_title&&g.name!==g.chat_title?' · 配置名：'+safe(g.name):''}</p></div><span class="status ${g.enabled&&a?.enabled?'done':'wait'}">${g.enabled&&a?.enabled?'已启用':'已停用'}</span></div><p class="group-prompt-excerpt">${safe(g.prompt_template.split('\n').filter(Boolean).slice(0,2).join(' · '))}</p><div class="group-card-bottom"><span>${g.enabled&&a?.enabled?(g.topic_collaboration?'群内协作 · 首次 @，回复可继续':'群内成员 @机器人即可使用'):'启用接收后，群内成员可使用'}</span><button class="btn" data-edit-group="${i}">编辑本群配置</button></div></article>`}).join('')||'<section class="empty-card"><h2>接入第一个群</h2><p>选择飞书应用，在目标群完成配对，然后设置项目和 Prompt。</p></section>');
  node('appCount').textContent=`（${vm.catalog.apps.length}）`;
  setHtmlIfChanged('applicationList',vm.catalog.apps.map((a,i)=>`<div class="application-row"><div><strong>${safe(a.name)}</strong><p>${safe(a.app_id)} · ${safe(a.runtime)}</p></div><button class="btn ghost" data-edit-app="${i}">应用设置</button></div>`).join('')||'<p class="config-help">还没有应用，先添加一次应用凭据。</p>');
 }
 async function fillGroupPurpose(g){const ref=g.app_ref;try{const defaults=await api('/api/feishu/group-defaults',{method:'POST',body:JSON.stringify({app_ref:ref})});if(vm.group===g&&g.app_ref===ref&&!vm.promptDirty){node('groupPrompt').value=defaults.prompt_template;validateGroup();}}catch(e){if(vm.group===g)report('groupFeedback',e.message,true);}}
 async function refreshCatalog(){try{vm.catalog=await api('/api/feishu/catalog');area.classList.remove('hidden');node('feishuPage').classList.add('multi-groups');lists();pairView();report('groupsNotice');}catch(e){report('groupsNotice',e.message,true);}}
 async function operation(fn){if(vm.busy)return;vm.busy=true;area.querySelectorAll('button').forEach(b=>b.disabled=true);try{await fn();}finally{vm.busy=false;area.querySelectorAll('button').forEach(b=>b.disabled=false);validateGroup();validateApp();}}
 function view(which){node('groupList').classList.toggle('hidden',!!which);node('groupEditor').classList.toggle('hidden',which!=='group');node('applicationEditor').classList.toggle('hidden',which!=='app');node('applicationsPanel').classList.toggle('hidden',!!which);node('addGroup').classList.toggle('hidden',!!which);}
 function leave(){if((vm.groupDirty||vm.appDirty)&&!confirm('有未保存的修改，确定放弃？'))return;vm.group=null;vm.app=null;vm.groupDirty=false;vm.appDirty=false;view(null);}
 function groupEditor(group,presetRef){
  vm.group=group?{...group,existing:true}:{app_ref:(groupApps().find(a=>a.ref===presetRef)||groupApps()[0])?.ref||'',chat_id:'',name:'',project_id:'',prompt_template:'飞书任务：{{message}}\n\n请执行任务，并说明结果。',enabled:true,topic_collaboration:true,existing:false};
  const g=vm.group,a=app(g.app_ref);g.revision=a?.revision;
  if(!group){g.project_id=defaultProject(g.app_ref);g.autoPair=true;}
  vm.groupDirty=false;vm.promptDirty=false;view('group');node('groupEditor').innerHTML=`<button class="mini-link group-back" id="groupBack">← 返回群配置</button><h2>${g.existing?'编辑群配置':'添加群配置'}</h2><form id="groupForm">
  <div class="field"><label for="groupName">配置名称</label><input class="input" id="groupName" maxlength="60" value="${safe(g.name)}" placeholder="例如：翻译群" required></div>
  <div class="field"><label for="groupApplication">飞书应用</label><select class="input" id="groupApplication" ${g.existing?'disabled':''} required>${applicationOptions(g.app_ref)}</select>${g.existing?'':'<button class="mini-link new-app-link" type="button" id="newAppFromGroup">使用其他应用？添加应用</button>'}</div>
  <div id="groupPairing"></div>
  <div class="field"><label for="groupProject">在哪个工作目录执行（助手项目）</label><select class="input" id="groupProject" required>${projectOptions(g.project_id)}</select><button class="mini-link" type="button" data-pick-folder="groupProject">选择本地文件夹…</button><p class="config-help folder-path" id="groupProjectPath"></p></div>
  <div class="field"><label for="groupPrompt">本群 Prompt</label><textarea class="input" id="groupPrompt" rows="10" spellcheck="false" aria-describedby="groupPromptHelp groupFeedback">${safe(g.prompt_template)}</textarea><p class="config-help" id="groupPromptHelp">保留一个 <code>{{message}}</code>，自动填入本群消息。保存只影响新任务。</p></div>
  ${vm.catalog.capabilities?.group_topics?`<label class="check-label"><input type="checkbox" id="groupTopics" ${g.topic_collaboration?'checked':''}> 开启群内协作</label>
  <p class="config-help">助手直接在普通群聊天中发言，不使用话题回复。首次 @ 开始任务，回复相关消息即可继续；已接入本群的助手用自己的身份交流。保存时自动检查群消息权限。未接入本群的助手由负责人注明身份转述。</p>`:''}
  <label class="check-label"><input type="checkbox" id="groupEnabled" ${g.enabled?'checked':''}> 启用这个群的消息接收</label>
  <div class="group-save-row"><button class="btn primary" id="saveGroup" type="submit">${g.existing?'保存本群设置':'确认添加这个群'}</button><span>不会修改其他群或历史任务</span>${g.existing?'<button class="btn ghost" type="button" id="removeGroup">删除本群配置</button>':''}</div><div id="groupFeedback" class="inline-feedback hidden" role="status" aria-live="polite"></div></form>`;
  node('groupBack').onclick=leave;showFolder('groupProject');
  node('groupForm').oninput=e=>{vm.groupDirty=true;if(e.target.id==='groupPrompt')vm.promptDirty=true;report('groupFeedback');validateGroup();};
  // Two-step confirm in the page itself: embedded browsers may suppress window.confirm.
  if(node('removeGroup'))node('removeGroup').onclick=()=>{const b=node('removeGroup');
   if(!b.dataset.armed){b.dataset.armed='1';b.textContent='确认删除（再点一次）';b.classList.add('danger');report('groupFeedback',`将删除群配置“${g.name}”：机器人不再响应这个群，已有任务记录保留。再次点击按钮确认。`,true);b.scrollIntoView({block:'center'});return;}
   operation(async()=>{try{
    // Deleting only touches this group, so use the latest application revision.
    vm.catalog=await api('/api/feishu/catalog');const latest=app(g.app_ref);
    vm.catalog=await api('/api/feishu/groups/remove',{method:'POST',body:JSON.stringify({app_ref:g.app_ref,revision:latest?.revision,chat_id:g.chat_id})});vm.groupDirty=false;vm.group=null;view(null);lists();report('groupsNotice','群配置已删除。');node('groupsNotice').scrollIntoView({block:'center'});
   }catch(err){delete b.dataset.armed;b.textContent='删除本群配置';report('groupFeedback',err.message,true);node('groupFeedback').scrollIntoView({block:'center'});}});};
  node('groupApplication').onchange=()=>{g.app_ref=node('groupApplication').value;g.chat_id='';g.autoPair=true;g.revision=app(g.app_ref)?.revision;node('groupProject').value=defaultProject(g.app_ref);showFolder('groupProject');if(vm.catalog.capabilities?.group_topics)fillGroupPurpose(g);pairView();validateGroup();};
  if(!g.existing&&vm.catalog.capabilities?.group_topics)fillGroupPurpose(g);
  if(node('newAppFromGroup'))node('newAppFromGroup').onclick=()=>applicationEditor(null,true);
  node('groupForm').onsubmit=e=>{e.preventDefault();if(!validateGroup())return;operation(async()=>{try{
   const selected=app(g.app_ref);const payload={app_ref:g.app_ref,revision:g.revision,chat_id:g.chat_id,name:node('groupName').value,project_id:node('groupProject').value,prompt_template:node('groupPrompt').value,enabled:node('groupEnabled').checked};
   if(node('groupTopics'))payload.topic_collaboration=node('groupTopics').checked;
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
   const until=new Date(a.pairing_expires_at*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});
   if(c?.chat_type==='group'&&vm.catalog.groups.some(x=>x.app_ref===a.ref&&x.chat_id===c.chat_id)){g.chat_id='';html=`<div class="bound-group">这个群已经配置过，请返回列表编辑它，或在另一个群发送配对码。</div>`;}
   else if(c?.chat_type==='group'){g.chat_id=c.chat_id;if(!node('groupName').value.trim())node('groupName').value=`${a.name} · 群 ${c.chat_id.slice(-6)}`;html=`<div class="bound-group">✓ 已识别目标群：<code>${safe(c.chat_id)}</code><p>第 3 步：核对下方名称和项目，点击“确认添加这个群”，全群成员即可 @机器人使用。</p></div>`;}
   else {g.chat_id='';html=`<ol class="config-help pair-steps"><li>把「${safe(a.bot_name||a.name)}」机器人拉进目标群（已在群里可跳过）。</li><li>在群里 @${safe(a.bot_name||'机器人')} 并发送：<strong>${safe(a.pairing_code)}</strong>（有效至 ${until}）</li><li>回到这里确认添加。</li></ol><p class="config-help">${c?'收到的是私聊消息，请改在目标群发送。':'发送后，这里会自动显示识别结果。'}</p>`;}
   html+='<button class="btn" type="button" id="groupPair">重新生成配对码</button>';
  }else{g.chat_id='';html=`<p class="config-help">${g.autoPair?'正在生成配对码…':'配对码已过期，请重新生成。'}</p><button class="btn" type="button" id="groupPair">生成群配对码</button>`;}
  if(!a.enabled)html+='<p class="config-help">应用已停用；生成配对码会启动本应用的消息接收。</p>';
  html+='<details class="group-help"><summary>飞书后台如何配置？</summary><p>启用机器人；在“事件与回调”选择长连接，订阅 im.message.receive_v1，开通群内 @机器人消息和机器人回复权限，再发布应用。同一应用不要在其他接收器重复建立长连接。</p></details>';
  setHtmlIfChanged('groupPairing',html);
  const action=kind=>operation(async()=>{try{vm.catalog=await api('/api/feishu/app-pairing',{method:'POST',body:JSON.stringify({app_ref:g.app_ref,action:kind})});g.revision=app(g.app_ref)?.revision;lists();pairView();}catch(e){report('groupFeedback',e.message,true);}});
  if(node('groupPair'))node('groupPair').onclick=()=>action('start');if(node('groupCheck'))node('groupCheck').onclick=()=>action('check');
  // A new group needs a code anyway: generate it once instead of asking for another click.
  if(g.autoPair&&a.bot_ready&&!active&&!vm.busy){g.autoPair=false;setTimeout(()=>action('start'));}
  validateGroup();
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
  <div class="field"><label for="applicationProject">默认工作目录（助手项目）</label><select class="input" id="applicationProject" required>${projectOptions(project)}</select><button class="mini-link" type="button" data-pick-folder="applicationProject">选择本地文件夹…</button><p class="config-help folder-path" id="applicationProjectPath"></p><p class="config-help">每个群还可以单独选择项目。</p></div>
  <details class="private-settings"><summary>私聊设置（可选）</summary><p class="config-help">默认只接收已添加的群。需要私聊使用时，再添加允许的用户和 Prompt。</p><div class="field"><label for="applicationUsers">允许私聊的用户 Open ID</label><textarea class="input" id="applicationUsers" rows="2" placeholder="每行一个 ou_…；留空则不接收私聊">${safe(a.allowed_open_ids.join('\n'))}</textarea></div><div class="field"><label for="applicationPrompt">私聊 Prompt</label><textarea class="input" id="applicationPrompt" rows="5">${safe(a.prompt_template)}</textarea><p class="config-help">保留一个 {{message}}。</p></div></details>
  <div class="group-save-row"><button class="btn primary" id="saveApplication">${a.ref?'保存应用设置':'验证并添加应用'}</button></div><div id="applicationFeedback" class="inline-feedback hidden" role="status" aria-live="polite"></div></form>`;
  node('appBack').onclick=()=>{if(vm.appDirty&&!confirm('放弃未保存的应用设置？'))return;vm.appDirty=false;vm.app=null;if(fromGroup){view('group');pairView();}else leave();};
  showFolder('applicationProject');
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
 function showFolder(id){const sel=node(id),out=node(id+'Path');if(sel&&out)out.textContent=state.projects.find(p=>p.id===sel.value)?.path||'';}
 area.addEventListener('change',e=>{if(e.target.id==='groupProject'||e.target.id==='applicationProject')showFolder(e.target.id);});
 async function pickFolder(id){const b=area.querySelector(`[data-pick-folder="${id}"]`);const label=b.textContent;b.textContent='请在弹出的窗口中选择文件夹…';b.disabled=true;
  try{const r=await api('/api/projects/pick',{method:'POST',body:'{}'});if(r.cancelled)return;state.projects=r.projects;
   const sel=node(id);sel.innerHTML=projectOptions(r.project.id);sel.value=r.project.id;sel.dispatchEvent(new Event('input',{bubbles:true}));showFolder(id);}
  catch(err){alert(err.message);}finally{b.textContent=label;b.disabled=false;}}
 area.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.pickFolder){pickFolder(b.dataset.pickFolder);return;}if(b.dataset.editGroup!==undefined)groupEditor(vm.catalog.groups[Number(b.dataset.editGroup)]);if(b.dataset.editApp!==undefined)applicationEditor(vm.catalog.apps[Number(b.dataset.editApp)]);});
 node('addGroup').onclick=()=>groupEditor(null);node('addApplication').onclick=()=>applicationEditor(null);
 window.addEventListener('bridge:edit-app',e=>{const a=app(e.detail);if(a){applicationEditor(a);area.scrollIntoView({behavior:'smooth',block:'start'});}});
 window.addEventListener('bridge:assistants-changed',()=>refreshCatalog());
 window.addEventListener('beforeunload',e=>{if(vm.groupDirty||vm.appDirty){e.preventDefault();e.returnValue='';}});
 refresh().then(refreshCatalog);setInterval(()=>{if(!vm.busy)refreshCatalog();},5000);
})();
