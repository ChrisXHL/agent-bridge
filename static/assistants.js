/* The setup UI only receives public progress. Secrets stay in private server state. */
(() => {
 const root=document.getElementById('assistantWorkspace');
 const model={jobs:[],loaded:false,loadError:false,busy:false,loading:false,requestId:null,showRemoved:false,pendingRemove:null,collaboration:null,policy:null};
 root.innerHTML=`<section id="assistantManagerEntry" class="assistant-manager-entry" aria-labelledby="assistantManagerTitle"></section>
 <section class="assistant-intro"><div><span class="assistant-eyebrow">让助手为你工作</span><h2>说说你想让它做什么</h2><p>机器人、连接和处理规则，一起配好。</p></div><div class="assistant-form-actions"><button class="btn" id="newDispatcher">创建任务管家</button><button class="btn primary" id="newAssistant">＋ 创建助手</button></div></section>
 <p id="assistantCollaborationPolicy"><span id="assistantCollaborationPolicyText">正在读取协作设置…</span> <button class="btn ghost" id="toggleCollaborationPolicy" disabled>切换</button></p>
 <form id="assistantForm" class="assistant-form hidden">
 <div class="field"><label for="assistantRequirements">你希望这个助手帮你做什么？</label><textarea class="input" id="assistantRequirements" rows="4" minlength="5" maxlength="2000" required placeholder="例如：群里发来资料后，整理成飞书文档，提取待办，再把链接发回群里。"></textarea></div>
 <fieldset class="assistant-audience"><legend>在哪里使用？</legend><label><input type="radio" name="assistantAudience" value="private" checked> 我自己私聊用</label><label><input type="radio" name="assistantAudience" value="group"> 在群里一起用</label></fieldset>
 <details class="assistant-options"><summary>更多选项</summary><div class="field"><label for="assistantProject">工作位置</label><select class="select" id="assistantProject"><option value="">自动创建独立工作目录</option></select><p class="config-help">默认独立保存这个助手的文件。需要处理已有项目时再选择。</p></div></details>
 <p class="assistant-explainer">点击后会根据用途准备助手，再请你用飞书扫码确认。基础文档、表格和文件能力会一起申请。</p>
 <div class="assistant-form-actions"><button class="btn primary" id="submitAssistant" type="submit">创建并配置</button><button class="btn ghost" id="closeAssistantForm" type="button">收起</button></div>
 </form><div id="assistantFeedback" class="inline-feedback hidden" role="status" aria-live="polite"></div>
 <dialog id="assistantRemoveDialog" class="assistant-remove-dialog" aria-labelledby="assistantRemoveTitle"><h3 id="assistantRemoveTitle">移除助手</h3><p id="assistantRemoveDescription"></p><p>将停止接收它的新任务。历史任务和文件保留，可随时恢复。飞书里的机器人仍会保留。</p><div class="assistant-form-actions"><button class="btn" id="cancelAssistantRemove" type="button">取消</button><button class="btn primary" id="confirmAssistantRemove" type="button">确认移除</button></div></dialog>
 <dialog id="assistantCollaborationDialog" class="assistant-remove-dialog" aria-labelledby="assistantCollaborationTitle"><h3 id="assistantCollaborationTitle">协作助手</h3><p id="assistantCollaborationDescription"></p><p id="assistantCollaborationHelp">主助手可把必要的任务资料交给所选助手，使用对方的用途、项目和已配置能力。结果交回主助手，由它统一回复。此设置对该助手接入的私聊和群任务长期生效。临时协作也可直接在飞书提出，由创建者确认本次调用。</p><div id="assistantCollaborators"></div><div id="assistantCollaborationFeedback" role="status"></div><div class="assistant-form-actions"><button class="btn" id="cancelAssistantCollaboration" type="button">取消</button><button class="btn primary" id="saveAssistantCollaboration" type="button">保存</button></div></dialog>
 <div id="assistantJobs" aria-live="polite" aria-relevant="additions text"></div>`;
 const el=id=>document.getElementById(id);
 function showProgress(id){const card=el('assistant-'+id);if(card){card.scrollIntoView({block:'start'});card.focus({preventScroll:true});}}
 const active=j=>!['ready','failed','needs_check','removed'].includes(j.phase);
 const labels={removed:'已移除',removing:'正在移除',restoring:'正在恢复',planning:'正在准备',registering:'正在准备扫码',waiting_scan:'等你扫码',connecting:'正在自动配置',waiting_group:'等你添加到群',ready:'已配置',failed:'需要继续配置',needs_check:'需要确认创建结果'};
 function feedback(text,error=false){el('assistantFeedback').textContent=text;el('assistantFeedback').className='inline-feedback'+(text?(error?' error':''):' hidden');}
 function validation(){
  el('submitAssistant').disabled=!model.loaded||model.busy||model.jobs.some(active)||el('assistantRequirements').value.trim().length<5;
  el('newAssistant').disabled=!model.loaded||model.busy||model.jobs.some(active);
  const dispatcher=model.jobs.find(j=>j.kind==='dispatcher');
  el('newDispatcher').textContent=dispatcher?(dispatcher.phase==='ready'?'打开任务管家':'查看任务管家'):'创建任务管家';
  el('newDispatcher').disabled=!model.loaded||model.busy||(!dispatcher&&model.jobs.some(active));
  if(el('enableAssistantManager'))el('enableAssistantManager').disabled=!model.loaded||model.busy||model.jobs.some(active);
 }
 function renderManager(){
  const manager=model.jobs.find(j=>j.kind==='manager');
  let title='一键创建助手管理员',description='以后在飞书私聊它，就能创建其他助手、查看进度和修改用途。',action='',hint='用你自己的飞书扫码确认，连接和管理规则自动配置。仅创建者可用。';
  if(!model.loaded){title=model.loadError?'暂时无法读取管理员状态':'正在检查助手管理员…';description=model.loadError?'重新检查后即可继续，已有进度会自动恢复。':'正在读取这台电脑上的配置。';hint='';action=model.loadError?'<button class="btn" id="checkAssistantManager">重新检查</button>':'<button class="btn" disabled>检查中…</button>';}
  else if(!manager){action=`<button class="btn primary" id="enableAssistantManager">${model.busy?'正在准备…':'创建助手管理员'}</button>`;if(model.jobs.some(active))hint='另一位助手正在创建。完成后即可创建管理员。';}
  else if(manager.phase==='ready'){title='助手管理员已就绪';description='在飞书里告诉它你想要什么助手，它会帮你创建并配置。';action=`<a class="btn primary" href="${esc(manager.open_url)}" target="_blank" rel="noopener noreferrer">打开助手管理员 ↗</a>`;hint='试着说：创建一个读书笔记助手，帮我提炼观点并生成飞书文档。';}
  else if(manager.phase==='removed'){title='恢复助手管理员';description='恢复后，继续在飞书创建和管理助手。';action=`<button class="btn primary" data-restore-assistant="${esc(manager.id)}">恢复助手管理员</button>`;hint='恢复原来的管理员和连接。';}
  else {title=manager.phase==='needs_check'?'核对管理员创建结果':'继续配置助手管理员';description=manager.message||'创建进度已保留，完成后就能在飞书使用。';action=manager.phase==='failed'?`<button class="btn primary" data-retry-assistant="${esc(manager.id)}">继续配置</button>`:`<button class="btn primary" data-manager-progress="${esc(manager.id)}">${manager.phase==='waiting_scan'?'查看二维码':manager.phase==='needs_check'?'查看恢复方式':'查看创建进度'}</button>`;hint='沿用这次创建记录，完成后自动连接。';}
  setHtmlIfChanged('assistantManagerEntry',`<div class="assistant-manager-copy"><span class="assistant-eyebrow">从飞书开始</span><h2 id="assistantManagerTitle">${esc(title)}</h2><p>${esc(description)}</p><p class="assistant-manager-hint">${esc(hint)}</p></div><div class="assistant-manager-action">${action}</div>`);
 }
 function render(){
  renderManager();
  const open=model.policy?.mode==='open';
  el('assistantCollaborationPolicyText').textContent=model.policy?(open?'自由协作已开启：助手可直接分工，不用协作码。':'逐次确认：未提前授权的协作会先询问创建者。'):'正在读取协作设置…';
  el('toggleCollaborationPolicy').textContent=open?'改为逐次确认':'开启自由协作';
  el('toggleCollaborationPolicy').disabled=!model.policy||model.busy;
  const removed=model.jobs.filter(j=>j.phase==='removed');
  const html=(removed.length?`<button class="btn ghost" data-toggle-removed="true" aria-expanded="${model.showRemoved}">${model.showRemoved?'收起':'查看'}已移除的助手（${removed.length}）</button>`:'')+model.jobs.filter(j=>j.phase!=='removed'||model.showRemoved).map(j=>{
   const expired=j.expires_at&&j.expires_at<=Date.now()/1000;
   let content='';
   if(j.phase==='waiting_scan') content=`<div class="assistant-scan">${j.qr_url&&!expired?`<img src="${esc(j.qr_url)}" alt="用飞书扫描以创建助手" width="280" height="280">`:''}<div><h4>${expired?'二维码已到期':'用飞书扫一扫'}</h4><p>${expired?'正在核对本次创建结果，请勿重复创建。':'在飞书里确认所属企业和创建，完成后这里会自动继续。'}</p>${!expired&&j.verification_url?`<a class="btn" href="${esc(j.verification_url)}" target="_blank" rel="noopener noreferrer">打开确认页面</a>`:''}<p class="config-help">${expired?'':'有效至 '+new Date(j.expires_at*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}</p></div></div>`;
   if(j.phase==='waiting_group') content=j.auto_group_activation?`<div class="assistant-group-step"><p>把 <strong>${esc(j.name)}</strong> 加进普通群，再由创建者 @它说一句要做什么，就会自动接好并开始处理。</p><p class="config-help">它会找本群已接入的助手分工。其他成员之后也能 @它派活。</p></div>`:`<div class="assistant-group-step"><p>1. 在飞书目标群的“群机器人”中添加 <strong>${esc(j.name)}</strong>。</p><p>2. ${j.pairing_expired?'配对文字已过期，请刷新后发送。':'把机器人拉进群后，由扫码创建者在群里 @它说第一句需求即可自动接入；也可以发送下面的配对码。'}</p>${j.pairing_text?`<div class="assistant-pair-text">${esc(j.pairing_text)}</div><button class="btn" data-copy-pair="${esc(j.id)}">复制配对文字</button>`:''}<button class="btn ghost" data-retry-assistant="${esc(j.id)}">刷新配对码</button><p class="config-help">识别后自动完成。全群成员即可 @它使用。</p></div>`;
   if(j.phase==='failed')content=`<button class="btn" data-retry-assistant="${esc(j.id)}">继续配置</button>`;
   if(j.phase==='needs_check')content=`<div class="assistant-recovery"><p>如果飞书已经创建成功，请使用下方“接入已有机器人”，保留同一个机器人。</p><button class="btn" data-restart-assistant="${esc(j.id)}">确认未创建，重新扫码</button></div>`;
   if(j.phase==='ready'){
    const t=j.latest_task;
    const detail=j.kind==='manager'?'仅创建者私聊可用。试着说：创建一个资料整理助手，私聊使用。':t?(t.status==='已完成'&&t.replied?'已完成任务并回复飞书':t.status==='已完成'?'任务已完成，正在回复飞书':`最近任务：${t.status}`):'发第一条消息开始使用；收到后会在这里显示执行结果。';
    content=`<div class="assistant-ready-actions"><a class="btn primary" href="${esc(j.open_url)}" target="_blank" rel="noopener noreferrer">打开飞书使用</a><button class="btn ghost" data-assistant-settings="${esc(j.app_ref)}">高级设置</button>${j.kind!=='manager'?`<button class="btn ghost" data-collaborate-assistant="${esc(j.id)}">${model.policy?.mode==='open'?'可协作助手':'协作助手'}${model.policy?.mode!=='open'&&j.collaboration_count?`（${j.collaboration_count}）`:''}</button>`:''}<button class="btn ghost" data-remove-assistant="${esc(j.id)}">移除助手</button></div><p class="assistant-verification">${esc(detail)}</p>`;
   }
   if(j.phase==='removed')content=`<button class="btn" data-restore-assistant="${esc(j.id)}">恢复助手</button>`;
   const step=['planning','registering'].includes(j.phase)?0:j.phase==='waiting_scan'?1:['connecting','failed'].includes(j.phase)?2:3;
   return `<article class="assistant-card" id="assistant-${esc(j.id)}" tabindex="-1"><div class="assistant-card-heading"><div><h3>${esc(j.name||'正在准备你的助手')}</h3><p>${esc(j.summary||j.requirements)}</p></div><span class="status ${j.phase==='ready'?'done':['failed','needs_check'].includes(j.phase)?'need':'wait'}">${esc(labels[j.phase]||j.phase)}</span></div>${!['ready','removed'].includes(j.phase)?`<ol class="assistant-progress" aria-label="创建进度">${['理解用途','扫码确认','自动配置','开始使用'].map((x,i)=>`<li class="${i<step?'complete':i===step?'current':''}">${x}</li>`).join('')}</ol>`:''}<p class="assistant-stage">${esc(j.phase==='ready'&&j.kind==='manager'?'已连接。直接在飞书私聊它，创建助手、查询进度或修改用途。':j.phase==='ready'&&j.latest_task?'助手已接入，后续直接在飞书发消息即可。':j.message)}</p>${content}${j.limitations&&j.phase!=='removed'?`<details class="assistant-limitations"><summary>这项用途还需要什么</summary><p>${esc(j.limitations)}</p></details>`:''}</article>`;
  }).join('');
  setHtmlIfChanged('assistantJobs',html);validation();
 }
 async function refreshJobs(){
  if(model.loading)return;model.loading=true;
  try{const [d,policy]=await Promise.all([api('/api/feishu/onboarding'),api('/api/feishu/collaboration-policy')]);model.policy=policy;model.jobs=d.assistants;model.loaded=true;model.loadError=false;
   if(model.requestId&&model.jobs.some(j=>j.request_id===model.requestId)){
    sessionStorage.removeItem('assistantPending');sessionStorage.removeItem('assistantDraft');
    el('assistantRequirements').value='';el('assistantForm').classList.add('hidden');model.requestId=null;feedback('');
   }
   render();if(el('assistantFeedback').dataset.network){feedback('');delete el('assistantFeedback').dataset.network;}}
  catch(e){model.loadError=true;renderManager();validation();feedback(e.message+'，恢复连接后会继续显示进度。',true);el('assistantFeedback').dataset.network='true';}
  finally{model.loading=false;}
 }
 function openForm(){
  el('assistantForm').classList.remove('hidden');
  const chosen=el('assistantProject').value;
  el('assistantProject').innerHTML='<option value="">自动创建独立工作目录</option>'+state.projects.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');el('assistantProject').value=chosen;
  el('assistantRequirements').focus();validation();
 }
 el('newAssistant').onclick=openForm;
 el('toggleCollaborationPolicy').onclick=async()=>{
  if(model.busy||!model.policy)return;
  model.busy=true;el('toggleCollaborationPolicy').disabled=true;
  try{model.policy=await api('/api/feishu/collaboration-policy',{method:'POST',body:JSON.stringify({mode:model.policy.mode==='open'?'confirm':'open'})});feedback(model.policy.mode==='open'?'自由协作已开启。已接入并启用的助手可分享任务资料、使用各自已有能力完成分工；任务管家只派给本群助手。':'已恢复逐次确认。已有明确授权继续保留。');}
  catch(e){feedback(e.message,true);}finally{model.busy=false;render();}
 };
 el('newDispatcher').onclick=async()=>{
  const old=model.jobs.find(j=>j.kind==='dispatcher');
  if(old){if(old.phase==='ready'&&old.open_url)window.open(old.open_url,'_blank','noopener,noreferrer');else{if(old.phase==='removed'){model.showRemoved=true;render();}showProgress(old.id);}return;}
  if(!model.loaded||model.busy||model.jobs.some(active))return;
  model.busy=true;validation();feedback('正在准备任务管家的扫码入口…');
  try{const j=await api('/api/feishu/dispatcher',{method:'POST',body:'{}'});feedback('');await refreshJobs();showProgress(j.id);}
  catch(e){feedback(e.message,true);}finally{model.busy=false;validation();}
 };
 el('closeAssistantForm').onclick=()=>el('assistantForm').classList.add('hidden');
 el('assistantRequirements').value=sessionStorage.getItem('assistantDraft')||'';
 el('assistantRequirements').oninput=()=>{sessionStorage.setItem('assistantDraft',el('assistantRequirements').value);if(!model.busy)model.requestId=null;validation();};
 el('assistantForm').onchange=()=>{if(!model.busy)model.requestId=null;};
 el('assistantForm').onsubmit=async event=>{
  event.preventDefault();if(model.busy||el('submitAssistant').disabled)return;
  model.busy=true;validation();feedback('正在开始创建…');
  model.requestId=model.requestId||crypto.randomUUID();
  const payload={request_id:model.requestId,requirements:el('assistantRequirements').value.trim(),audience:el('assistantForm').querySelector('input[name="assistantAudience"]:checked').value,project_id:el('assistantProject').value};
  sessionStorage.setItem('assistantPending',JSON.stringify(payload));
  try{await api('/api/feishu/onboarding',{method:'POST',body:JSON.stringify(payload)});sessionStorage.removeItem('assistantPending');sessionStorage.removeItem('assistantDraft');el('assistantRequirements').value='';el('assistantForm').classList.add('hidden');model.requestId=null;feedback('');await refreshJobs();}
  catch(e){feedback(e.message,true);}
  finally{model.busy=false;validation();}
 };
 root.addEventListener('click',async event=>{
  const b=event.target.closest('button');if(!b)return;
  if(b.id==='checkAssistantManager'){await refreshJobs();return;}
  if(b.dataset.managerProgress){showProgress(b.dataset.managerProgress);return;}
  if(b.dataset.collaborateAssistant){
   if(model.busy)return;model.busy=true;b.disabled=true;
   try{const d=await api('/api/feishu/collaboration/'+b.dataset.collaborateAssistant);model.collaboration=d;
    const open=d.policy?.mode==='open';
    el('assistantCollaborationDescription').textContent=open?`“${d.source_name}”已开启自由协作。`:`允许“${d.source_name}”调用哪些助手？`;
    el('assistantCollaborationHelp').textContent=open?'已接入且启用的助手可以直接分工，传递任务需要的资料，使用各自已有的能力。任务管家只调度本群助手。如需逐次确认，可在助手页面切换。':'选择长期允许调用的助手；未选择的助手仍可通过创建者确认本次协作。';
    el('saveAssistantCollaboration').hidden=open;
    el('cancelAssistantCollaboration').textContent=open?'关闭':'取消';
    el('assistantCollaborators').innerHTML=d.candidates.length?d.candidates.map(x=>`<label class="assistant-collaborator"><input type="checkbox" value="${esc(x.id)}" ${(open?x.available:d.targets.includes(x.id))?'checked':''} ${open||!x.available?'disabled':''}><span><strong>${esc(x.name)}${x.available?'':'（暂不可用）'}</strong><small>${esc(x.summary)}</small></span></label>`).join(''):'<p>还没有其他可用的业务助手。先创建另一位助手，再来选择。</p>';
    el('assistantCollaborationFeedback').textContent='';el('assistantCollaborationDialog').showModal();
   }catch(e){feedback(e.message,true);}finally{model.busy=false;b.disabled=false;validation();}return;
  }
  if(b.id==='cancelAssistantCollaboration'){if(!model.busy)el('assistantCollaborationDialog').close();return;}
  if(b.id==='saveAssistantCollaboration'){
   if(model.busy||!model.collaboration)return;model.busy=true;b.disabled=true;
   const d=model.collaboration,targets=Array.from(el('assistantCollaborators').querySelectorAll('input:checked:not(:disabled)'),x=>x.value);
   try{await api('/api/feishu/collaboration/'+d.source_id,{method:'POST',body:JSON.stringify({targets,revision:d.revision})});el('assistantCollaborationDialog').close();feedback(targets.length?'协作助手已保存。下一条新任务会自动获得调用入口，无需修改 Prompt。':'已取消长期协作授权。临时协作仍需在飞书确认。');await refreshJobs();}
   catch(e){el('assistantCollaborationFeedback').textContent=e.message;}finally{model.busy=false;b.disabled=false;validation();}return;
  }
  if(b.dataset.toggleRemoved){model.showRemoved=!model.showRemoved;render();return;}
  if(b.id==='cancelAssistantRemove'){el('assistantRemoveDialog').close();model.pendingRemove=null;return;}
  if(b.dataset.removeAssistant){
   const j=model.jobs.find(x=>x.id===b.dataset.removeAssistant);if(model.busy||!j)return;
   model.pendingRemove=j.id;el('assistantRemoveDescription').textContent=`从 Bridge 移除“${j.name}”？${j.kind==='manager'?'移除管理员后，可从此页面恢复管理入口。':''}`;
   el('assistantRemoveDialog').showModal();el('cancelAssistantRemove').focus();return;
  }
  if(b.id==='confirmAssistantRemove'||b.dataset.restoreAssistant){
   if(model.busy)return;
   const id=b.dataset.restoreAssistant||model.pendingRemove;if(!id)return;
   el('assistantRemoveDialog').close();model.pendingRemove=null;
   const action=b.dataset.restoreAssistant?'restore':'remove';
   model.busy=true;b.disabled=true;validation();feedback(action==='remove'?'正在移除…':'正在恢复…');
   try{await api(`/api/feishu/onboarding/${id}/${action}`,{method:'POST',body:'{}'});feedback(action==='remove'?'已从 Bridge 移除。需要时可展开“已移除的助手”恢复。':'助手已恢复。');await refreshJobs();window.dispatchEvent(new CustomEvent('bridge:assistants-changed'));}
   catch(e){feedback(e.message,true);}finally{model.busy=false;b.disabled=false;validation();}return;
  }
  if(b.id==='enableAssistantManager'){
   if(!model.loaded||model.busy||model.jobs.some(active))return;model.busy=true;renderManager();validation();feedback('正在准备助手管理员的官方确认入口…');
   try{const j=await api('/api/feishu/manager',{method:'POST',body:'{}'});feedback('');await refreshJobs();if(j.phase!=='ready')showProgress(j.id);}
   catch(e){feedback(e.message,true);}finally{model.busy=false;renderManager();validation();}return;
  }
  if(b.dataset.assistantSettings){window.dispatchEvent(new CustomEvent('bridge:edit-app',{detail:b.dataset.assistantSettings}));return;}
  if(b.dataset.copyPair){const j=model.jobs.find(x=>x.id===b.dataset.copyPair);try{await navigator.clipboard.writeText(j.pairing_text);feedback('配对文字已复制。在飞书中请用真正的 @ 提及选中机器人。');}catch{feedback('请选中配对文字复制。');}return;}
  const id=b.dataset.retryAssistant||b.dataset.restartAssistant;if(!id||model.busy)return;
  if(b.dataset.restartAssistant&&!confirm('请确认飞书中没有创建成功。若已有机器人，重新扫码会产生重复应用。确认尚未创建？'))return;
  model.busy=true;b.disabled=true;validation();
  try{await api(`/api/feishu/onboarding/${id}/retry`,{method:'POST',body:JSON.stringify(b.dataset.restartAssistant?{confirmed_not_created:true}:{})});feedback('');await refreshJobs();}
  catch(e){feedback(e.message,true);}finally{model.busy=false;b.disabled=false;validation();}
 });
 // If a POST response was lost, reuse its idempotency key instead of creating a duplicate.
 const pending=sessionStorage.getItem('assistantPending');
 if(pending){try{const p=JSON.parse(pending);model.requestId=p.request_id;el('assistantRequirements').value=p.requirements;root.querySelector(`input[value="${p.audience==='group'?'group':'private'}"]`).checked=true;openForm();el('assistantProject').value=p.project_id;feedback('上次提交结果尚未确认。再次点击会查询同一次创建，不会重复生成。');}catch{sessionStorage.removeItem('assistantPending');}}
 renderManager();validation();refreshJobs();setInterval(()=>{if(state.page==='feishu'&&!model.busy)refreshJobs();},2500);
})();
