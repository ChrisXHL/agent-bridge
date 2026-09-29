// The shared panel helpers escape every value before HTML rendering.
let editingConfig=null, configBusy=false;
const deliveryLabels={queued:'等待发送',sending:'正在发送',sent:'已发送，等待操作',pending:'已收到操作，等待原任务结束',submitted:'已提交回原任务',delivery_uncertain:'发送结果待核对'};
const checkCards=(selected,prefix)=>`<div class="config-checks">${(state.feishu?.cards||[]).map(c=>`<label><input type="checkbox" name="${prefix}" value="${esc(c.id)}" ${selected.includes(c.id)?'checked':''}> ${esc(c.name)}</label>`).join('')||'尚未配置卡片'}</div>`;
function renderConfigPages(){
 const f=state.feishu;if(!f)return;
 if(state.page==='routes')setHtmlIfChanged('routesGrid',(f.routes||[]).map(r=>{const c=state.connections.find(c=>c.id===r.connection_id);return `<div class="conn-card"><div class="conn-head"><div><div class="conn-title">${esc(r.name)}</div><p class="config-help">${esc(c?.project_name||'项目不可用')} · ${r.workflow?r.workflow.steps.length+' 个阶段':r.card_types.length+' 种卡片'}</p></div>${badge(r.enabled===false?'已停用':'可用')}</div>${r.workflow?workflowOverview(r.workflow):''}<button class="btn" data-edit-route="${esc(r.chat_id)}">${r.workflow?'查看 Skill 与执行细节':'编辑流程'}</button></div>`;}).join('')||'<div class="empty-card">还没有单独配置群流程。已配对的群目前使用默认项目和 Prompt。</div>');
 if(state.page==='routes')loadWorkflowRuns();
 if(state.page==='cards'&&window.renderCardsPage)window.renderCardsPage();
}
window.renderConfigPages=renderConfigPages;
let deliveryLoading=false;
async function loadDeliveries(){
 if(deliveryLoading)return;deliveryLoading=true;
 try{
  const [list,runs]=await Promise.all([api('/api/feishu/card-deliveries'),api('/api/feishu/workflow-runs')]);
  const current=runs.filter(r=>r.chat_id===assistantGroup).map(r=>`<div class="config-delivery"><strong>${esc(r.name)}</strong> · ${esc(workflowLabels[r.state]||r.state)}<p class="config-help">${esc(r.stage_name)}${r.error?'<br>'+esc(r.error):''}</p><button class="btn" data-workflow-run="${esc(r.id)}">查看进度</button></div>`).join('');
  const older=list.map(r=>`<div class="config-delivery"><strong>${esc(state.feishu.cards.find(c=>c.id===r.card_type)?.name||'审核卡')}</strong> · ${esc(deliveryLabels[r.state]||r.state)}<p class="config-help">${timeText(r.created_at)}${r.error?'<br>'+esc(r.error):''}</p></div>`).join('');
  setHtmlIfChanged('cardDeliveries',current+older||'<p class="config-help">发起任务后，这里会显示发送和审核进度。</p>');
 }catch(e){$('cardDeliveries').textContent=e.message;}finally{deliveryLoading=false;}
}
function editRoute(chatId){
 const f=state.feishu;if(!f)return;
 const r=(f.routes||[]).find(r=>r.chat_id===chatId)||{chat_id:'',name:'',connection_id:f.connection_id,prompt_template:f.prompt_template,review_prompt_template:f.review_prompt_template||'',card_types:f.default_card_types||[],enabled:true};
 editingConfig={kind:'route',original:chatId||null,workflow:r.workflow};
 if(r.workflow){
  const project=state.connections.find(c=>c.id===r.connection_id)?.project_name||'项目不可用';
  return openOverlay('route','群流程',`<h2>${esc(r.name)}</h2><p>${esc(project)} · ${r.enabled===false?'已停用':'已启用'}</p>${workflowOverview(r.workflow)}${workflowDetails(r.workflow)}<p class="config-help">例如：“在配音之后增加试听审核，不通过就重新生成。”Codex 会保留现有群和项目，只修改相关阶段并检查输入输出。</p>`);
 }
 openOverlay('route','群流程',`<p class="config-help">新消息使用这里的流程。已有任务及已发送卡片仍回到原项目。</p><div class="field"><label for="routeName">流程名称</label><input id="routeName" class="input" value="${esc(r.name)}" placeholder="例如：视频翻译审核"></div><div class="field"><label for="routeChat">已配对的群</label><select id="routeChat" class="select" ${chatId?'disabled':''}><option value="">选择一个群</option>${(f.allowed_chat_ids||[]).map(id=>`<option value="${esc(id)}" ${id===r.chat_id?'selected':''}>${esc(f.routes?.find(x=>x.chat_id===id)?.name||'已配对群')} · ${esc(id)}</option>`).join('')}</select></div><div class="field"><label for="routeConnection">Codex 项目连接</label><select id="routeConnection" class="select">${state.connections.filter(c=>c.status!=='已停用').map(c=>`<option value="${esc(c.id)}" ${c.id===r.connection_id?'selected':''}>${esc(c.source)} → ${esc(c.project_name)}</option>`).join('')}</select><p class="config-help">如需其他项目，先在“连接”页创建连接。</p></div>${r.workflow?workflowOverview(r.workflow):''}<details ${r.workflow?'':'open'}><summary>兼容流程设置</summary><p class="config-help">多阶段流程通过 Codex 对话维护。这里的旧版提示词只用于尚未迁移的流程。</p><div class="field"><label for="routePrompt">收到新任务时</label><textarea id="routePrompt" class="input" rows="10">${esc(r.prompt_template)}</textarea><p class="config-help">用 {{message}} 放入群里发来的消息，必须恰好出现一次。</p></div><div class="field"><label for="routeReviewPrompt">旧版卡片回传后的处理</label><textarea id="routeReviewPrompt" class="input" rows="10">${esc(r.review_prompt_template||'')}</textarea><p class="config-help">独立维护审核后的步骤，用 {{message}} 接收卡片信息；留空沿用卡片原有指令。</p></div></details><label>该群可用的卡片</label>${checkCards(r.card_types,'routeCard')}<label><input type="checkbox" id="routeEnabled" ${r.enabled!==false?'checked':''}> 启用此群流程</label><div class="config-actions"><button class="btn primary" id="saveRoute">保存流程</button>${chatId?'<button class="btn" id="removeRoute">恢复默认流程</button>':''}</div>`);
}
async function saveConfig(endpoint,body){
 if(configBusy)return;configBusy=true;feedback('drawerFeedback','正在保存…');
 try{state.feishu=await api(endpoint,{method:'POST',body:JSON.stringify(body)});feedback('drawerFeedback','已保存，新消息会使用这份配置。','success');await refresh(true);return true;}
 catch(e){feedback('drawerFeedback',e.message,'error');return false;}finally{configBusy=false;}
}
function selectedCards(name){return [...document.querySelectorAll(`input[name="${name}"]:checked`)].map(x=>x.value);}
document.addEventListener('click',async e=>{
 const b=e.target.closest('button');if(!b)return;
 if(b.id==='addRoute')return openOverlay('route','通过对话创建流程',`<h2>告诉 Codex 想怎么处理</h2><p>例如：给翻译 2.0 创建字幕流程，译文需要人工审核，通过后用示例音色配音，再把音频和 FCPXML 发回来。</p><p class="config-help">Codex 会使用 codex-bridge-workflows 匹配已配对群和项目，检查每步的输入输出并保存。之后直接说要改哪一步即可。</p>`);
 if(b.dataset.editRoute)return editRoute(b.dataset.editRoute);
 if(b.id==='saveRoute'){
  const body={chat_id:$('routeChat').value,name:$('routeName').value.trim(),connection_id:$('routeConnection').value,prompt_template:$('routePrompt').value,review_prompt_template:$('routeReviewPrompt').value,card_types:selectedCards('routeCard'),enabled:$('routeEnabled').checked};
  if(editingConfig.workflow)body.workflow=editingConfig.workflow;
  if(await saveConfig('/api/feishu/routes',body)){editingConfig.original=body.chat_id;}
 }
 if(b.id==='removeRoute'&&editingConfig?.original){if(await saveConfig('/api/feishu/routes/remove',{chat_id:editingConfig.original}))closeDrawer();}

});
renderConfigPages();

function workflowOverview(w){
 const lookup=Object.fromEntries(w.steps.map(s=>[s.id,s]));
 const main=[];let step=w.steps[0];const seen=new Set();
 while(step&&!seen.has(step.id)){main.push(step);seen.add(step.id);step=lookup[step.next];}
 return `<div class="detail-section"><h3>${esc(w.name)}</h3><ol>${main.map(s=>`<li style="margin:8px 0">${esc(s.name)}${s.kind==='review'?' · 等待人工':''}${stageSkills(s)}${s.revise?'<p class="config-help">有修改意见：'+esc(lookup[s.revise].name)+'，之后重新审核。</p>':''}</li>`).join('')}</ol><p class="config-help">告诉 Codex 要增加、删除或调整哪一步，它会检查并保存。变更适用于新任务。</p></div>`;
}
const artifactNames={original_fcpxml:'最初的 FCPXML 原件',subtitle_manifest:'逐字幕映射清单',review_document:'英文审核文档',revision_report:'评论修订报告',translated_fcpxml:'英文 FCPXML',timeline_report:'时间轴校验报告',audio:'完整配音音频',audio_alignment_report:'逐段音频对齐报告',delivery:'飞书交付回执',translation_supervision:'翻译严格监督报告',revision_supervision:'修订严格监督报告'};
const artifactName=k=>artifactNames[k]||k;
function stageSkills(s){return s.kind==='review'?'<p class="stage-skill-note">人工操作 · Bridge 接收决定并核对版本</p>':`<div class="stage-skills">${s.skills?.length?s.skills.map(name=>'<span class="skill-tag">'+esc(name)+'</span>').join(''):'<span class="stage-skill-note">未单独指定 Skill，请查看阶段指令</span>'}</div>`;}
function workflowDetails(w){
 const lookup=Object.fromEntries(w.steps.map(s=>[s.id,s.name]));
 const gate={subtitle_manifest:'从原件重算字幕清单，绑定真实文档段落 ID',subtitle_alignment:'重算逐条批准文字映射，并核对完整 XML、时间、资源和样式',audio_alignment:'重新实测分段、重建成品并核对本任务合成回执',file:'文件存在且非空，保存副本和校验值',fcpxml:'与原件比对结构、资源、样式和时间属性',feishu_document:'读取正文及完整未解决评论并保存版本快照',delivery:'读回实际消息，核对原会话和附件标识'};
 return `<section class="workflow-details"><h3>每阶段执行说明</h3><p class="config-help">以下是已配置的执行要求。实际调用情况和验收结果需查看运行记录；文件存在不代表内容质量通过。</p>${w.steps.map((s,i)=>`<section class="stage-detail"><h4>${esc(s.name)}${s.id==='revise'?' · 修改分支':''}</h4>${stageSkills(s)}<details ${i===0?'open':''}><summary>输入、产物和验收要求</summary><dl><dt>读取什么</dt><dd>${s.needs?.length?s.needs.map(k=>esc(artifactName(k))).join('、'):'原任务消息及最初发送的文件'}</dd><dt>执行要求</dt><dd>${esc(s.prompt||'等待真实用户提交卡片。通过后继续，提交意见后修订并再次审核。')}</dd>${s.settings?'<dt>执行参数</dt><dd><ul>'+Object.entries(s.settings).map(([k,v])=>'<li>'+esc(k)+'：'+esc(v)+'</li>').join('')+'</ul></dd>':''}<dt>交付什么</dt><dd>${Object.keys(s.produces||{}).length?Object.keys(s.produces).map(k=>esc(artifactName(k))).join('、'):'审核决定、选项及对应文档版本'}</dd><dt>业务验收要求</dt><dd>${s.acceptance?.length?'<ul>'+s.acceptance.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ul>':'未单列；需核对阶段指令'}</dd><dt>Bridge 自动检查</dt><dd>${s.kind==='review'?'审核人权限、卡片轮次、选择项和文档版本':Object.entries(s.produces||{}).map(([k,v])=>esc(artifactName(k))+'：'+esc(gate[v.kind]||v.kind)).join('<br>')}</dd><dt>完成后</dt><dd>${esc(lookup[s.next]||'流程结束')}${s.revise?'；有意见则进入 '+esc(lookup[s.revise]):''}</dd></dl></details></section>`).join('')}</section>`;
}
const workflowLabels={running:'处理中',waiting_review:'等待审核',checking_review:'核对审核版本',blocked:'需要处理',completed:'已完成',cancelled:'已取消'};
let runsLoading=false;
async function loadWorkflowRuns(){
 if(runsLoading)return;runsLoading=true;
 try{const runs=await api('/api/feishu/workflow-runs');setHtmlIfChanged('workflowRuns',runs.map(r=>`<div class="config-delivery"><strong>${esc(r.name)}</strong> · ${esc(workflowLabels[r.state]||r.state)}<p class="config-help">${esc(r.stage_name)} · 第 ${r.epoch} 轮${r.error?'<br>'+esc(r.error):''}</p><button class="btn" data-workflow-run="${esc(r.id)}">查看进度</button></div>`).join('')||'<p class="config-help">新任务开始后，这里会显示当前阶段、审核和恢复记录。已有旧版任务继续按原规则处理。</p>');}catch(e){$('workflowRuns').textContent=e.message;}finally{runsLoading=false;}
}
document.addEventListener('click',async e=>{
 const b=e.target.closest('[data-workflow-run]');if(!b)return;
 try{const r=await api('/api/feishu/workflow-runs/'+encodeURIComponent(b.dataset.workflowRun));
 state.drawer={kind:'workflow',id:r.id};
 openOverlay('workflow','流程进度',`<h2>${esc(r.definition.name)}</h2><p>${esc(r.stage.name)} · ${esc(workflowLabels[r.state]||r.state)}</p>${r.error?'<p>'+esc(r.error)+'</p>':''}<p class="config-help">需要恢复或调整时，告诉 Codex 任务名称和当前情况。</p><h3>处理记录</h3>${r.history.map(h=>`<div class="config-delivery">${timeText(h.created_at)} · 第 ${h.epoch} 轮 · ${esc(({started:'任务开始',stage_entered:'进入阶段',review_requested:'等待审核',decision_received:'收到审核决定',decision_verified:'审核版本核对通过',outputs_verified:'产物校验通过',completed:'全部完成',blocked:'执行暂停',review_blocked:'审核暂停',retry_requested:'恢复当前阶段',review_reopened:'返回人工审核',cancelled:'任务取消',effect_started:'登记外部操作',effect_succeeded:'外部操作成功',effect_uncertain:'外部结果待核对'})[h.event]||h.event)}${h.data.name?'<br>'+esc(h.data.name):''}${h.data.reason?'<br>'+esc(h.data.reason):''}</div>`).join('')}`);
 }catch(err){toast(err.message);}
});
