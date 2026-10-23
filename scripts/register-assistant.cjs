/* Private per-attempt state. Credentials never cross stdout or the browser. */
const fs = require('node:fs');
const path = require('node:path');
const lark = require('@larksuiteoapi/node-sdk');
const QRCode = require('qrcode');
process.umask(0o077);
const dir = path.resolve(process.argv[2]);
const input = JSON.parse(fs.readFileSync(path.join(dir,'registration-input.json'),'utf8'));
const statusPath = path.join(dir,'registration.json');
function write(name, value) {
 const target=path.join(dir,name), tmp=target+'.'+process.pid+'.tmp';
 fs.writeFileSync(tmp,JSON.stringify(value),{mode:0o600});fs.renameSync(tmp,target);
}
function state(value) {write('registration.json',{...value,pid:process.pid,updated_at:Date.now()/1000});}
async function main(){
 if(fs.existsSync(path.join(dir,'credentials.json'))) {state({status:'credentials_saved'});return;}
 if(fs.existsSync(statusPath)) {throw new Error('existing_attempt');}
 state({status:'starting'});
 const result=await lark.registerApp({
  createOnly:true,source:'codex-bridge',appPreset:{name:input.name,desc:input.summary},
  addons:{preset:false,scopes:{tenant:input.scopes},events:{items:{tenant:['im.message.receive_v1']}}},
  onQRCodeReady(info){
   const u=new URL(info.url);
   if(u.protocol!=='https:'||!['accounts.feishu.cn','open.feishu.cn','accounts.larksuite.com','open.larksuite.com'].includes(u.hostname))throw new Error('verification_host');
   state({status:'waiting_scan',url:info.url,expires_at:Date.now()/1000+info.expireIn});
   QRCode.toFile(path.join(dir,'qr.png'),info.url,{width:480,margin:2}).catch(()=>{});
  }
 });
 if(!result.client_id||!result.client_secret)throw new Error('missing_credentials');
 // Save before any CLI/Bridge operation so retries never re-create the app.
 write('credentials.json',{app_id:result.client_id,app_secret:result.client_secret,
  owner_open_id:result.user_info?.open_id||'',brand:result.user_info?.tenant_brand||'feishu'});
 state({status:'credentials_saved'});
}
main().catch(e=>{
 const old=fs.existsSync(statusPath)?JSON.parse(fs.readFileSync(statusPath)):{};
 const code=String(e.code||e.error||'registration_failed');
 state({...old,status:'needs_check',error_code:code.slice(0,80)});
 process.exitCode=1;
});
