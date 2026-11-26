// Generic pages for chat platforms registered in platforms/<id>.py.
// Layout, classes and copy follow the 微信 page so every platform looks the same
// (rules: skills/bridge-platform-adapter/references/ui-rules.md).
(function(){
  const pages=window.platformPages=window.platformPages||{};
  const ACTIVE=['wait','scaned','need_verifycode'];

  const pad=l=>/^[\x00-\x7f]/.test(l)?' '+l+' ':l;
  const spaced=s=>s.replace(/ +([，。、：；！？「」])/g,'$1').trim();
  function mount(meta){
    const key='p_'+meta.id, pageId=key+'Page', navId='nav'+key[0].toUpperCase()+key.slice(1);
    if($(pageId))return;
    const nav=document.createElement('button');
    nav.id=navId; nav.dataset.page=key; nav.setAttribute('aria-label',meta.label); nav.title=meta.label;
    nav.innerHTML=`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="${esc(meta.icon)}"/></svg><span>${esc(meta.label)}</span>`;
    document.querySelector('.nav').appendChild(nav);
    const page=document.createElement('div');
    page.id=pageId; page.className='hidden';
    page.innerHTML=`<div data-p-list><div class="groups-toolbar"><div class="groups-toolbar-actions"><button class="btn primary" data-p-bind type="button">${esc(meta.bind_label)}</button></div></div><div data-p-body></div></div><section data-p-editor class="wx-settings hidden"></section>`;
    $('weixinPage').after(page);
    pages[key]=Object.assign(controller(meta,page),{title:meta.label,subtitle:meta.subtitle||spaced('接入'+pad(meta.label)+'，在'+pad(meta.label)+'里给助手派任务。')});
  }

  function controller(meta,root){
    // Latin names get spaces next to Chinese text: 在 Telegram 里 / 在钉钉里
    const L=/^[\x00-\x7f]/.test(meta.label)?' '+meta.label+' ':meta.label;
    const tx=s=>s.replace(/ +([，。、：；！？「」])/g,'$1').replace(/([「」]) +/g,'$1').trim();
    let timer=null,busy=false,data=null,confirming=null,editing=null,dirty=false,binding=false;
    const q=s=>root.querySelector(s);
    const base='/api/platforms/'+encodeURIComponent(meta.id);
    const scanning=d=>d.login&&ACTIVE.includes(d.login.status)&&d.login.qr_url;

    function bindCard(lg){
      if(meta.bind_mode==='qr'){
        const code=lg.status==='need_verifycode'?`<div class="account-bot-actions"><label>手机上显示的数字 <input class="input" data-p-code inputmode="numeric" autocomplete="off"></label><div class="account-bot-buttons"><button class="btn primary" data-p-verify>提交</button></div></div>`:'';
        return `<div class="account-card"><div class="account-heading"><div><h3>扫码接入</h3><p>${esc(lg.message)}</p></div><span>二维码 5 分钟内有效</span></div><div class="account-column"><div class="assistant-scan"><img src="${esc(lg.qr_url)}" alt="扫码接入${esc(meta.label)}" width="240" height="240"><div><h4>用手机扫一扫</h4><p>${esc(meta.bind_help)}</p>${code}</div></div></div></div>`;
      }
      const fields=meta.bind_fields.map(f=>`<label class="wx-row"><span>${esc(f.label)}</span><input data-p-field="${esc(f.key)}" ${f.secret?'type="password" autocomplete="new-password"':'autocomplete="off"'} placeholder="${esc(f.placeholder)}"></label>`).join('');
      return `<div class="account-card"><div class="account-heading"><div><h3>${esc(meta.bind_label.replace(/^＋\s*/,''))}</h3><p>${esc(meta.bind_help)}</p></div></div><div class="account-column"><form data-p-bindform><div class="wx-group">${fields}</div><div class="account-bot-buttons" style="justify-content:flex-end"><button class="btn" type="button" data-p-bindcancel>取消</button><button class="btn primary" type="submit">验证并接入</button></div><div data-p-feedback class="inline-feedback hidden" role="status" aria-live="polite"></div></form></div></div>`;
    }

    function botCard(a){
      const ask=confirming&&confirming.id===a.id?confirming.kind:null;
      const box=ask==='off'?`<div class="bot-disable-confirm" role="group" aria-label="确认停用${esc(a.name)}"><p>停用后，这个机器人将停止接收${esc(L)}消息；已有任务和记录保留。</p><div><button class="btn" data-p-cancel>取消</button><button class="btn primary" data-p-confirm-off="${esc(a.id)}">确认停用</button></div></div>`
        :ask==='delete'?`<div class="bot-disable-confirm" role="group" aria-label="确认删除${esc(a.name)}"><p>${esc(tx(`删除后，这个${L}机器人不再接入本机 Bridge`))}，需要重新接入才能恢复；已有任务记录和工作文件夹保留。</p><div><button class="btn" data-p-cancel>取消</button><button class="btn primary" data-p-confirm-delete="${esc(a.id)}">确认删除</button></div></div>`:'';
      return `<div class="account-item account-bot"><div class="account-item-main"><strong>${esc(a.name)}</strong><span class="status ${esc(a.tone)}">${esc(a.runtime)}</span></div><p class="bot-folder" title="${esc(a.project_path||'')}">工作文件夹：${esc(a.project_name||'未设置')}</p>${a.error?`<p class="account-bot-warning">${esc(a.error)}</p>`:''}${a.pair_code?`<p class="account-bot-warning">${esc(tx(`在${L}里私聊这个机器人，发送：配对 ${a.pair_code}`))}</p>`:''}<div class="account-bot-actions"><label>任务执行 <select class="select bot-backend" data-p-backend="${esc(a.id)}" aria-label="${esc(a.name)}的任务执行工具"><option value="codex" ${a.execution_backend==='claude'?'':'selected'}>Codex CLI · GPT</option><option value="claude" ${a.execution_backend==='claude'?'selected':''}>Claude Code CLI</option></select></label><div class="account-bot-buttons"><button class="btn ghost" data-p-edit="${esc(a.id)}">设置</button><button class="btn ghost" data-p-toggle="${esc(a.id)}">${a.enabled?'停用':'重新启用'}</button><button class="btn ghost" data-p-delete="${esc(a.id)}">删除</button></div></div>${box}</div>`;
    }

    function render(){
      const d=data,n=d.accounts.length;
      const note=d.login&&!scanning(d)&&!binding&&d.login.status!=='confirmed'?`<p class="config-help">${esc(d.login.message)}</p>`:'';
      const top=scanning(d)?bindCard(d.login):binding?bindCard({}):note;
      const list=n?`<div class="account-card"><div class="account-heading"><div><h3>${esc(meta.label)}</h3></div><span>${n} 个机器人</span></div><div class="account-column account-bot-column"><h4>机器人</h4><div class="account-bot-list">${d.accounts.map(botCard).join('')}</div></div></div>`
        :(top?'':`<p class="config-help">${esc(tx(`还没有接入${L}。点击「${meta.bind_label.replace(/^＋\s*/,'')}」开始。`))}</p>`);
      q('[data-p-body]').innerHTML=top+list;
    }

    async function load(){
      clearTimeout(timer);
      if(state.page!=='p_'+meta.id)return;
      try{
        data=await api(base);
        const typing=root.contains(document.activeElement)&&document.activeElement.matches('input,select,textarea');
        if(!editing&&!typing)render();
      }catch(e){ if(!editing&&!binding)q('[data-p-body]').innerHTML=`<p class="config-help">${esc(e.message)}</p>`; }
      timer=setTimeout(load,data&&scanning(data)?1500:4000);
    }

    function view(edit){ editing=edit; q('[data-p-list]').classList.toggle('hidden',!!edit); q('[data-p-editor]').classList.toggle('hidden',!edit); if(!edit){dirty=false;load();} }

    function editor(a){
      view(a.id); dirty=false;
      let projectId=a.project_id;
      const ed=q('[data-p-editor]');
      ed.innerHTML=`<form data-p-form>
  <div class="wx-bar"><button class="wx-link" data-p-back type="button">‹ ${esc(tx(L+'账号'))}</button><strong>${esc(a.name)}</strong><button class="btn primary" data-p-save>保存</button></div>
  <div class="wx-group">
    <label class="wx-row"><span>名称</span><input data-p-name maxlength="60" value="${esc(a.name)}" required></label>
    <div class="wx-row"><span>工作文件夹</span><span class="wx-value" data-p-folder title="${esc(a.project_path||'')}">${esc(a.project_name||'未设置')}</span><button class="wx-link" data-p-pick type="button">更改…</button></div>
  </div>
  <div class="wx-group"><label class="wx-group-title">业务 Prompt</label><textarea data-p-prompt rows="9" spellcheck="false" placeholder="留空则直接发送消息原文">${esc(a.prompt)}</textarea></div>
  <p class="wx-note">${esc(tx(`${L}消息会填入 {{message}}。在${L}里说「记住…」会直接更新这里。`))}</p>
  <div data-p-feedback class="inline-feedback hidden" role="status" aria-live="polite"></div></form>`;
      const form=ed.querySelector('[data-p-form]'), report=(m,bad)=>{const f=ed.querySelector('[data-p-feedback]');f.textContent=m;f.className='inline-feedback'+(m?(bad?' error':' info'):' hidden');};
      form.oninput=()=>{dirty=true;};
      ed.querySelector('[data-p-back]').onclick=()=>{ if(dirty&&!confirm('放弃未保存的修改？'))return; view(null); };
      ed.querySelector('[data-p-pick]').onclick=async e=>{ const b=e.currentTarget; b.disabled=true;
        try{const r=await api('/api/projects/pick',{method:'POST',body:JSON.stringify({project_id:projectId})}); if(!r.cancelled){state.projects=r.projects;projectId=r.project.id;dirty=true;const f=ed.querySelector('[data-p-folder]');f.textContent=r.project.name;f.title=r.project.path;}}
        catch(err){report(err.message,true);} finally{b.disabled=false;} };
      form.onsubmit=async e=>{ e.preventDefault(); const s=ed.querySelector('[data-p-save]'); s.disabled=true;
        try{await patch(a.id,{name:ed.querySelector('[data-p-name]').value,project_id:projectId,prompt:ed.querySelector('[data-p-prompt]').value}); view(null); toast('已保存');}
        catch(err){report(err.message,true);s.disabled=false;} };
    }

    const patch=(id,body)=>api(base+'/accounts/'+encodeURIComponent(id),{method:'PATCH',body:JSON.stringify(body)});
    async function act(fn){ if(busy)return; busy=true; try{await fn();}catch(e){toast(e.message);} finally{busy=false; load();} }

    root.addEventListener('click',e=>{
      const b=e.target.closest('button'); if(!b||editing)return;
      const ds=b.dataset;
      if(ds.pBind!==undefined){ if(meta.bind_mode==='qr') act(()=>api(base+'/login',{method:'POST',body:'{}'})); else {binding=true; if(data)render();} }
      if(ds.pBindcancel!==undefined){ binding=false; render(); }
      if(ds.pVerify!==undefined) act(()=>api(base+'/login/verify',{method:'POST',body:JSON.stringify({code:q('[data-p-code]').value})}));
      if(ds.pEdit){ const a=data.accounts.find(x=>x.id===ds.pEdit); if(a)editor(a); }
      if(ds.pToggle){ const a=data.accounts.find(x=>x.id===ds.pToggle); if(a.enabled){confirming={id:a.id,kind:'off'};render();} else act(()=>patch(a.id,{enabled:true})); }
      if(ds.pDelete){ confirming={id:ds.pDelete,kind:'delete'}; render(); }
      if(ds.pCancel!==undefined){ confirming=null; render(); }
      if(ds.pConfirmOff) act(async()=>{confirming=null; await patch(ds.pConfirmOff,{enabled:false});});
      if(ds.pConfirmDelete) act(async()=>{confirming=null; await api(base+'/accounts/'+encodeURIComponent(ds.pConfirmDelete)+'/delete',{method:'POST',body:'{}'}); toast(tx('已删除这个'+L+'机器人'));});
    });
    root.addEventListener('submit',async e=>{
      if(!e.target.matches('[data-p-bindform]'))return;
      e.preventDefault();
      const body={}; e.target.querySelectorAll('[data-p-field]').forEach(i=>{body[i.dataset.pField]=i.value;});
      const fb=e.target.querySelector('[data-p-feedback]'), btn=e.target.querySelector('[type=submit]');
      btn.disabled=true; fb.textContent='正在验证…'; fb.className='inline-feedback info';
      try{ data=await api(base+'/bind',{method:'POST',body:JSON.stringify(body)}); binding=false; render(); toast(data.login?.message||'已接入'); }
      catch(err){ fb.textContent=err.message; fb.className='inline-feedback error'; btn.disabled=false; }
    });
    root.addEventListener('change',e=>{ const id=e.target.dataset?.pBackend; if(id){e.target.blur(); act(async()=>{await patch(id,{execution_backend:e.target.value}); toast('切换执行工具只影响新任务');});} });
    return {render:()=>{ if(!editing)load(); }};
  }

  api('/api/platforms').then(r=>{
    (r.platforms||[]).forEach(mount);
    const saved=window.initialBridgePage;
    if(saved&&pages[saved]&&state.page!==saved)switchPage(saved);
  }).catch(()=>{});
})();
