// Card configuration uses the existing Codex engine. These previews never submit review decisions.
let assistantGroup='',assistantTask=null,assistantBusy=false,assistantLoading=false,assistantRequest=null;
let assistantRows=[],cardPreviews=[];
const fieldTitle=k=>({voice:'音色',language:'语言',format:'格式'}[k]||k);
const choiceTitle=o=>{const d=o.data;const t=d&&typeof d==='object'?(d.label||d.voice||o.value):d;return t==='example_voice'?'示例音色':String(t??o.value);};
function safeCardLink(url,label){try{const u=new URL(url);if(!['https:','http:'].includes(u.protocol)||u.username||u.password)return esc(label);return `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`;}catch{return esc(label);}}
function fieldsForStage(s,cards){const form=cards.find(c=>c.id===s.form_card);return Object.assign({},...(form?.actions||[]).map(a=>a.fields),s.fields||{});}
function cardsForGroup(f,chat){
 const route=(f.routes||[]).find(r=>r.chat_id===chat),cards=f.cards||[];
 if(route?.workflow){const lookup=Object.fromEntries(route.workflow.steps.map(s=>[s.id,s]));return route.workflow.steps.filter(s=>s.kind==='review').map(s=>({name:s.name,group:route.name,fields:fieldsForStage(s,cards),next:lookup[s.next]?.name||'完成任务',revise:lookup[s.revise]?.name,legacy:false}));}
 return cards.filter(c=>(route?.card_types||f.default_card_types||[]).includes(c.id)).map(c=>({name:c.name,group:route?.name||'当前群',fields:Object.assign({},...c.actions.map(a=>a.fields)),next:'交回原 Codex 任务，按已有规则继续',legacy:true,actions:c.actions.map(a=>a.label)}));
}
function previewMarkup(c,i){
 const fields=Object.entries(c.fields).map(([name,s])=>{
  const options=s.options||[],first=options[0],material=first?.data?.material_url;
  return `<div class="preview-field"><span>${esc(fieldTitle(name))}</span>${s.type==='choice'?`<div class="preview-choice">${esc(first?choiceTitle(first):'未设置')}<span class="preview-default">默认</span></div>${options.length>1?'<p class="config-help">可选：'+options.map(o=>esc(choiceTitle(o))).join('、')+'</p>':''}`:'<div class="preview-choice">提交时填写</div>'}${material?'<p class="config-help">素材已随音色绑定 · '+safeCardLink(material,'查看默认素材')+'</p>':''}</div>`;
 }).join('');
 return `<article class="card-preview"><div class="card-preview-head"><span class="card-eyebrow">${c.legacy?'已保存的卡片':'流程审核卡'}</span><h3>${esc(c.name)}</h3></div><div class="card-preview-body"><div class="preview-document">审核内容由任务自动带入</div>${fields}<div class="preview-buttons" aria-label="按钮预览">${c.legacy?(c.actions||[]).map(a=>'<span>'+esc(a)+'</span>').join(''):'<span class="preview-primary">确认通过，继续</span>'+(c.revise?'<span>提交修改意见</span>':'')+'<span>取消任务</span>'}</div></div><div class="card-preview-footer"><p>通过后：${esc(c.next)}</p>${c.revise?'<p>有意见：'+esc(c.revise)+'，之后重新审核。</p>':''}<button class="btn" data-card-change="${i}">描述修改</button></div></article>`;
}
function renderCardsPage(){
 const f=state.feishu;if(!f)return;
 const groups=f.allowed_chat_ids||[];
 if(!groups.includes(assistantGroup)){assistantGroup=groups[0]||'';assistantTask=null;}
 const options=groups.map(id=>{const r=f.routes?.find(x=>x.chat_id===id);return `<option value="${esc(id)}" ${id===assistantGroup?'selected':''}>${esc(r?.name||'已配对群')}</option>`;}).join('');
 setHtmlIfChanged('cardIntentGroup',options||'<option value="">先在飞书页完成群配对</option>');
 $('submitCardIntent').disabled=assistantBusy||!assistantGroup;
 cardPreviews=cardsForGroup(f,assistantGroup);
 setHtmlIfChanged('cardsGrid',cardPreviews.map(previewMarkup).join('')||'<div class="empty-card">描述你要的第一张卡片，Codex 会把它加入这个群的流程。</div>');
 loadAssistantConversation();loadDeliveries();
}
window.renderCardsPage=renderCardsPage;
function renderAssistantConversation(){
 const rows=assistantRows.filter(r=>r.chat_id===assistantGroup);
 const row=rows.find(r=>r.task_id===assistantTask);
 $('cardAssistantConversation').classList.toggle('hidden',!row&&rows.length===0);
 if(!row){$('submitCardIntent').disabled=assistantBusy||!assistantGroup;$('submitCardIntent').textContent='交给 Codex 配置';$('cardIntentHint').textContent='配置应用于新任务。';setHtmlIfChanged('cardAssistantConversation',rows.length?`<div class="assistant-result"><strong>最近一次配置对话</strong><p>${esc(rows[0].message)}</p><button class="btn" data-resume-card="${esc(rows[0].task_id)}">继续这次对话</button></div>`:'');return;}
 const active=['执行中','等待 Codex'].includes(row.status);
 $('submitCardIntent').disabled=assistantBusy||active||!assistantGroup;
 $('submitCardIntent').textContent=active?'Codex 正在配置…':assistantTask?'继续交给 Codex':'交给 Codex 配置';
 $('cardIntentHint').textContent=assistantTask?'补充要求会进入同一次配置对话。':'配置应用于新任务。';
 setHtmlIfChanged('cardAssistantConversation',`<div class="assistant-result"><div class="assistant-result-head"><strong>${active?'Codex 正在处理':row.status==='已完成'?'Codex 已回复':esc(row.status)}</strong><button class="mini-link" type="button" id="newCardConversation">开始新需求</button></div><p class="assistant-user">${esc(row.message)}</p>${row.result?'<div class="assistant-answer">'+esc(row.result)+'</div>':'<p class="config-help">正在读取现有流程和卡片，稍后会在这里显示结果。</p>'}${row.thread_id?'<a class="mini-link" href="codex://threads/'+encodeURIComponent(row.thread_id)+'">在 Codex 中查看完整对话</a>':''}</div>`);
}
async function loadAssistantConversation(){
 if(assistantLoading)return;assistantLoading=true;
 try{assistantRows=await api('/api/feishu/card-assistant');renderAssistantConversation();}
 catch(e){feedback('cardIntentFeedback',e.message,'error');}finally{assistantLoading=false;}
}
$('cardIntentGroup').addEventListener('change',()=>{assistantGroup=$('cardIntentGroup').value;assistantTask=null;assistantRequest=null;$('submitCardIntent').textContent='交给 Codex 配置';renderCardsPage();});
$('cardIntentForm').addEventListener('submit',async e=>{
 e.preventDefault();if(assistantBusy)return;
 const message=$('cardIntentText').value.trim();if(!message)return feedback('cardIntentFeedback','写一句你希望卡片怎么改。','error');
 const signature=JSON.stringify([assistantGroup,assistantTask,message]);
 if(!assistantRequest||assistantRequest.signature!==signature)assistantRequest={signature,key:crypto.randomUUID()};
 const body={chat_id:assistantGroup,message,request_id:assistantRequest.key};if(assistantTask)body.task_id=assistantTask;
 assistantBusy=true;$('submitCardIntent').disabled=true;$('cardIntentGroup').disabled=true;feedback('cardIntentFeedback','正在交给 Codex…');
 try{const r=await api('/api/feishu/card-assistant',{method:'POST',body:JSON.stringify(body)});assistantTask=r.task_id;$('cardIntentText').value='';assistantRequest=null;feedback('cardIntentFeedback','已交给 Codex，处理结果会显示在下方。','success');await loadAssistantConversation();}
 catch(err){feedback('cardIntentFeedback',err.message+'；重试会沿用同一次请求。','error');}
 finally{assistantBusy=false;$('cardIntentGroup').disabled=false;renderAssistantConversation();}
});
document.addEventListener('click',e=>{
 const b=e.target.closest('button');if(!b)return;
 let text='';
 if(b.dataset.cardExample){text={voice:'把审核卡默认音色设为示例音色，保留现有素材链接，并让素材始终随音色一起选择。',revise:'给文档审核卡增加提交修改意见的按钮，按文档评论修改后重新发起审核。',new:'我想增加一张审核卡：审核内容是……，需要选择……，通过后……，有意见时……。'}[b.dataset.cardExample];}
 if(b.dataset.cardChange!==undefined){const card=cardPreviews[Number(b.dataset.cardChange)];if(card)text='修改“'+card.name+'”：';}
 if(text){$('cardIntentText').value=text;$('cardIntentText').focus();$('cardIntentText').scrollIntoView({behavior:'smooth',block:'center'});}
 if(b.id==='newCardConversation'){assistantTask=null;assistantRequest=null;$('submitCardIntent').textContent='交给 Codex 配置';$('cardIntentHint').textContent='配置应用于新任务。';renderCardsPage();}
 if(b.dataset.resumeCard){assistantTask=b.dataset.resumeCard;renderAssistantConversation();$('cardIntentText').focus();}
});
if(state.page==='cards')renderCardsPage();
