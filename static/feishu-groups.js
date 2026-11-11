/* Multiple group configurations, with credentials kept at application level. */
(() => {
 const node=id=>document.getElementById(id), safe=esc;
 const vm={catalog:{apps:[],groups:[],archived_accounts:[]},busy:false,group:null,app:null,linkJobId:null,accountId:null,accountToRemove:null,botToDisable:null,exportRef:null,groupDirty:false,appDirty:false,importKind:null,section:sessionStorage.getItem('feishuSection')==='assistants'?'assistants':'accounts'};
 const area=node('groupsWorkspace');
 function showSection(section){
  vm.section=section;sessionStorage.setItem('feishuSection',section);
  node('feishuPage').dataset.feishuView=section;
  for(const [key,id] of [['accounts','feishuAccountsTab'],['assistants','feishuAssistantsTab']]){
   node(id).classList.toggle('active',key===section);
   node(id).setAttribute('aria-selected',String(key===section));
  }
 }
 node('feishuAccountsTab').onclick=()=>showSection('accounts');
 node('feishuAssistantsTab').onclick=()=>showSection('assistants');
 showSection(vm.section);
 area.innerHTML=`<div class="groups-toolbar"><div><h2>飞书账号</h2><p>管理机器人和它们所在的群。</p></div><div class="groups-toolbar-actions"><input class="input bot-search" id="botSearch" type="search" placeholder="查找机器人" aria-label="查找机器人"><button class="btn ghost" id="openMigration">批量导出 / 导入</button><button class="btn" id="addApplication">接入已有机器人</button><button class="btn ghost" id="addApplicationManual">手动接入</button><button class="btn primary" id="addGroup">＋ 添加群</button></div></div>
 <p id="bridgeCli" class="bridge-cli">切换执行工具只影响新任务。</p>
 <section id="migrationPanel" class="group-editor hidden" aria-labelledby="migrationTitle"><div class="migration-heading"><h2 id="migrationTitle">迁移机器人</h2><button class="btn ghost" id="migrationBack" type="button">返回机器人列表</button></div><p class="config-help">也可以直接告诉当前 Agent：帮我把指定机器人迁到另一台电脑。</p><div class="migration-switch"><button class="btn active" id="migrationExportTab" type="button" aria-pressed="true">从这台电脑导出</button><button class="btn" id="migrationImportTab" type="button" aria-pressed="false">导入到这台电脑</button></div><div id="migrationExportFlow"><p class="config-help">选好机器人，下载配置包。配置包含机器人凭证，只保存在自己控制的设备上。</p><div class="migration-selection-tools"><button class="btn" id="migrationSelectAll" type="button">全选可迁移机器人</button><button class="btn" id="migrationClear" type="button">清空选择</button><span id="migrationSelectedCount">已选 0 个</span></div><div id="migrationChoices"></div><button class="btn primary" id="exportMigration" type="button">下载所选机器人配置</button><details id="migrationStopDetails" class="connection-details"><summary>新电脑已导入？停用这台电脑</summary><p class="config-help">在新电脑导入并核对设置，保持停用。然后在这里停用所选机器人，再去新电脑启用并发送测试消息。若新电脑未接通，可在这台电脑重新启用。</p><label class="check-label"><input id="migrationTargetVerified" type="checkbox"> 新电脑已导入并核对配置</label><button class="btn" id="stopMigrated" type="button">停用这台电脑上的所选机器人</button></details></div><div id="migrationImportFlow" class="hidden"><p class="config-help">选择从旧电脑下载的配置包。机器人导入后先保持停用，核对文件夹和执行工具，再切换接收。</p><label for="migrationFile">机器人配置包</label><input id="migrationFile" type="file" accept=".json,application/json"><p id="migrationFileSummary" class="config-help"></p><label for="migrationTarget">导入后使用的工作文件夹</label><select class="input" id="migrationTarget"></select><button class="mini-link folder-add-link" id="migrationPickFolder" type="button">从本机选择文件夹…</button><details class="folder-fallback" id="migrationFolderFallback"><summary>手动输入路径</summary><div class="folder-entry-row"><input class="input" id="migrationFolderPath" placeholder="/Users/你的名字/工作文件夹" aria-label="本机文件夹路径"><button class="btn" id="migrationUseFolder" type="button">添加并选中</button></div></details><div id="migrationLegacyPassword" class="hidden"><label for="migrationImportPassword">旧加密包的迁移密码</label><input class="input" id="migrationImportPassword" type="password" autocomplete="off"></div><button class="btn primary" id="importMigration" type="button">导入并保持停用</button></div><div id="migrationFeedback" class="inline-feedback hidden" role="status"></div></section>
 <div id="groupsNotice" class="inline-feedback hidden" role="status" aria-live="polite"></div>
 <div id="groupList"></div><section id="groupEditor" class="group-editor hidden"></section><section id="applicationEditor" class="group-editor hidden"></section><section id="existingLink" class="group-editor hidden"></section>
 <span id="appCount" class="hidden"></span>
 <dialog id="accountNameDialog" class="account-name-dialog" aria-labelledby="accountNameTitle"><h3 id="accountNameTitle">设置账号名称</h3><p>给这个飞书账号起个容易认出的名字。机器人和群配置不会改变。</p><form id="accountNameForm"><label for="accountNameInput">账号名称</label><input class="input" id="accountNameInput" maxlength="60" required placeholder="例如：工作飞书"><div id="accountNameFeedback" class="inline-feedback hidden" role="status"></div><div class="account-name-actions"><button class="btn" type="button" id="cancelAccountName">取消</button><button class="btn primary" type="submit">保存名称</button></div></form></dialog>
 <dialog id="accountRemoveDialog" class="account-name-dialog" aria-labelledby="accountRemoveTitle"><h3 id="accountRemoveTitle">移除飞书账号</h3><p id="accountRemoveDescription"></p><p>移除后，这些机器人会停止从飞书接收新消息，群连接也会停用。历史任务和文件保留，可在此页面恢复。飞书里的账号和机器人不会删除。</p><div class="account-name-actions"><button class="btn" type="button" id="cancelAccountRemove">取消</button><button class="btn primary" type="button" id="confirmAccountRemove">确认移除</button></div></dialog>
 <dialog id="exportConfigDialog" class="export-config-dialog" aria-labelledby="exportConfigTitle"><h3 id="exportConfigTitle">导出配置</h3><p id="exportConfigName"></p><div class="export-config-options"><button class="export-config-option" id="exportWithBot" type="button"><strong>带飞书机器人信息</strong><span>迁到另一台电脑继续用这个机器人。包含应用凭证、私聊名单和群绑定。</span></button><button class="export-config-option" id="exportWithoutBot" type="button"><strong>不带飞书机器人信息</strong><span>只复制助手的工作规则，给另一位机器人使用。不包含飞书凭证和群绑定。</span></button></div><div id="exportConfigFeedback" class="inline-feedback hidden" role="status"></div><div class="account-name-actions"><button class="btn" id="cancelExportConfig" type="button">取消</button></div></dialog>`;
 node('migrationTitle').textContent='导出与导入';
 node('migrationImportFlow').insertAdjacentHTML('afterbegin',`<div id="templateTargetRow" class="hidden"><label for="templateTargetBot">复制到哪位机器人</label><select class="input" id="templateTargetBot"></select><p class="config-help">只替换这位机器人的默认工作规则。飞书身份、允许私聊的人和群设置保持原样；执行工具沿用目标机器人。</p></div>`);
 node('migrationTarget').previousElementSibling.id='migrationTargetLabel';
 node('migrationImportFlow').querySelector(':scope > p.config-help').id='migrationImportHelp';
 const report=(id,message='',error=false)=>{const n=node(id);if(n){n.textContent=message;n.className='inline-feedback'+(message?(error?' error':' success'):' hidden');}};
 function app(ref){return vm.catalog.apps.find(x=>x.ref===ref);}
 function botStatus(a){
  if(!a.enabled||a.runtime==='已停用')return {label:'已停用',tone:'wait'};
  if(a.runtime==='已连接飞书')return {label:'已连接',tone:'done'};
  if(a.runtime==='正在连接飞书')return {label:'连接中',tone:'wait'};
  if(a.runtime==='配置错误')return {label:'配置错误',tone:'fail'};
  if(a.runtime==='等待启动')return {label:'等待启动',tone:'wait'};
  if(a.runtime==='未配置')return {label:'未配置',tone:'fail'};
  return {label:a.runtime||'状态未知',tone:'wait'};
 }
 function projectOptions(selected){return '<option value="">请选择工作文件夹</option>'+state.projects.map(p=>`<option value="${safe(p.id)}" ${p.id===selected?'selected':''}>${safe(p.name)} · ${safe(p.path)}</option>`).join('');}
 function folderField(prefix){return `<p class="config-help folder-selected-path" id="${prefix}SelectedPath"></p><button class="mini-link folder-add-link" type="button" id="${prefix}PickFolder">从本机选择文件夹…</button><details class="folder-fallback" id="${prefix}FolderFallback"><summary>手动输入路径</summary><div class="folder-entry"><label for="${prefix}FolderPath">本机文件夹路径</label><div class="folder-entry-row"><input class="input" id="${prefix}FolderPath" placeholder="/Users/你的名字/工作文件夹" aria-label="本机文件夹路径"><button class="btn" id="${prefix}UseFolder" type="button">添加并选中</button></div><p class="config-help">文件夹需要已存在。选中后，再保存${prefix==='group'?'本群':'机器人'}设置。</p></div></details>`;}
 function bindFolderField(prefix,selectId,feedbackId){
  const showPath=()=>{node(prefix+'SelectedPath').textContent=state.projects.find(p=>p.id===node(selectId).value)?.path||'';};
  const choose=project=>{
   node(selectId).innerHTML=projectOptions(project.id);
   node(selectId).value=project.id;
   if(prefix==='group')vm.groupDirty=true;else vm.appDirty=true;
   showPath();
   report(feedbackId,`已选中「${project.name}」，点击保存后用于新任务。`);
  };
  node(selectId).addEventListener('change',showPath);showPath();
  node(prefix+'PickFolder').onclick=()=>operation(async()=>{
   const button=node(prefix+'PickFolder'),label=button.textContent;
   button.textContent='请在弹出的窗口中选择文件夹…';report(feedbackId,'正在打开本机文件夹选择窗口…');
   try{
    const result=await api('/api/projects/pick',{method:'POST',body:JSON.stringify({project_id:node(selectId).value})});
    if(result.cancelled){report(feedbackId,'未更改工作文件夹。');return;}
    state.projects=result.projects;choose(result.project);
   }catch(err){node(prefix+'FolderFallback').open=true;report(feedbackId,err.message,true);}
   finally{button.textContent=label;}
  });
  node(prefix+'FolderPath').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();node(prefix+'UseFolder').click();}};
  node(prefix+'UseFolder').onclick=()=>operation(async()=>{try{
   const path=node(prefix+'FolderPath').value.trim();
   report(feedbackId,'正在检查文件夹…');
   const project=await api('/api/projects',{method:'POST',body:JSON.stringify({path})});
   state.projects=await api('/api/projects');
   node(prefix+'FolderFallback').open=false;choose(project);
  }catch(err){report(feedbackId,err.message,true);}});
 }
 function applicationOptions(selected){return '<option value="">请选择飞书应用</option>'+vm.catalog.apps.map(a=>`<option value="${safe(a.ref)}" ${a.ref===selected?'selected':''}>${safe(a.name)} · ${safe(a.app_id)}</option>`).join('');}
 function lists(){
  window.dispatchEvent(new CustomEvent('bridge:feishu-connections',{detail:{
   ids:vm.catalog.managed_connection_ids||[...vm.catalog.apps.map(a=>a.connection_id),...vm.catalog.groups.map(g=>g.connection_id)],
   names:[...vm.catalog.apps.map(a=>a.name),...vm.catalog.groups.map(g=>g.name)].filter(Boolean)
  }}));
  const query=(node('botSearch')?.value||'').trim().toLocaleLowerCase();
  const accounts=new Map();
  vm.catalog.apps.forEach((a,i)=>{const id=a.account_id||'unverified:'+a.ref;if(!accounts.has(id))accounts.set(id,{id,verified:a.account_verified,name:a.account_name||'',apps:[],groups:[]});accounts.get(id).apps.push({value:a,index:i});});
  vm.catalog.groups.forEach((g,i)=>{const a=app(g.app_ref),account=accounts.get(a?.account_id||'unverified:'+g.app_ref);if(account)account.groups.push({value:g,index:i});});
  const accountHtml=Array.from(accounts.values()).sort((a,b)=>Number(b.verified)-Number(a.verified)||a.id.localeCompare(b.id)).map(account=>{
   const title=safe(account.name||(account.verified?account.apps[0].value.name+' 所在账号':account.apps[0].value.name+' 的账号待确认'));
   const visibleApps=account.apps.filter(({value:a})=>!query||account.name.toLocaleLowerCase().includes(query)||a.name.toLocaleLowerCase().includes(query));
   if(query&&!visibleApps.length)return '';
  const bots=visibleApps.map(({value:a,index:i})=>{const status=botStatus(a);return `<div class="account-item account-bot"><div class="account-item-main"><strong>${safe(a.name)}</strong><span class="status ${status.tone}">${safe(status.label)}</span></div><p class="bot-folder" title="${safe(a.project_path||'')}">工作文件夹：${safe(a.project_name||'未设置')}</p><div class="account-bot-actions"><label>任务执行 <select class="select bot-backend" data-backend-app="${safe(a.ref)}" aria-label="${safe(a.name)}的任务执行工具"><option value="codex" ${a.execution_backend==='claude'?'':'selected'}>Codex CLI · GPT</option><option value="claude" ${a.execution_backend==='claude'?'selected':''}>Claude Code CLI</option></select></label><div class="account-bot-buttons"><button class="btn ghost" data-edit-app="${i}">设置</button><button class="btn ghost" data-export-bot="${safe(a.ref)}" ${a.migration_supported?'':'disabled title="此类助手需要单独迁移管理记录"'}>导出配置</button><button class="btn ghost" data-toggle-bot="${safe(a.ref)}">${a.enabled?'停用':'重新启用'}</button></div></div>${vm.botToDisable===a.ref&&a.enabled?`<div class="bot-disable-confirm" role="group" aria-label="确认停用${safe(a.name)}"><p>停用后，这个机器人将停止接收飞书消息；已有任务和记录保留。正在处理的任务不会取消，停用期间暂不回传结果。</p><div><button class="btn" data-cancel-disable-bot="${safe(a.ref)}">取消</button><button class="btn primary" data-confirm-disable-bot="${safe(a.ref)}">确认停用</button></div></div>`:''}</div>`;}).join('');
   const chats=new Map();
   account.groups.forEach(route=>{const id=route.value.chat_id;if(!chats.has(id))chats.set(id,[]);chats.get(id).push(route);});
   const groups=Array.from(chats.values()).map(routes=>{
    const enabled=routes.filter(({value:g})=>g.enabled&&app(g.app_ref)?.enabled).length;
    const name=routes.find(({value:g})=>!g.name.startsWith('已接入群 · ')&&!g.name.includes(' · '))?.value.name||routes[0].value.name;
    const detail=routes.map(({value:g,index:i})=>`<div class="account-group-route"><p><strong>${safe(g.app_name)}</strong><br><span title="${safe(g.project_path||'')}">工作文件夹：${safe(g.project_name)}</span></p><button class="btn ghost" data-edit-group="${i}">设置</button></div>`).join('');
    return `<div class="account-item"><div class="account-item-main"><strong>${safe(name)}</strong><span class="status ${enabled===routes.length?'done':'wait'}">${enabled===routes.length?'已启用':enabled?'部分停用':'已停用'}</span></div><p>${routes.length} 个机器人</p>${detail}</div>`;
   }).join('')||'<p class="account-empty">这个账号还没有接入群。</p>';
   return `<section class="account-card"><div class="account-heading"><div><h3>${title}</h3><div class="account-heading-actions"><button class="mini-link" data-name-account="${safe(account.id)}">${account.name?'修改名称':'设置名称'}</button><button class="mini-link account-remove-link" data-remove-account="${safe(account.id)}">移除账号</button></div></div><span>${account.apps.length} 个机器人 · ${chats.size} 个群</span></div><div class="account-column account-bot-column"><h4>机器人</h4><div class="account-bot-list">${bots}</div></div><details class="account-groups" data-account-groups="${safe(account.id)}"><summary>群 · ${chats.size}</summary><div class="account-group-list">${groups}</div></details></section>`;
  }).join('')||(query?'<section class="empty-card"><h2>没有找到匹配的机器人</h2><p>换个名称试试。</p></section>':'<section class="empty-card"><h2>还没有接入机器人</h2><p>可以在上方创建助手，或接入已有机器人。</p></section>');
  const archived=vm.catalog.archived_accounts||[];
  const archivedHtml=archived.length?`<details id="archivedAccounts" class="archived-accounts"><summary>已移除的账号（${archived.length}）</summary>${archived.map(a=>`<div class="archived-account"><div><strong>${safe(a.name)}</strong><p>${a.app_count} 个机器人 · ${a.group_count} 条群连接</p></div><button class="btn ghost" data-restore-account="${safe(a.account_id)}" ${a.state==='removed'?'':'disabled'}>${a.state==='removed'?'恢复账号':'处理中'}</button></div>`).join('')}</details>`:'';
  const wasOpen=node('archivedAccounts')?.open||false;
  const openGroups=new Set(Array.from(area.querySelectorAll('details[data-account-groups]')).filter(x=>x.open).map(x=>x.dataset.accountGroups));
  setHtmlIfChanged('groupList',accountHtml+archivedHtml);
  if(node('archivedAccounts'))node('archivedAccounts').open=wasOpen;
  area.querySelectorAll('details[data-account-groups]').forEach(x=>x.open=openGroups.has(x.dataset.accountGroups));
  node('appCount').textContent=`（${vm.catalog.apps.length}）`;
 }
 async function fillGroupPurpose(g){const ref=g.app_ref;try{const defaults=await api('/api/feishu/group-defaults',{method:'POST',body:JSON.stringify({app_ref:ref})});if(vm.group===g&&g.app_ref===ref&&!vm.promptDirty){node('groupPrompt').value=defaults.prompt_template;validateGroup();}}catch(e){if(vm.group===g)report('groupFeedback',e.message,true);}}
 async function refreshCatalog(){try{vm.catalog=await api('/api/feishu/catalog');area.classList.remove('hidden');node('feishuPage').classList.add('multi-groups');lists();pairView();}catch(e){window.dispatchEvent(new CustomEvent('bridge:feishu-connections',{detail:{error:e.message}}));report('groupsNotice',e.message,true);}}
 async function operation(fn){if(vm.busy)return;vm.busy=true;area.querySelectorAll('button').forEach(b=>b.disabled=true);try{await fn();}finally{vm.busy=false;area.querySelectorAll('button').forEach(b=>b.disabled=false);validateGroup();validateApp();}}
 function view(which){node('groupList').classList.toggle('hidden',!!which);node('bridgeCli').classList.toggle('hidden',!!which);node('migrationPanel').classList.toggle('hidden',which!=='migration');node('groupEditor').classList.toggle('hidden',which!=='group');node('applicationEditor').classList.toggle('hidden',which!=='app');node('existingLink').classList.toggle('hidden',which!=='link');node('botSearch').classList.toggle('hidden',!!which);node('openMigration').classList.toggle('hidden',!!which);node('addGroup').classList.toggle('hidden',!!which);node('addApplication').classList.toggle('hidden',!!which);node('addApplicationManual').classList.toggle('hidden',!!which);}
 function leave(){if((vm.groupDirty||vm.appDirty)&&!confirm('有未保存的修改，确定放弃？'))return;vm.group=null;vm.app=null;vm.groupDirty=false;vm.appDirty=false;view(null);}
 function groupEditor(group){
  vm.group=group?{...group,existing:true}:{app_ref:vm.catalog.apps[0]?.ref||'',chat_id:'',name:'',project_id:'',prompt_template:'飞书任务：{{message}}\n\n请执行任务，并说明结果。',enabled:true,topic_collaboration:true,existing:false};
  const g=vm.group,a=app(g.app_ref);g.revision=a?.revision;
  if(!group){g.project_id=state.connections.find(c=>c.id===a?.connection_id)?.project_id||'';}
  vm.groupDirty=false;vm.promptDirty=false;view('group');node('groupEditor').innerHTML=`<button class="mini-link group-back" id="groupBack">← 返回群配置</button><h2>${g.existing?'编辑群配置':'添加群配置'}</h2><form id="groupForm">
  <div class="field"><label for="groupName">群名称</label><input class="input" id="groupName" maxlength="60" value="${safe(g.name)}" placeholder="${g.existing?'例如：翻译群':'保存时自动读取，也可以自己填写'}" ${g.existing?'required':''}></div>
  <div class="field"><label for="groupApplication">飞书应用</label><select class="input" id="groupApplication" ${g.existing?'disabled':''} required>${applicationOptions(g.app_ref)}</select>${g.existing?'':'<button class="mini-link new-app-link" type="button" id="newAppFromGroup">使用其他应用？添加应用</button>'}</div>
  <div id="groupPairing"></div>
  <div class="field"><label for="groupProject">本群工作文件夹</label><select class="input" id="groupProject" required>${projectOptions(g.project_id)}</select>${folderField('group')}<p class="config-help">只影响这个群的新任务；其他群和机器人私聊继续使用各自的文件夹。</p></div>
  <div class="field"><label for="groupPrompt">本群 Prompt</label><textarea class="input" id="groupPrompt" rows="10" spellcheck="false" aria-describedby="groupPromptHelp groupFeedback">${safe(g.prompt_template)}</textarea><p class="config-help" id="groupPromptHelp">保留一个 <code>{{message}}</code>，自动填入本群消息。保存只影响新任务。群成员也可以在群里 @助手，直接说明以后在本群怎么做；不会修改其他群或私聊。</p>${g.self_update_rule?`<details class="connection-details"><summary>在群里更新的规则</summary><p style="white-space:pre-wrap">${safe(g.self_update_rule)}</p></details>`:''}</div>
  ${vm.catalog.capabilities?.group_topics?`<details class="connection-details"><summary>群聊高级设置</summary><label class="check-label"><input type="checkbox" id="groupTopics" ${g.topic_collaboration?'checked':''}> 允许群内继续讨论和助手协作</label><p class="config-help">开启后，@ 助手发起任务，群成员可回复相关消息继续讨论，已接入本群的助手可分工。关闭后仍可 @ 助手发起任务。保存时会检查群消息权限。</p></details>`:''}
  <label class="check-label"><input type="checkbox" id="groupEnabled" ${g.enabled?'checked':''}> 启用这个群的消息接收</label>
  <div class="group-save-row"><button class="btn primary" id="saveGroup" type="submit">${g.existing?'保存本群设置':'确认添加这个群'}</button><span>不会修改其他群或历史任务</span></div><div id="groupFeedback" class="inline-feedback hidden" role="status" aria-live="polite"></div></form>`;
  node('groupBack').onclick=leave;
  bindFolderField('group','groupProject','groupFeedback');
  node('groupForm').oninput=e=>{vm.groupDirty=true;if(e.target.id==='groupPrompt')vm.promptDirty=true;report('groupFeedback');validateGroup();};
  node('groupApplication').onchange=()=>{g.app_ref=node('groupApplication').value;g.chat_id='';g.revision=app(g.app_ref)?.revision;const selected=app(g.app_ref);node('groupProject').value=state.connections.find(c=>c.id===selected?.connection_id)?.project_id||'';if(vm.catalog.capabilities?.group_topics)fillGroupPurpose(g);pairView();validateGroup();};
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
   if(c?.chat_type==='group'){g.chat_id=c.chat_id;html=`<div class="bound-group">已识别目标群：<code>${safe(c.chat_id)}</code><p>保存时会读取飞书群名；读取不到时再手动填写。点击下方“确认添加这个群”后，全群成员可用。</p></div>`;}
   else {g.chat_id='';html=`<div class="bound-group">在目标群 <strong>@${safe(a.bot_name||'机器人')} ${safe(a.pairing_code)}</strong><p>${c?'收到的是私聊消息，请改在目标群发送。':'发送后，这里会自动显示识别结果。'} 配对码有效期至 ${new Date(a.pairing_expires_at*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'})}。</p></div>`;}
   html+='<button class="btn" type="button" id="groupPair">重新生成配对码</button>';
  }else{g.chat_id='';html='<p class="config-help">把机器人加入目标群，再用配对码识别这个群。</p><button class="btn" type="button" id="groupPair">生成群配对码</button>';}
  if(!a.enabled)html+='<p class="config-help">应用已停用；生成配对码会启动本应用的消息接收。</p>';
  html+='<details class="group-help"><summary>飞书后台如何配置？</summary><p>启用机器人；在“事件与回调”选择长连接，订阅 im.message.receive_v1，开通群内 @机器人消息和机器人回复权限，再发布应用。同一应用不要在其他接收器重复建立长连接。</p></details>';
  setHtmlIfChanged('groupPairing',html);
  const action=kind=>operation(async()=>{try{vm.catalog=await api('/api/feishu/app-pairing',{method:'POST',body:JSON.stringify({app_ref:g.app_ref,action:kind})});g.revision=app(g.app_ref)?.revision;lists();pairView();}catch(e){report('groupFeedback',e.message,true);}});
  if(node('groupPair'))node('groupPair').onclick=()=>action('start');if(node('groupCheck'))node('groupCheck').onclick=()=>action('check');validateGroup();
 }
 function validateGroup(){if(!vm.group||!node('saveGroup'))return false;const v=node('groupPrompt').value;const valid=v.trim()&&v.length<=8000&&v.split('{{message}}').length===2;node('groupPrompt').setAttribute('aria-invalid',String(!valid));const ready=!!(valid&&(!vm.group.existing||node('groupName').value.trim())&&node('groupProject').value&&vm.group.chat_id&&vm.group.app_ref);node('saveGroup').disabled=vm.busy||!ready;if(!valid)report('groupFeedback','Prompt 需为 1 到 8000 字，且恰好保留一个 {{message}}。',true);return ready;}
 function renderExistingJob(j){
  const target=node('existingLinkProgress');if(!target)return;
  if(j.phase==='already_connected'){target.innerHTML=`<p>「${safe(j.name)}」已经接入 Bridge，原连接和处理规则保持不变。</p>`;return;}
  const status={planning:'正在准备',registering:'正在准备授权',waiting_scan:'等你确认',connecting:'正在接入',ready:'已接入',needs_check:'授权未完成',failed:'接入未完成'}[j.phase]||j.phase;
  let action='';
  if(j.phase==='waiting_scan'&&j.qr_url){action=`<div class="assistant-scan"><img src="${safe(j.qr_url)}" alt="飞书已有应用授权二维码" width="280" height="280"><div><p>用飞书扫码，确认授权「${safe(j.expected_app_id)}」这个已有应用。这里不会创建新应用。</p>${j.verification_url?`<a class="btn" href="${safe(j.verification_url)}" target="_blank" rel="noopener noreferrer">打开确认页面</a>`:''}</div></div>`;}
  if(['failed','needs_check'].includes(j.phase)&&j.expected_app_id)action=`<button class="btn" id="retryExistingLink" type="button">继续接入</button>`;
  if(j.phase==='ready')action=`<p>已接入现有项目。请在飞书给这位机器人发送一条非敏感消息，确认它能收到并回复。</p>`;
  target.innerHTML=`<div class="assistant-card"><h3>${safe(status)}</h3><p>${safe(j.message||'')}</p>${action}</div>`;
  if(node('retryExistingLink'))node('retryExistingLink').onclick=async()=>{try{await api(`/api/feishu/onboarding/${j.id}/retry`,{method:'POST',body:'{}'});await pollExistingLink();}catch(e){report('existingLinkFeedback',e.message,true);}};
 }
 async function pollExistingLink(){
  if(!vm.linkJobId||node('existingLink').classList.contains('hidden'))return;
  try{const j=await api('/api/feishu/onboarding/existing/'+vm.linkJobId);renderExistingJob(j);
   if(j.phase==='ready'){vm.linkJobId=null;await refreshCatalog();}
  }catch(e){report('existingLinkFeedback',e.message,true);}
 }
 async function openExistingLink(){
  view('link');node('existingLink').innerHTML='<p>正在查找本机已有的飞书应用…</p>';
  try{
   const d=await api('/api/feishu/onboarding/existing');
   const available=d.candidates.filter(x=>!x.connected);
   node('existingLink').innerHTML=`<button class="mini-link group-back" id="existingLinkBack">← 返回飞书账号</button><h2>接入飞书已有机器人</h2><p class="config-help">选择已经创建的飞书应用，授权它连接 Bridge。不会新建飞书应用或助手。</p>
   <form id="existingLinkForm"><div class="field"><label for="existingAppChoice">已有飞书应用</label><select class="input" id="existingAppChoice"><option value="">${available.length?'选择本机已保存的应用':'本机还没有未接入的应用'}</option>${available.map(x=>`<option value="${safe(x.app_id)}">${safe(x.profile)} · ${safe(x.app_id)}</option>`).join('')}</select><p class="config-help">列表来自本机飞书工具。找不到时，在下方填写已有应用的 App ID。</p></div>
   <div class="field"><label for="existingAppId">其他已有应用的 App ID（可选）</label><input class="input" id="existingAppId" placeholder="cli_…" autocomplete="off"><p class="config-help">填写后只会打开这个应用的授权页，不能创建新应用。</p></div>
   <div class="field"><label for="existingProject">交给哪个已有助手或项目处理</label><select class="input" id="existingProject" required>${projectOptions('')}</select><p class="config-help">Bridge 会把新消息交给这里，不会新建助手或项目。飞书里原有的业务规则不会自动复制到 Bridge。</p></div>
   <div class="group-save-row"><button class="btn primary" type="submit">连接这位机器人</button></div><div id="existingLinkFeedback" class="inline-feedback hidden" role="status"></div></form><div id="existingLinkProgress" aria-live="polite"></div>`;
   node('existingLinkBack').onclick=leave;
   node('existingLinkForm').onsubmit=async e=>{e.preventDefault();if(vm.busy)return;
    const app_id=(node('existingAppId').value.trim()||node('existingAppChoice').value),project_id=node('existingProject').value;
    if(!/^cli_[a-z0-9]{16}$/.test(app_id)||!project_id){report('existingLinkFeedback','请选择已有应用和运行项目。',true);return;}
    vm.busy=true;report('existingLinkFeedback','正在准备已有应用的授权页…');
    try{const j=await api('/api/feishu/onboarding/existing',{method:'POST',body:JSON.stringify({app_id,project_id})});report('existingLinkFeedback');
     if(j.phase==='already_connected'){renderExistingJob(j);await refreshCatalog();}
     else{vm.linkJobId=j.id;renderExistingJob(j);}
    }catch(err){report('existingLinkFeedback',err.message,true);}finally{vm.busy=false;}
   };
   const pending=d.jobs.find(j=>!['ready','failed','needs_check','removed'].includes(j.phase)&&j.expected_app_id);
   if(pending){vm.linkJobId=pending.id;node('existingAppId').value=pending.expected_app_id;node('existingProject').value=pending.project_id||'';renderExistingJob(pending);}
  }catch(e){node('existingLink').innerHTML=`<button class="mini-link group-back" id="existingLinkBack">← 返回飞书账号</button><p class="inline-feedback error">${safe(e.message)}</p>`;node('existingLinkBack').onclick=leave;}
 }
 function applicationEditor(existing,fromGroup=false){
  const a=existing||{name:'',app_id:'',prompt_template:'飞书任务：{{message}}\n\n请执行任务，并说明结果。',allowed_open_ids:[],enabled:false};vm.app={...a,fromGroup};vm.appDirty=false;view('app');
  const project=a.project_id||state.connections.find(c=>c.id===a.connection_id)?.project_id||state.projects[0]?.id||'';
  node('applicationEditor').innerHTML=`<button class="mini-link group-back" id="appBack">← ${fromGroup?'返回群配置编辑':'返回群配置'}</button><h2>${a.ref?'机器人设置':'接入飞书机器人'}</h2><p class="config-help">同一机器人用于多个群时，直接选择已有机器人，无需重复填写凭据。</p><form id="applicationForm">
  <div class="field"><label for="appLabel">应用名称</label><input class="input" id="appLabel" maxlength="60" value="${safe(a.name)}" placeholder="例如：公司 A 工作助手" required></div>
  <div class="field"><label for="applicationId">App ID</label><input class="input" id="applicationId" value="${safe(a.app_id)}" ${a.ref?'readonly':''} placeholder="cli_…" required></div>
  ${a.ref?`<details class="connection-details"><summary>连接信息</summary><p>飞书操作 CLI：${a.cli_profile?`<code>lark-cli --profile ${safe(a.cli_profile)}</code>`:'未检测到 lark-cli 配置'}</p><p>消息接收：Bridge 长连接 · ${safe(a.runtime||'未启动')}</p></details>`:''}
  <div class="field"><label for="applicationSecret">App Secret</label><input class="input" type="password" autocomplete="new-password" id="applicationSecret" placeholder="${a.has_secret?'留空保留已有密钥':'从飞书开发者后台复制'}" ${a.has_secret?'':'required'}></div>
  ${a.ref?`<label class="check-label"><input type="checkbox" id="applicationEnabled" ${a.enabled?'checked':''}> 启用本应用的消息接收（影响本应用全部群）</label>`:''}
  <div class="field"><label for="applicationProject">机器人工作文件夹</label><select class="input" id="applicationProject" required>${projectOptions(project)}</select>${folderField('application')}<p class="config-help">用于这个机器人的私聊及新群默认值。已有群使用各自设置的文件夹。</p></div>
  <details class="private-settings"><summary>私聊设置（可选）</summary><p class="config-help">默认只接收已添加的群。需要私聊使用时，再添加允许的用户和 Prompt。</p><div class="field"><label for="applicationUsers">允许私聊的用户 Open ID</label><textarea class="input" id="applicationUsers" rows="2" placeholder="每行一个 ou_…；留空则不接收私聊">${safe(a.allowed_open_ids.join('\n'))}</textarea></div><div class="field"><label for="applicationPrompt">私聊 Prompt</label><textarea class="input" id="applicationPrompt" rows="5">${safe(a.prompt_template)}</textarea><p class="config-help">保留一个 {{message}}。创建者也可以私聊助手，直接说明以后怎么做；会影响这位助手的全部新任务。</p>${a.self_update_rule?`<details class="connection-details"><summary>在对话中更新的规则</summary><p style="white-space:pre-wrap">${safe(a.self_update_rule)}</p></details>`:''}</div></details>
  <div class="group-save-row"><button class="btn primary" id="saveApplication">${a.ref?'保存机器人设置':'验证并接入'}</button></div><div id="applicationFeedback" class="inline-feedback hidden" role="status" aria-live="polite"></div></form>`;
  node('appBack').onclick=()=>{if(vm.appDirty&&!confirm('放弃未保存的机器人设置？'))return;vm.appDirty=false;vm.app=null;if(fromGroup){view('group');pairView();}else leave();};
  bindFolderField('application','applicationProject','applicationFeedback');
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
 function validateApp(){if(!vm.app||!node('saveApplication'))return false;const text=node('applicationPrompt').value;const valid=text.trim()&&text.length<=8000&&text.split('{{message}}').length===2;node('applicationPrompt').setAttribute('aria-invalid',String(!valid));node('saveApplication').disabled=vm.busy||!valid||!node('applicationProject').value;if(!valid){node('applicationPrompt').closest('details').open=true;report('applicationFeedback','私聊 Prompt 需为 1 到 8000 字，且恰好包含一个 {{message}}。',true);}return !!(valid&&node('applicationProject').value);}
 async function toggleBot(ref,enabled){
  const a=app(ref);if(!a||vm.busy)return;
  await operation(async()=>{try{
   vm.catalog=await api('/api/feishu/apps/enabled',{method:'POST',body:JSON.stringify({app_ref:a.ref,revision:a.revision,enabled})});
   vm.botToDisable=null;lists();window.dispatchEvent(new CustomEvent('bridge:assistants-changed'));
   report('groupsNotice',enabled?`「${a.name}」已重新启用，正在连接飞书。`:`「${a.name}」已停用，其他机器人不受影响。`);
  }catch(err){report('groupsNotice',err.message,true);}});
 }
 async function exportBots(selected,feedbackId,fileBase){
  if(vm.busy)return;
  vm.busy=true;report(feedbackId,'正在准备配置包…');
  try{
   const result=await api('/api/feishu/migration/export',{method:'POST',body:JSON.stringify({selected})});
   const blob=new Blob([JSON.stringify(result.bundle)],{type:'application/json'}),url=URL.createObjectURL(blob),link=document.createElement('a');
   link.href=url;link.download=`${fileBase}-${new Date().toISOString().slice(0,10)}.json`;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
   report(feedbackId,`已导出 ${result.count} 个机器人。配置包含机器人凭证，请只保存在自己的设备上。${result.warnings?.join('；')||''}`);
   return true;
  }catch(e){report(feedbackId,e.message,true);return false;}finally{vm.busy=false;}
 }
 async function exportAssistant(a,feedbackId){
  if(vm.busy)return;
  vm.busy=true;report(feedbackId,'正在准备助手配置…');
  try{
   const result=await api('/api/feishu/assistant-template/export',{method:'POST',body:JSON.stringify({ref:a.ref,revision:a.revision})});
   const blob=new Blob([JSON.stringify(result.bundle)],{type:'application/json'}),url=URL.createObjectURL(blob),link=document.createElement('a');
   link.href=url;link.download=`assistant-config-${new Date().toISOString().slice(0,10)}.json`;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);
   report(feedbackId,`已导出「${a.name}」的助手配置。文件不含飞书凭证和群绑定；工作文件夹需要在目标电脑重新选择。`);
   return true;
  }catch(e){report(feedbackId,e.message,true);return false;}finally{vm.busy=false;}
 }
 function closeExportDialog(){node('exportConfigDialog').close();vm.exportRef=null;}
 node('cancelExportConfig').onclick=closeExportDialog;
 node('exportConfigDialog').addEventListener('close',()=>{vm.exportRef=null;report('exportConfigFeedback');});
 node('exportWithBot').onclick=async()=>{
  const a=app(vm.exportRef);if(!a||vm.busy)return;
  const success=await exportBots([{ref:a.ref,revision:a.revision}],'exportConfigFeedback','feishu-bot-private');
  if(success){closeExportDialog();report('groupsNotice',`「${a.name}」的机器人配置已下载。文件包含飞书凭证，请只保存在自己的设备上。`);}
 };
 node('exportWithoutBot').onclick=async()=>{
  const a=app(vm.exportRef);if(!a||vm.busy)return;
  const success=await exportAssistant(a,'exportConfigFeedback');
  if(success){closeExportDialog();report('groupsNotice',`「${a.name}」的助手配置已下载，不包含飞书机器人信息。`);}
 };
 area.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;
  if(b.dataset.exportBot!==undefined){const a=app(b.dataset.exportBot);if(a&&a.migration_supported){vm.exportRef=a.ref;node('exportConfigName').textContent=`${a.name}：选择要下载的内容`;report('exportConfigFeedback');node('exportConfigDialog').showModal();node('cancelExportConfig').focus();}return;}
  if(b.dataset.toggleBot!==undefined){const a=app(b.dataset.toggleBot);if(!a)return;if(a.enabled){vm.botToDisable=a.ref;lists();area.querySelector('[data-confirm-disable-bot="'+CSS.escape(a.ref)+'"]')?.focus();}else toggleBot(a.ref,true);return;}
  if(b.dataset.cancelDisableBot!==undefined){vm.botToDisable=null;lists();return;}
  if(b.dataset.confirmDisableBot!==undefined){toggleBot(b.dataset.confirmDisableBot,false);return;}
  if(b.dataset.editGroup!==undefined)groupEditor(vm.catalog.groups[Number(b.dataset.editGroup)]);if(b.dataset.editApp!==undefined)applicationEditor(vm.catalog.apps[Number(b.dataset.editApp)]);if(b.dataset.nameAccount!==undefined){vm.accountId=b.dataset.nameAccount;const a=vm.catalog.apps.find(x=>x.account_id===vm.accountId);node('accountNameInput').value=a?.account_name||'';report('accountNameFeedback');node('accountNameDialog').showModal();node('accountNameInput').focus();}if(b.dataset.removeAccount!==undefined){const apps=vm.catalog.apps.filter(a=>a.account_id===b.dataset.removeAccount);if(!apps.length)return;const name=apps[0].account_name||apps[0].name+' 所在账号';vm.accountToRemove={account_id:b.dataset.removeAccount,expected:apps.map(a=>({ref:a.ref,revision:a.revision}))};node('accountRemoveDescription').textContent=`从 Bridge 移除「${name}」及其 ${apps.length} 个机器人、${vm.catalog.groups.filter(g=>apps.some(a=>a.ref===g.app_ref)).length} 条群连接？`;node('accountRemoveDialog').showModal();node('cancelAccountRemove').focus();}if(b.dataset.restoreAccount!==undefined){operation(async()=>{try{vm.catalog=await api('/api/feishu/accounts/restore',{method:'POST',body:JSON.stringify({account_id:b.dataset.restoreAccount})});lists();window.dispatchEvent(new CustomEvent('bridge:assistants-changed'));report('groupsNotice','账号已恢复，机器人正在重新连接飞书。');}catch(err){report('groupsNotice',err.message,true);}});}});
 area.addEventListener('change',async e=>{
  const select=e.target.closest('select[data-backend-app]');
  if(!select||vm.busy)return;
  const a=app(select.dataset.backendApp);if(!a)return;
  const original=a.execution_backend||'codex';
  if(select.value===original)return;
  vm.busy=true;select.disabled=true;report('groupsNotice','正在切换执行工具…');
  try{
   vm.catalog=await api('/api/feishu/apps/backend',{method:'POST',body:JSON.stringify({app_ref:a.ref,revision:a.revision,execution_backend:select.value})});
   lists();report('groupsNotice',`「${a.name}」已切换。新任务使用${select.value==='claude'?' Claude Code CLI':' Codex CLI'}；已经开始的任务继续按原工具执行。`);
  }catch(err){select.value=original;report('groupsNotice',err.message,true);}
  finally{vm.busy=false;select.disabled=false;}
 });
 node('botSearch').oninput=lists;
 const migrationMode=mode=>{const exporting=mode==='export';node('migrationExportFlow').classList.toggle('hidden',!exporting);node('migrationImportFlow').classList.toggle('hidden',exporting);for(const [id,active] of [['migrationExportTab',exporting],['migrationImportTab',!exporting]]){node(id).classList.toggle('active',active);node(id).setAttribute('aria-pressed',String(active));}report('migrationFeedback');};
 function renderExportChoices(selectedRef){
  node('migrationChoices').innerHTML=vm.catalog.apps.map(a=>`<label class="check-label"><input type="checkbox" name="migrationChoice" class="migration-choice" value="${safe(a.ref)}" ${a.migration_supported?'':'disabled'} ${a.ref===selectedRef&&a.migration_supported?'checked':''}> ${safe(a.account_name||'未命名账号')} · ${safe(a.name)}${a.migration_supported?'':'（需单独迁移管理记录）'}</label>`).join('')||'<p>尚无可导出的机器人。</p>';
  countMigration();
 }
 node('migrationExportTab').onclick=()=>migrationMode('export');
 node('migrationImportTab').onclick=()=>migrationMode('import');
 node('migrationBack').onclick=()=>view(null);
 async function openMigrationFlow(selectedRef){view('migration');migrationMode('export');
  if(!vm.catalog.apps.length)await refreshCatalog();
  renderExportChoices(selectedRef);
  node('migrationTarget').innerHTML=projectOptions('');
  node('templateTargetBot').innerHTML='<option value="">选择已接入的目标机器人</option>'+vm.catalog.apps.filter(a=>a.migration_supported).map(a=>`<option value="${safe(a.ref)}">${safe(a.account_name||'未命名账号')} · ${safe(a.name)}</option>`).join('');
  node('migrationPanel').scrollIntoView({behavior:'smooth',block:'start'});
 }
 node('openMigration').onclick=()=>openMigrationFlow();
 const countMigration=()=>node('migrationSelectedCount').textContent=`已选 ${area.querySelectorAll('.migration-choice:checked').length} 个`;
 node('migrationChoices').onchange=countMigration;
 node('migrationSelectAll').onclick=()=>{area.querySelectorAll('.migration-choice:not(:disabled)').forEach(x=>x.checked=true);countMigration();};
 node('migrationClear').onclick=()=>{area.querySelectorAll('.migration-choice').forEach(x=>x.checked=false);countMigration();};
 const setMigrationProject=project=>{state.projects=state.projects.filter(p=>p.id!==project.id).concat(project);node('migrationTarget').innerHTML=projectOptions(project.id);node('migrationTarget').value=project.id;report('migrationFeedback',`已选中工作文件夹「${project.name}」。`);};
 node('migrationPickFolder').onclick=async()=>{if(vm.busy)return;vm.busy=true;report('migrationFeedback','正在打开文件夹选择窗口…');
  try{const result=await api('/api/projects/pick',{method:'POST',body:JSON.stringify({project_id:node('migrationTarget').value})});if(result.cancelled){report('migrationFeedback','未更改工作文件夹。');return;}setMigrationProject(result.project);}
  catch(e){node('migrationFolderFallback').open=true;report('migrationFeedback',e.message,true);}finally{vm.busy=false;}
 };
 node('migrationUseFolder').onclick=async()=>{if(vm.busy)return;vm.busy=true;try{const project=await api('/api/projects',{method:'POST',body:JSON.stringify({path:node('migrationFolderPath').value.trim()})});setMigrationProject(project);node('migrationFolderFallback').open=false;}catch(e){report('migrationFeedback',e.message,true);}finally{vm.busy=false;}};
 node('templateTargetBot').onchange=()=>{const target=app(node('templateTargetBot').value);if(target){node('migrationTarget').value=target.project_id||'';report('migrationFeedback');}};
 node('migrationFile').onchange=async()=>{const file=node('migrationFile').files[0];let legacy=false;
  vm.importKind=null;node('migrationFileSummary').textContent='';
  if(file&&file.size<=750000){try{
   const bundle=JSON.parse(await file.text());legacy=bundle.format==='codex-bridge-feishu-migration'&&bundle.version===1;
   if(bundle.format==='codex-bridge-assistant-template'&&bundle.version===1&&bundle.assistant){
    vm.importKind='template';const a=bundle.assistant;
    node('migrationFileSummary').textContent=`助手配置：${a.name||'未命名'}。原工作文件夹：${a.workspace_hint||'未设置'}；原执行工具：${a.backend==='claude'?'Claude Code CLI':'Codex CLI'}。`;
   }else if(bundle.format==='codex-bridge-feishu-migration'&&Array.isArray(bundle.apps)){
    vm.importKind='migration';node('migrationFileSummary').textContent=legacy?'旧版加密包：导入时需填写原密码。':`包含 ${bundle.apps.length} 个机器人：${bundle.apps.map(a=>a.name).join('、')}`;
   }else node('migrationFileSummary').textContent='文件内容无法识别。';
  }catch(e){node('migrationFileSummary').textContent='文件内容无法识别。';}}
  const template=vm.importKind==='template';
  node('templateTargetRow').classList.toggle('hidden',!template);
  node('migrationImportHelp').textContent=template?'选择一位已接入的机器人，把这份助手配置用在它的新任务上。飞书身份和群设置保留。':'选择从旧电脑下载的机器人配置包。导入后先保持停用，核对文件夹和执行工具，再切换接收。';
  node('migrationTargetLabel').textContent=template?'目标机器人使用的工作文件夹':'导入后使用的工作文件夹';
  node('migrationFile').previousElementSibling.textContent=template?'助手配置文件':'配置文件';
  node('importMigration').textContent=template?'复制设置到目标机器人':'导入并保持停用';
  node('migrationLegacyPassword').classList.toggle('hidden',!legacy);
 };
 node('exportMigration').onclick=async()=>{
  const selected=Array.from(area.querySelectorAll('.migration-choice:checked'),x=>{const a=app(x.value);return a&&{ref:a.ref,revision:a.revision};}).filter(Boolean);
  if(!selected.length){report('migrationFeedback','请选择要导出的机器人。',true);return;}
  await exportBots(selected,'migrationFeedback','feishu-bots-private');
 };
 node('importMigration').onclick=async()=>{if(vm.busy)return;
  const file=node('migrationFile').files[0],project_id=node('migrationTarget').value;
  if(!file||!project_id){report('migrationFeedback','请选择配置包和工作文件夹。',true);return;}
  if(file.size>750000){report('migrationFeedback','配置包超过大小限制，请检查文件。',true);return;}
  if(!vm.importKind){report('migrationFeedback','文件内容无法识别。',true);return;}
  vm.busy=true;report('migrationFeedback','正在导入配置…');
  try{const bundle=JSON.parse(await file.text());let result;
   if(vm.importKind==='template'){
    const target=app(node('templateTargetBot').value);
    if(!target)throw new Error('请选择要使用这份配置的机器人。');
    result=await api('/api/feishu/assistant-template/apply',{method:'POST',body:JSON.stringify({bundle,target_ref:target.ref,revision:target.revision,project_id})});
   }else{
    const payload={bundle,project_id};if(bundle.version===1)payload.password=node('migrationImportPassword').value;
    result=await api('/api/feishu/migration/import',{method:'POST',body:JSON.stringify(payload)});
   }
   const copied=vm.importKind==='template';
   node('migrationImportPassword').value='';node('migrationFile').value='';node('migrationFileSummary').textContent='';node('migrationLegacyPassword').classList.add('hidden');await refreshCatalog();
   vm.importKind=null;node('templateTargetRow').classList.add('hidden');
   report('migrationFeedback',copied?`「${result.name}」已用上这份配置。飞书身份和群设置没变；任务仍由原来的 ${result.execution_backend==='claude'?'Claude Code CLI':'Codex CLI'} 执行。`:`已导入 ${result.created.length} 个机器人，均保持停用。请核对工作文件夹与执行工具后再启用。`);
  }catch(e){report('migrationFeedback',e.message,true);}finally{vm.busy=false;}
 };
 node('stopMigrated').onclick=async()=>{if(vm.busy)return;
  if(!node('migrationTargetVerified').checked){report('migrationFeedback','请先在新电脑导入并核对配置。',true);return;}
  const selected=Array.from(area.querySelectorAll('.migration-choice:checked'),x=>{const a=app(x.value);return a&&{ref:a.ref,revision:a.revision};}).filter(Boolean);
  if(!selected.length){report('migrationFeedback','请选择要停用的机器人。',true);return;}
  vm.busy=true;
  try{const result=await api('/api/feishu/migration/stop',{method:'POST',body:JSON.stringify({selected,target_prepared:true})});
   await refreshCatalog();node('migrationTargetVerified').checked=false;
   report('migrationFeedback',`这台电脑已停用 ${result.stopped} 个机器人。现在到新电脑启用并发测试消息。`);
  }catch(e){report('migrationFeedback',e.message,true);}finally{vm.busy=false;}
 };
 node('cancelAccountName').onclick=()=>node('accountNameDialog').close();
 node('accountNameForm').onsubmit=e=>{e.preventDefault();if(vm.busy)return;const name=node('accountNameInput').value.trim();if(!name)return;operation(async()=>{try{vm.catalog=await api('/api/feishu/accounts',{method:'POST',body:JSON.stringify({account_id:vm.accountId,name})});node('accountNameDialog').close();lists();report('groupsNotice','账号名称已保存。');}catch(err){report('accountNameFeedback',err.message,true);}});};
 node('cancelAccountRemove').onclick=()=>{node('accountRemoveDialog').close();vm.accountToRemove=null;};
 node('confirmAccountRemove').onclick=()=>{if(!vm.accountToRemove||vm.busy)return;const payload=vm.accountToRemove;node('accountRemoveDialog').close();vm.accountToRemove=null;operation(async()=>{try{vm.catalog=await api('/api/feishu/accounts/remove',{method:'POST',body:JSON.stringify(payload)});lists();window.dispatchEvent(new CustomEvent('bridge:assistants-changed'));report('groupsNotice','账号已从 Bridge 移除，可在下方恢复。');}catch(err){report('groupsNotice',err.message,true);}});};
 node('addGroup').onclick=()=>groupEditor(null);
 node('addApplicationManual').onclick=()=>applicationEditor(null);
 node('addApplication').onclick=openExistingLink;
 window.addEventListener('bridge:edit-app',e=>{const a=app(e.detail);if(a){applicationEditor(a);area.scrollIntoView({behavior:'smooth',block:'start'});}});
 window.addEventListener('bridge:assistants-changed',()=>refreshCatalog());
 window.addEventListener('beforeunload',e=>{if(vm.groupDirty||vm.appDirty){e.preventDefault();e.returnValue='';}});
 refresh().then(refreshCatalog);setInterval(()=>{if(!vm.busy)refreshCatalog();if(state.page==='feishu')pollExistingLink();},5000);
})();
