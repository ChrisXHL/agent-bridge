const $ = id => document.getElementById(id);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const savedPage=sessionStorage.getItem('bridgePage');
const state = {page:['tasks','connections','feishu'].includes(savedPage)?savedPage:'tasks', projects:[], connections:[], tasks:[], feishu:null, feishuDirty:false, promptDirty:false, quickDirty:false, newConnection:null, history:false, drawer:null, pending:{setup:false,check:false,pair:false,advanced:false,connection:false,approval:false}};
const statusClass = s => ({'等待 Codex':'wait','执行中':'run','流程进行中':'run','等待审核':'need','流程受阻':'need','需要你处理':'need','已完成':'done','失败':'fail','已停止':'fail','待验证':'wait','可用':'done','已停用':'fail'})[s] || 'wait';
const badge = s => `<span class="status ${statusClass(s)}"><span class="s-dot"></span>${esc(s)}</span>`;
const timeText = ts => new Date(ts*1000).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'});
async function api(path, options={}) {
 let response;
 try{response=await fetch(path,{...options,headers:{'Content-Type':'application/json',...(options.headers||{})}});}
 catch{throw new Error('无法连接本机 Bridge 服务');}
 let result;
 try{result=await response.json();}catch{throw new Error('Bridge 返回了无效响应');}
 if(!response.ok)throw new Error(result.error?.message||'操作失败');
 return result;
}
function toast(message) { $('toast').textContent=message; $('toast').classList.add('show'); clearTimeout(window.toastTimer); window.toastTimer=setTimeout(()=>$('toast').classList.remove('show'),2500); }
function feedback(id, message='', tone='info') { const element=$(id); element.textContent=message; element.className='inline-feedback'+(message?' '+tone:' hidden'); }
function stepState(id, label, tone='') { const element=$(id); element.textContent=label; element.className='step-state'+(tone?' '+tone:''); }
function switchPage(page) { state.page=page; sessionStorage.setItem('bridgePage',page); for(const name of ['tasks','connections','feishu']) { $(name+'Page').classList.toggle('hidden',page!==name); $('nav'+name[0].toUpperCase()+name.slice(1)).classList.toggle('active',page===name); } $('pageTitle').textContent=({tasks:'任务',connections:'连接',feishu:'飞书',})[page]; $('pageSubtitle').textContent=({tasks:'外部系统交给 Codex 的任务。',connections:'管理谁可以给哪些 Codex 项目派任务。',feishu:'在这台 Mac 上直接接收飞书消息。'})[page]; $('primaryAction').classList.toggle('hidden',page!=='connections'); if(page==='feishu') renderFeishu();  }
function taskStatus(t){return t.display_status||t.status;}
function taskRow(t) { return `<div class="task" data-task="${esc(t.id)}"><div class="task-main"><div class="meta-row"><span class="source-chip">${esc(t.source)}</span><span class="arrow">→</span><span>${esc(t.project_name)}</span></div><div class="task-title">${esc(t.archive_name||t.title)}</div></div>${badge(taskStatus(t))}<div class="time">${timeText(t.updated_at)}</div></div>`; }
function renderTasks() {
 const tasks=state.tasks; const attention=tasks.filter(t=>['需要你处理','流程受阻','等待审核'].includes(taskStatus(t))); const others=tasks.filter(t=>!attention.includes(t));
 const paired=(state.feishu?.allowed_open_ids||[]).length>0;
 $('firstUseTitle').textContent=paired?'等待第一条飞书任务':state.feishu?.has_secret?'完成飞书配对':'还没有外部任务';
 $('firstUseDescription').textContent=paired?'向已配对的飞书机器人发送一条普通消息，任务会显示在这里。':state.feishu?.has_secret?'应用已连接；完成机器人设置和身份配对后即可发送任务。':state.connections.length?'连接已创建；从任务来源发送一条消息，验证它能否进入 Codex。':'连接飞书机器人后，可以直接在飞书里给 Codex 派任务。';
 $('firstConnect').textContent=paired||state.feishu?.has_secret?'查看飞书接入':'连接飞书机器人';
 $('firstUseEmpty').classList.toggle('hidden',tasks.length>0); $('taskContent').classList.toggle('hidden',tasks.length===0);
 $('attentionSection').classList.toggle('hidden',attention.length===0); $('attentionCount').textContent=attention.length; setHtmlIfChanged('attentionList',attention.map(taskRow).join(''));
 $('recentCount').textContent=Math.min(others.length,8); setHtmlIfChanged('recentList',others.slice(0,8).map(taskRow).join(''));
 $('historyToggle').classList.toggle('hidden',others.length<=8); $('historyList').classList.toggle('hidden',!state.history); setHtmlIfChanged('historyItems',others.slice(8).map(taskRow).join('')); $('historyChevron').textContent=state.history?'⌄':'›';
}
function renderConnections() { setHtmlIfChanged('connectionsGrid',state.connections.map(c=>`<div class="conn-card"><div class="conn-head"><div><div class="conn-title">${esc(c.source)} → ${esc(c.project_name)}</div></div>${badge(c.status)}</div><div class="conn-actions"><button class="btn" data-copy="${esc(c.id)}">复制连接信息</button><button class="btn ghost" data-settings="${esc(c.id)}">设置</button></div></div>`).join('') || '<div class="empty-card">还没有连接。点击右上角创建连接。</div>'); }
let refreshPromise=null;
async function refresh(force=false) {
 if(refreshPromise){ if(!force)return refreshPromise; await refreshPromise; }
 const request=(async()=>{
  try {
   const [projects,connections,tasks,feishu]=await Promise.all([api('/api/projects'),api('/api/connections'),api('/api/tasks'),api('/api/feishu')]);
   state.projects=projects; state.connections=connections; state.tasks=tasks; state.feishu=feishu;
   renderTasks(); renderConnections(); if(state.page==='feishu')renderFeishu(); 
   $('serviceNotice').classList.add('hidden');
   if(state.drawer?.kind==='task')await showTask(state.drawer.id,false);
   return true;
  } catch(e) {
   $('serviceNoticeText').textContent='面板数据暂时无法更新：'+e.message+'。当前内容可能不是最新的。';
   $('serviceNotice').classList.remove('hidden');
   return false;
  }
 })();
 refreshPromise=request;
 try{return await request;}finally{if(refreshPromise===request)refreshPromise=null;}
}
const renderedHtml=new WeakMap();
function setHtmlIfChanged(id, html) {
 const element=$(id);
 if(renderedHtml.get(element)!==html){ element.innerHTML=html; renderedHtml.set(element,html); }
}
function renderQuickFeishu() {
 const f=state.feishu; if(!f)return;
 const existing=state.connections.find(c=>c.id===f.connection_id);
 const pending=state.pending;
 const busy=pending.setup||pending.check||pending.pair;
 const active=Boolean(f.pairing_code&&Date.now()/1000<f.pairing_expires_at);
 const expired=Boolean(f.pairing_code&&!active);
 const candidate=f.pairing_candidate;
 if(!state.quickDirty){
  if($('quickAppId').value!==(f.app_id||''))$('quickAppId').value=f.app_id||'';
  $('quickSecret').value='';
  setHtmlIfChanged('quickProject','<option value="">'+(state.projects.length?'请选择 Codex 项目':'未找到 Codex 项目')+'</option>'+state.projects.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}</option>`).join(''));
  $('quickProject').value=existing?.project_id||'';
 }
 $('quickSecret').placeholder=f.has_secret?'已验证并保存，留空即可':'从飞书开发者后台复制';
 $('quickAppId').disabled=pending.setup;
 $('quickSecret').disabled=pending.setup;
 $('quickProject').disabled=pending.setup;
 $('quickSetup').disabled=busy;
 $('quickSetup').textContent=pending.setup?'正在验证…':'验证并保存';
 const appState=pending.setup?'验证中':state.quickDirty?'待验证':f.has_secret&&existing?'已验证':'待配置';
 stepState('appStepState',appState,appState==='已验证'?'done':pending.setup||state.quickDirty?'active':'');
 const status=state.quickDirty?'当前输入尚未验证或保存。':!state.projects.length?'未找到 Codex 项目，请先在 Codex 创建或添加一个项目。':f.has_secret?`✓ 已验证应用 ${esc(f.app_id)}；${existing?'已连接 Codex 项目 '+esc(existing.project_name):'请选择项目'}`:'等待填写应用凭据并选择项目。';
 setHtmlIfChanged('quickStatus',status);
 $('quickCheck').disabled=!f.has_secret||state.quickDirty||busy;
 $('quickCheck').textContent=pending.check?'检查中…':'检查机器人状态';
 $('quickBotStatus').textContent=state.quickDirty?'先验证并保存当前输入，再检查机器人状态。':f.bot_ready?`✓ 机器人 ${f.bot_name||f.bot_open_id} 已识别；接收事件仍需通过实际配对消息验证。`:f.has_secret?'尚未确认机器人已启用。完成飞书后台设置后点击“检查机器人状态”。':'先验证并保存应用凭据。';
 stepState('botStepState',pending.check?'检查中':state.quickDirty?'等待保存':f.bot_ready?'已识别机器人':'待检查',!state.quickDirty&&f.bot_ready?'done':pending.check?'active':'');
 $('quickPair').disabled=!f.bot_ready||!f.bot_open_id||!f.connection_id||state.quickDirty||busy;
 $('quickPair').textContent=pending.pair?'处理中…':active||expired?'重新生成配对码':'生成配对码并启动接收';
 const pairState=pending.pair?'处理中':state.quickDirty?'等待保存':candidate&&active?'待确认':active?'等待消息':expired?'已过期':(f.allowed_open_ids||[]).length?'已绑定':'待配对';
 stepState('pairStepState',pairState,pairState==='已绑定'?'done':pairState==='已过期'?'error':active||pending.pair?'active':'');
 if(candidate&&active){
  setHtmlIfChanged('quickPairStatus',`✓ 收到配对消息：用户 <code>${esc(candidate.sender_id)}</code>${candidate.chat_type==='group'?`，群 <code>${esc(candidate.chat_id)}</code>`:'，私聊'}。<br><button class="btn primary" id="quickConfirmPair" type="button" style="margin-top:8px" ${pending.pair?'disabled':''}>${pending.pair?'绑定中…':'确认绑定此身份'}</button>`);
 }else if(active){
  const minutes=Math.max(1,Math.ceil((f.pairing_expires_at-Date.now()/1000)/60));
  setHtmlIfChanged('quickPairStatus',`向机器人发送：<strong style="font-size:17px;letter-spacing:.08em;color:var(--text)">${esc(f.pairing_code)}</strong><br>等待配对消息，约 ${minutes} 分钟后过期。${f.runtime?'当前状态：'+esc(f.runtime):''}`);
 }else if(expired){
  setHtmlIfChanged('quickPairStatus','配对码已过期。点击“重新生成配对码”后，再向机器人发送新码。');
 }else if((f.allowed_open_ids||[]).length){
  setHtmlIfChanged('quickPairStatus',`✓ 已绑定 ${(f.allowed_open_ids||[]).length} 位用户。现在发送普通飞书消息，在“任务”页查看 Codex 是否收到。`);
 }else{
  setHtmlIfChanged('quickPairStatus','等待机器人就绪后生成配对码。');
 }
}
function renderFeishu() {
 const f=state.feishu; if(!f)return;
 const runtime=$('feishuRuntime');
 runtime.textContent=f.runtime||'未启动';
 runtime.className='status '+(f.runtime==='已停用'||f.runtime==='配置错误'?'fail':'wait');
 renderQuickFeishu();
 $('advancedSave').textContent=state.pending.advanced?'保存中…':'保存飞书设置';
 if(!state.promptDirty)$('feishuPrompt').value=f.prompt_template||'';
 if(state.feishuDirty){renderFeishuPromptPreview();return;}
 if($('feishuAppId').value!==(f.app_id||''))$('feishuAppId').value=f.app_id||'';
 $('feishuSecret').value='';
 $('feishuSecretHint').textContent=f.has_secret?'已有密钥，留空则保留。':'尚未保存密钥。';
 setHtmlIfChanged('feishuConnection','<option value="">选择连接</option>'+state.connections.map(c=>`<option value="${esc(c.id)}">${esc(c.source)} → ${esc(c.project_name)}</option>`).join(''));
 $('feishuConnection').value=f.connection_id||'';
 $('feishuUsers').value=(f.allowed_open_ids||[]).join('\n');
 $('feishuChats').value=(f.allowed_chat_ids||[]).join('\n');
 $('feishuBotId').value=f.bot_open_id||'';
 if(!state.promptDirty)$('feishuPrompt').value=f.prompt_template||'';
 $('feishuEnabled').checked=!!f.enabled;
 renderFeishuPromptPreview();
}
async function quickSetup(){
 if(state.pending.setup)return;
 const body={app_id:$('quickAppId').value.trim(),app_secret:$('quickSecret').value.trim(),project_id:$('quickProject').value};
 const sameApp=body.app_id===state.feishu?.app_id;
 if(!body.app_id){feedback('quickSetupFeedback','请填写 App ID。','error');$('quickAppId').focus();return;}
 if(!body.app_secret&&!(sameApp&&state.feishu?.has_secret)){feedback('quickSetupFeedback','请填写这个应用的 App Secret。','error');$('quickSecret').focus();return;}
 if(!body.project_id){feedback('quickSetupFeedback',state.projects.length?'请选择 Codex 项目。':'先在 Codex 创建或添加一个项目。','error');$('quickProject').focus();return;}
 state.pending.setup=true;feedback('quickSetupFeedback','正在向飞书验证应用，并创建 Codex 连接…');renderQuickFeishu();
 try{
  state.feishu=await api('/api/feishu/setup',{method:'POST',body:JSON.stringify(body)});
  state.quickDirty=false;state.feishuDirty=false;
  const refreshed=await refresh(true);
  feedback('quickSetupFeedback',refreshed?'应用已验证，Codex 连接已创建。':'应用已保存，但面板数据暂时无法刷新，请点击顶部重试。',refreshed?'success':'error');
 }catch(e){feedback('quickSetupFeedback',e.message,'error');}
 finally{state.pending.setup=false;renderQuickFeishu();}
}
async function quickCheck(){
 if(state.pending.check||state.quickDirty)return;
 state.pending.check=true;feedback('quickCheckFeedback','正在查询飞书机器人状态…');renderQuickFeishu();
 try{
  state.feishu=await api('/api/feishu/check',{method:'POST',body:'{}'});
  renderFeishu();
  feedback('quickCheckFeedback',state.feishu.bot_ready?'已识别机器人。继续配对，以验证消息事件是否能到达本机。':'凭据有效，但机器人尚未启用或发布。完成飞书后台设置后再检查。',state.feishu.bot_ready?'success':'info');
 }catch(e){feedback('quickCheckFeedback',e.message,'error');}
 finally{state.pending.check=false;renderQuickFeishu();}
}
async function quickPair(action){
 if(state.pending.pair||state.quickDirty)return;
 state.pending.pair=true;feedback('quickPairFeedback',action==='start'?'正在生成配对码并启动接收…':'正在绑定此飞书身份…');renderQuickFeishu();
 try{
  state.feishu=await api('/api/feishu/pairing',{method:'POST',body:JSON.stringify({action})});
  renderFeishu();
  feedback('quickPairFeedback',action==='start'?'配对码已生成。请在飞书里发送上方新码。':'飞书身份已绑定。发送一条普通消息来验证任务投递。','success');
 }catch(e){feedback('quickPairFeedback',e.message,'error');}
 finally{state.pending.pair=false;renderQuickFeishu();}
}
function renderFeishuPromptPreview() {
 const template=$('feishuPrompt').value, sample=$('feishuSample').value, preview=$('feishuPromptPreview');
 let error='';
 if(template.split('{{message}}').length!==2)error='模板需恰好包含一次 {{message}}';
 else if(!template.trim()||template.length>8000)error='模板需为 1 到 8000 字';
 const result=error?'':template.replace('{{message}}',sample);
 if(!error){
  const bytes=new TextEncoder().encode(JSON.stringify({message:result,request_id:'fs_'+'x'.repeat(48),task_id:'x'.repeat(100)})).length;
  if(result.length>12000||bytes>16384)error='合成后超过 Bridge 长度限制，请缩短模板或消息。';
 }
 preview.textContent=error||result;
 preview.classList.toggle('preview-error',!!error);
 $('feishuPrompt').setAttribute('aria-invalid',error?'true':'false');
 $('advancedSave').disabled=state.pending.advanced;
 $('savePrompt').disabled=!!error||!!state.pending.prompt;
 return !error;
}
const idLines = value => [...new Set(value.split(/\r?\n|,/).map(x=>x.trim()).filter(Boolean))];
async function saveFeishu(e) {
 e.preventDefault();
 if(state.pending.advanced)return;
 const body={app_id:$('feishuAppId').value.trim(),connection_id:$('feishuConnection').value,allowed_open_ids:idLines($('feishuUsers').value),allowed_chat_ids:idLines($('feishuChats').value),bot_open_id:$('feishuBotId').value.trim(),enabled:$('feishuEnabled').checked};
 if($('feishuSecret').value)body.app_secret=$('feishuSecret').value;
 state.pending.advanced=true;feedback('advancedFeedback','正在保存飞书设置…');renderFeishu();
 try{
  state.feishu=await api('/api/feishu',{method:'PATCH',body:JSON.stringify(body)});
  state.feishuDirty=false;renderFeishu();
  feedback('advancedFeedback','飞书设置已保存。','success');
 }catch(err){feedback('advancedFeedback',err.message,'error');}
 finally{state.pending.advanced=false;renderFeishuPromptPreview();$('advancedSave').textContent='保存飞书设置';}
}
function openOverlay(kind, heading, html) { if(state.drawer?.kind!==kind)feedback('drawerFeedback'); state.drawer={kind,id:state.drawer?.id}; $('drawerHeading').textContent=heading; setHtmlIfChanged('drawerBody',html); $('overlay').classList.add('show'); $('drawer').classList.add('show'); }
function closeDrawer(){ $('drawer').classList.remove('show'); state.drawer=null; if(!$('modal').classList.contains('show')) $('overlay').classList.remove('show'); }
async function showTask(id,focus=true) {
 if(focus)feedback('drawerFeedback');
 try {
  const t=await api('/api/tasks/'+encodeURIComponent(id));
  if(!focus && state.drawer?.id!==id)return;
  const messages=t.messages.map(m=>`<div class="result-box" style="white-space:pre-wrap;margin:10px 0"><small>${timeText(m.created_at)}</small><p>${esc(m.content)}</p></div>`).join('');
  let action='';
  if(!t.archived && t.status==='需要你处理' && t.thread_id)action=`<a class="btn primary" href="codex://threads/${encodeURIComponent(t.thread_id)}">去 Codex 处理</a>`;
  if(!t.archived && t.error_code==='project_unavailable')action=`<button class="btn primary" data-repath="${esc(t.connection_id)}">重新选择项目</button>`;
  if(!t.archived && t.approval)action+=`<div class="cred"><p>${esc(t.approval.detail)}</p><button class="btn primary" data-approval="${esc(t.approval.id)}" data-decision="accept" ${state.pending.approval?'disabled':''}>允许此操作</button> <button class="btn" data-approval="${esc(t.approval.id)}" data-decision="decline" ${state.pending.approval?'disabled':''}>拒绝</button></div>`;
  const html=`<h2 class="detail-title">${esc(t.archive_name||t.title)}</h2><div class="detail-grid"><div class="detail-item"><label>状态</label><strong>${esc(taskStatus(t))}</strong></div><div class="detail-item"><label>时间</label><strong>${timeText(t.created_at)}</strong></div></div>${t.archived?'<p class="config-help">这是改版前的历史记录，仅供查看。新任务请从飞书发送一条新消息，将按当前 Prompt 执行。</p>':''}<section class="detail-section"><h3>任务回复</h3><div class="result-box" style="white-space:pre-wrap">${esc(t.result||'尚无回复。')}</div><div style="margin-top:12px">${action}</div></section><details class="detail-section"><summary>任务消息记录</summary>${messages}</details>`;
  openOverlay('task','任务详情',html);state.drawer={kind:'task',id};
 }catch(e){toast(e.message);}
}
function connectionText(c) { return `请通过这个连接给我的 Codex 发送任务：\n连接：${c.source} → ${c.project_name}\n接口：${c.endpoint}\n密钥：${c.api_key}\n\n新任务：POST 接口，Authorization: Bearer <密钥>，Content-Type: application/json，JSON: {"message":"任务内容","request_id":"唯一请求编号"}。\n继续同一个任务：在 JSON 中加上返回的 task_id；每条新消息使用新的 request_id。\n查询状态：GET ${c.endpoint}/<task_id>，使用同一密钥。`; }
function legacyCopy(text) {
 const field=document.createElement('textarea');
 field.value=text;
 field.setAttribute('readonly','');
 field.style.cssText='position:fixed;left:-9999px;top:0;opacity:0';
 document.body.appendChild(field);
 field.focus(); field.select();
 try { return typeof document.execCommand==='function' && document.execCommand('copy'); }
 catch { return false; }
 finally { field.remove(); }
}
async function writeText(text) {
 if(navigator.clipboard?.writeText) {
  try { await navigator.clipboard.writeText(text); return true; }
  catch { /* Some browsers expose the API but deny this origin. */ }
 }
 return legacyCopy(text);
}
function showManualCopy(text) {
 openOverlay('copy','复制连接信息','<p style="font-size:13px;line-height:1.6;margin:0 0 12px">浏览器阻止了自动复制。下方内容已选中，请按 ⌘C（或 Ctrl+C）复制。</p><textarea id="manualCopyText" class="input" readonly aria-label="连接信息" style="height:280px;resize:vertical;line-height:1.5"></textarea>');
 const field=$('manualCopyText');
 field.value=text;
 if($('modal').classList.contains('show')) closeModal();
 field.focus(); field.select();
}
async function copyConnection(id) {
 try {
  const c=await api('/api/connections/'+encodeURIComponent(id));
  const text=connectionText(c);
  if(await writeText(text)) toast('连接信息已复制');
  else showManualCopy(text);
 } catch(e) { toast(e.message); }
}
async function showSettings(id, revealRepair=false) { feedback('drawerFeedback'); try { const c=await api('/api/connections/'+encodeURIComponent(id)); const project=state.projects.find(p=>p.id===c.project_id); const html=`<div class="meta-row" style="margin-bottom:12px">连接设置</div><h2 class="detail-title">${esc(c.source)} → ${esc(c.project_name)}</h2><div class="detail-grid"><div class="detail-item"><label>状态</label><strong>${esc(c.status)}</strong></div><div class="detail-item"><label>创建时间</label><strong>${timeText(c.created_at)}</strong></div></div><button class="btn" data-copy="${esc(id)}">复制连接信息</button> <button class="btn ghost" data-toggle="${esc(id)}">${c.status==='已停用'?'重新启用':'停用连接'}</button><div class="detail-section hidden" id="projectRepair"><h3>重新选择项目位置</h3><p style="font-size:12px;color:var(--muted)">项目文件夹移动后，在这里填入它的新位置。</p><input class="input" id="projectPath" value="${esc(project?.path||'')}" aria-label="项目文件夹位置"><button class="btn" style="margin-top:10px" data-save-path="${esc(c.project_id)}">保存项目位置</button></div><div class="detail-section"><button class="mini-link" id="showTechnical">查看技术信息</button><div class="cred hidden" id="technicalDetails"><div class="cred-label">接口地址</div><div class="cred-value" style="white-space:normal;word-break:break-all">${esc(c.endpoint)}</div><div class="cred-label" style="margin-top:12px">密钥</div><div class="cred-value" style="white-space:normal;word-break:break-all">${esc(c.api_key)}</div><div class="cred-label" style="margin-top:12px">Codex 项目标识</div><div class="cred-value">${esc(c.project_id)}</div></div></div>`; openOverlay('settings','连接设置',html); state.drawer={kind:'settings',id}; if(revealRepair) $('projectRepair').classList.remove('hidden'); } catch(e){toast(e.message);} }
function updateRoute() { const source=$('sourceName').value.trim()||'任务来源'; const project=state.projects.find(p=>p.id===$('projectSelect').value); $('routePreview').textContent=`${source} → ${project?.name||'Codex 项目'}`; }
async function openNewConnection() {
 try{state.projects=await api('/api/projects');}catch(e){toast(e.message);return;}
 state.newConnection=null;
 $('sourceName').value='';
 setHtmlIfChanged('projectSelect',state.projects.length?state.projects.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}</option>`).join(''):'<option value="">未找到 Codex 项目</option>');
 $('step1').classList.remove('hidden');$('step2').classList.add('hidden');$('stepBar2').classList.remove('active');
 $('backBtn').style.visibility='hidden';$('nextBtn').textContent='创建连接';$('nextBtn').disabled=false;$('cancelBtn').textContent='取消';
 $('newTechnical').classList.add('hidden');feedback('connectionFeedback');updateRoute();
 $('overlay').classList.add('show');$('modal').classList.add('show');$('sourceName').focus();
}
function closeModal(){ if(state.pending.connection)return; $('modal').classList.remove('show'); if(!$('drawer').classList.contains('show')) $('overlay').classList.remove('show'); }
async function nextStep(){
 if(state.pending.connection)return;
 if(state.newConnection){closeModal();switchPage('connections');return;}
 const source=$('sourceName').value.trim(), projectId=$('projectSelect').value;
 if(!source||source.length>60){feedback('connectionFeedback','任务来源需为 1 到 60 个字。','error');$('sourceName').focus();return;}
 if(!projectId){feedback('connectionFeedback','先选择一个 Codex 项目。','error');$('projectSelect').focus();return;}
 state.pending.connection=true;$('nextBtn').disabled=true;$('nextBtn').textContent='创建中…';$('cancelBtn').disabled=true;
 feedback('connectionFeedback','正在创建连接…');
 try{
  const c=await api('/api/connections',{method:'POST',body:JSON.stringify({source,project_id:projectId})});
  state.newConnection=c;
  $('step1').classList.add('hidden');$('step2').classList.remove('hidden');$('stepBar2').classList.add('active');
  $('finalRoute').textContent=`${c.source} → ${c.project_name}`;
  $('backBtn').style.visibility='hidden';$('nextBtn').textContent='稍后设置';$('cancelBtn').textContent='关闭';
  await refresh(true);
 }catch(e){feedback('connectionFeedback',e.message,'error');}
 finally{state.pending.connection=false;$('nextBtn').disabled=false;$('cancelBtn').disabled=false;if(!state.newConnection)$('nextBtn').textContent='创建连接';}
}
document.addEventListener('click',async e=>{ const target=e.target.closest('button,[data-task],a'); if(!target)return; if(target.id==='quickConfirmPair')return quickPair('confirm'); if(target.dataset.page) return switchPage(target.dataset.page); if(target.dataset.task) return showTask(target.dataset.task); if(target.dataset.approval){ if(state.pending.approval)return; const taskId=state.drawer?.id; state.pending.approval=true; document.querySelectorAll('#drawerBody [data-approval]').forEach(button=>button.disabled=true); feedback('drawerFeedback','正在提交决定…'); try{await api('/api/approvals/'+encodeURIComponent(target.dataset.approval),{method:'POST',body:JSON.stringify({decision:target.dataset.decision})}); state.pending.approval=false; await showTask(taskId,false); feedback('drawerFeedback','决定已提交，任务状态已更新。','success');}catch(err){state.pending.approval=false; document.querySelectorAll('#drawerBody [data-approval]').forEach(button=>button.disabled=false); feedback('drawerFeedback',err.message,'error');} return; } if(target.dataset.copy) return copyConnection(target.dataset.copy); if(target.dataset.repath) return showSettings(target.dataset.repath,true); if(target.dataset.settings) return showSettings(target.dataset.settings); if(target.dataset.toggle){ try{const c=state.connections.find(x=>x.id===target.dataset.toggle); await api('/api/connections/'+encodeURIComponent(c.id),{method:'PATCH',body:JSON.stringify({status:c.status==='已停用'?'待验证':'已停用'})}); await refresh(); await showSettings(c.id); toast('连接设置已更新');}catch(err){toast(err.message);} return;} if(target.dataset.savePath){try{await api('/api/projects/'+encodeURIComponent(target.dataset.savePath),{method:'PATCH',body:JSON.stringify({path:$('projectPath').value})}); await refresh(); toast('项目位置已更新');}catch(err){toast(err.message);}return;} if(target.id==='showTechnical'){ $('technicalDetails').classList.toggle('hidden'); return $('projectRepair').classList.toggle('hidden'); } });
$('primaryAction').onclick=openNewConnection; $('firstConnect').onclick=()=>switchPage('feishu'); $('firstOtherConnect').onclick=()=>{switchPage('connections');openNewConnection();}; $('closeDrawer').onclick=closeDrawer; $('closeModal').onclick=closeModal; $('cancelBtn').onclick=closeModal; $('overlay').onclick=()=>{closeDrawer();closeModal();}; $('nextBtn').onclick=nextStep; $('backBtn').onclick=()=>{}; $('sourceName').oninput=()=>{updateRoute();feedback('connectionFeedback');}; $('projectSelect').onchange=()=>{updateRoute();feedback('connectionFeedback');}; $('copyNew').onclick=()=>copyConnection(state.newConnection.id); $('showNewTechnical').onclick=async()=>{const c=await api('/api/connections/'+encodeURIComponent(state.newConnection.id)); $('newTechnical').innerHTML=`<div class="cred-label">接口地址</div><div class="cred-value" style="white-space:normal">${esc(c.endpoint)}</div><div class="cred-label" style="margin-top:12px">密钥</div><div class="cred-value" style="white-space:normal;word-break:break-all">${esc(c.api_key)}</div>`; $('newTechnical').classList.toggle('hidden');}; $('historyToggle').onclick=()=>{state.history=!state.history;renderTasks();}; document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeDrawer();closeModal();}});
$('feishuForm').addEventListener('submit',saveFeishu);
$('feishuForm').addEventListener('input',e=>{if(e.target.id!=='feishuSample')state.feishuDirty=true;feedback('advancedFeedback');renderFeishuPromptPreview();});
const markQuickDirty=()=>{state.quickDirty=true;feedback('quickSetupFeedback');feedback('quickCheckFeedback');feedback('quickPairFeedback');renderQuickFeishu();};
$('quickAppId').addEventListener('input',markQuickDirty);
$('quickSecret').addEventListener('input',markQuickDirty);
$('quickProject').addEventListener('change',markQuickDirty);
$('quickSetup').onclick=quickSetup;
$('quickCheck').onclick=quickCheck;
$('quickPair').onclick=()=>quickPair('start');
$('retryRefresh').onclick=async()=>{const button=$('retryRefresh');if(button.disabled)return;button.disabled=true;button.textContent='重试中…';try{await refresh(true);}finally{button.disabled=false;button.textContent='重试';}};
switchPage(state.page);refresh();setInterval(refresh,3000);
$('startupNotice').hidden=true;
$('panelRoot').hidden=false;

$('promptForm').addEventListener('input',e=>{if(e.target.id!=='feishuSample')state.promptDirty=true;feedback('promptFeedback');renderFeishuPromptPreview();});
$('promptForm').addEventListener('submit',async e=>{
 e.preventDefault();if(state.pending.prompt||!renderFeishuPromptPreview())return;
 state.pending.prompt=true;$('savePrompt').disabled=true;$('savePrompt').textContent='保存中…';
 try{await api('/api/feishu',{method:'PATCH',body:JSON.stringify({prompt_template:$('feishuPrompt').value})});state.promptDirty=false;await refresh(true);feedback('promptFeedback','已保存。之后收到的新消息使用这个 Prompt。','success');}
 catch(err){feedback('promptFeedback',err.message,'error');}
 finally{state.pending.prompt=false;$('savePrompt').disabled=false;$('savePrompt').textContent='保存 Prompt';}
});
