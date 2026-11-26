(function(){
  let timer=null, busy=false, data=null, confirming=null, editing=null, dirty=false;
  const scanning=d=>d.login&&['wait','scaned','need_verifycode'].includes(d.login.status);
  const report=(m='',bad=false)=>feedback('wxFeedback',m,bad?'error':'info');

  function scanCard(lg){
    const code=lg.status==='need_verifycode'?`<div class="account-bot-actions"><label>手机上显示的数字 <input class="input" id="wxCode" inputmode="numeric" autocomplete="off"></label><div class="account-bot-buttons"><button class="btn primary" id="wxVerify">提交</button></div></div>`:'';
    return `<div class="account-card"><div class="account-heading"><div><h3>扫码接入</h3><p>${esc(lg.message)}</p></div><span>二维码 5 分钟内有效</span></div><div class="account-column"><div class="assistant-scan"><img src="${esc(lg.qr_url)}" alt="用微信扫码接入 ClawBot" width="240" height="240"><div><h4>用手机微信扫一扫</h4><p>先在手机微信「我 → 设置 → 插件」打开微信 ClawBot。扫码并在手机上确认后，这里会自动更新。</p>${code}</div></div></div></div>`;
  }

  function botCard(a){
    const ask=confirming&&confirming.id===a.id?confirming.kind:null;
    const box=ask==='off'?`<div class="bot-disable-confirm" role="group" aria-label="确认停用${esc(a.name)}"><p>停用后，这个机器人将停止接收微信消息；已有任务和记录保留。</p><div><button class="btn" data-wx-cancel>取消</button><button class="btn primary" data-wx-confirm-off="${esc(a.id)}">确认停用</button></div></div>`
      :ask==='delete'?`<div class="bot-disable-confirm" role="group" aria-label="确认删除${esc(a.name)}"><p>删除后，这个微信不再接入本机 Bridge，需要重新扫码才能恢复；已有任务记录和工作文件夹保留。</p><div><button class="btn" data-wx-cancel>取消</button><button class="btn primary" data-wx-confirm-delete="${esc(a.id)}">确认删除</button></div></div>`:'';
    return `<div class="account-item account-bot"><div class="account-item-main"><strong>${esc(a.name)}</strong><span class="status ${esc(a.tone)}">${esc(a.runtime)}</span></div><p class="bot-folder" title="${esc(a.project_path||'')}">工作文件夹：${esc(a.project_name||'未设置')}</p>${a.error?`<p class="account-bot-warning">${esc(a.error)}</p>`:''}<div class="account-bot-actions"><label>任务执行 <select class="select bot-backend" data-wx-backend="${esc(a.id)}" aria-label="${esc(a.name)}的任务执行工具"><option value="codex" ${a.execution_backend==='claude'?'':'selected'}>Codex CLI · GPT</option><option value="claude" ${a.execution_backend==='claude'?'selected':''}>Claude Code CLI</option></select></label><div class="account-bot-buttons"><button class="btn ghost" data-wx-edit="${esc(a.id)}">设置</button><button class="btn ghost" data-wx-toggle="${esc(a.id)}">${a.enabled?'停用':'重新启用'}</button><button class="btn ghost" data-wx-delete="${esc(a.id)}">删除</button></div></div>${box}</div>`;
  }

  function render(){
    const d=data, n=d.accounts.length;
    const note=d.login&&!scanning(d)&&d.login.status!=='confirmed'?`<p class="config-help">${esc(d.login.message)}</p>`:'';
    const list=n?`<div class="account-card"><div class="account-heading"><div><h3>微信</h3></div><span>${n} 个机器人</span></div><div class="account-column account-bot-column"><h4>机器人</h4><div class="account-bot-list">${d.accounts.map(botCard).join('')}</div></div></div>`:'<p class="config-help">还没有接入微信。点击「扫码接入微信」开始。</p>';
    $('wxBody').innerHTML=(scanning(d)?scanCard(d.login):note)+list;
  }

  async function load(){
    clearTimeout(timer);
    if(state.page!=='weixin')return;
    try{
      const d=await api('/api/weixin');
      if(!d.accounts){$('wxBody').innerHTML='<p class="config-help">微信接入正在更新，请稍候…</p>';}
      else{data=d; if(!editing&&!['wxCode'].includes(document.activeElement?.id)&&!document.activeElement?.dataset?.wxBackend)render();}
    }catch(e){ if(!editing)$('wxBody').innerHTML=`<p class="config-help">${esc(e.message)}</p>`; }
    timer=setTimeout(load,data&&scanning(data)?1500:4000);
  }

  function view(edit){ editing=edit; $('wxList').classList.toggle('hidden',!!edit); $('wxEditor').classList.toggle('hidden',!edit); if(!edit){dirty=false;load();} }

  function editor(a){
    view(a.id); dirty=false;
    let projectId=a.project_id;
    $('wxEditor').innerHTML=`<form id="wxForm">
  <div class="wx-bar"><button class="wx-link" id="wxBack" type="button">‹ 微信账号</button><strong>${esc(a.name)}</strong><button class="btn primary" id="wxSave">保存</button></div>
  <div class="wx-group">
    <label class="wx-row"><span>名称</span><input id="wxName" maxlength="60" value="${esc(a.name)}" required></label>
    <div class="wx-row"><span>工作文件夹</span><span class="wx-value" id="wxFolder" title="${esc(a.project_path||'')}">${esc(a.project_name||'未设置')}</span><button class="wx-link" id="wxPickFolder" type="button">更改…</button></div>
  </div>
  <div class="wx-group"><label class="wx-group-title" for="wxPrompt">业务 Prompt</label><textarea id="wxPrompt" rows="9" spellcheck="false" placeholder="留空则直接发送消息原文">${esc(a.prompt)}</textarea></div>
  <p class="wx-note">微信消息会填入 {{message}}。在微信里说「记住…」会直接更新这里。</p>
  <div id="wxFeedback" class="inline-feedback hidden" role="status" aria-live="polite"></div></form>`;
    $('wxForm').oninput=()=>{dirty=true;};
    $('wxBack').onclick=()=>{ if(dirty&&!confirm('放弃未保存的修改？'))return; view(null); };
    $('wxPickFolder').onclick=async()=>{ const b=$('wxPickFolder'); b.disabled=true;
      try{const r=await api('/api/projects/pick',{method:'POST',body:JSON.stringify({project_id:projectId})}); if(!r.cancelled){state.projects=r.projects;projectId=r.project.id;dirty=true;$('wxFolder').textContent=r.project.name;$('wxFolder').title=r.project.path;}}
      catch(err){report(err.message,true);} finally{b.disabled=false;} };
    $('wxForm').onsubmit=async e=>{ e.preventDefault(); $('wxSave').disabled=true;
      try{await patch(a.id,{name:$('wxName').value,project_id:projectId,prompt:$('wxPrompt').value}); view(null); toast('已保存');}
      catch(err){report(err.message,true);$('wxSave').disabled=false;} };
  }

  const patch=(id,body)=>api('/api/weixin/accounts/'+encodeURIComponent(id),{method:'PATCH',body:JSON.stringify(body)});
  async function act(fn){ if(busy)return; busy=true; try{await fn();}catch(e){toast(e.message);} finally{busy=false; load();} }

  document.addEventListener('click',e=>{
    const b=e.target.closest('#weixinPage button'); if(!b||editing)return;
    const ds=b.dataset;
    if(b.id==='wxLogin') act(()=>api('/api/weixin/login',{method:'POST',body:'{}'}));
    if(b.id==='wxVerify') act(()=>api('/api/weixin/login/verify',{method:'POST',body:JSON.stringify({code:$('wxCode').value})}));
    if(ds.wxEdit){ const a=data.accounts.find(x=>x.id===ds.wxEdit); if(a)editor(a); }
    if(ds.wxToggle){ const a=data.accounts.find(x=>x.id===ds.wxToggle); if(a.enabled){confirming={id:a.id,kind:'off'};render();} else act(()=>patch(a.id,{enabled:true})); }
    if(ds.wxDelete){ confirming={id:ds.wxDelete,kind:'delete'}; render(); }
    if(ds.wxCancel!==undefined){ confirming=null; render(); }
    if(ds.wxConfirmOff) act(async()=>{confirming=null; await patch(ds.wxConfirmOff,{enabled:false});});
    if(ds.wxConfirmDelete) act(async()=>{confirming=null; await api('/api/weixin/accounts/'+encodeURIComponent(ds.wxConfirmDelete)+'/delete',{method:'POST',body:'{}'}); toast('已删除这个微信机器人');});
  });
  document.addEventListener('change',e=>{ const id=e.target.dataset?.wxBackend; if(id){e.target.blur(); act(async()=>{await patch(id,{execution_backend:e.target.value}); toast('切换执行工具只影响新任务');});} });
  window.renderWeixin=()=>{ if(!editing)load(); };
  load();
})();
