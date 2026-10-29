const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const html = fs.readFileSync(process.argv[2], 'utf8');
const elements = new Map();
function get(id) {
  if (!elements.has(id)) elements.set(id, {value:'', textContent:'', innerHTML:'', dataset:{},
    disabled:false, checked:false, addEventListener(){}, classList:{contains(){return true;}, remove(){}}});
  return elements.get(id);
}
const saved = {host:'vlab.example',user:'ubuntu',port:22,identity_file:'key.pem',auto_deploy_gateway:true};
for (const [id,key] of Object.entries({cfghost:'host',cfguser:'user',cfgport:'port',cfgidentity:'identity_file'})) get(id).value = String(saved[key]);
get('cfgautodeploy').checked = true;
const requests = [];
const timers = new Map(); let timerId = 0;
let responder;
const ctx = {CFG:{vlab:saved}, document:{getElementById:get, querySelector(){return {dataset:{c:'conn'}};}, querySelectorAll(){return [];}},
  setInterval(){}, setTimeout(fn, ms){const id=++timerId; timers.set(id,{fn,ms}); return id;},
  clearTimeout(id){timers.delete(id);}, AbortController, Date, Promise, toast(){}, esc:String, fmtBytes:String, connFlips(){},
  api:async (action,body) => {requests.push(action); return responder(action,body);}};
vm.createContext(ctx);
function section(start,end) {return html.slice(html.indexOf(start),html.indexOf(end,html.indexOf(start)));}
vm.runInContext(section('// A check applies', '/* ===================== 服务器 添加/编辑'),ctx);
vm.runInContext(section('let serverRefreshRun', 'function currentProvider'),ctx);
vm.runInContext(section('let workspaceRefreshBusy', 'async function workspaceDoctor'),ctx);
vm.runInContext(section('let serverSaveBusy', '/* ===================== 会话（记忆）'),ctx);
const tick = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  let resolveCheck;
  responder = () => new Promise(resolve => {resolveCheck=resolve;});
  const check = ctx.checkVlabConnection();
  ctx.checkVlabConnection();
  assert.equal(requests.length,1,'duplicate checks must share work');
  assert.equal(get('vlab-status').dataset.state,'checking');
  get('cfghost').value = 'edited.example'; ctx.vlabEdited();
  resolveCheck({ok:true,connected:true,message:'SSH available'}); await check;
  assert.equal(get('vlab-status').dataset.state,'dirty','old success must not green-light edited config');
  await ctx.openVlabTerminal();
  assert.equal(requests.length,1,'unsaved settings must not silently launch old host');
  get('cfghost').value = saved.host; ctx.vlabEdited();
  responder = () => ({ok:true,connected:false,message:'SSH denied'});
  await ctx.checkVlabConnection();
  assert.equal(get('vlab-status').dataset.state,'error');
  responder = () => ({ok:true}); await ctx.openVlabTerminal();
  assert.equal(get('vlab-status').dataset.state,'error','opening a terminal is not proof of a connection');
  assert.equal(get('vlab-terminal').disabled,false);
  let slow;
  const snapshots = [];
  ctx.renderServerState = st => snapshots.push(st.servers.map(s => ({...s})));
  responder = (action,body) => action === 'state'
    ? {ok:true,servers:[{name:'slow'},{name:'fast'}]}
    : body.server === 'slow' ? new Promise(resolve => {slow=resolve;}) : {ok:true,connected:true};
  const refresh = ctx.refreshServers(); ctx.refreshServers(); await tick();
  assert.equal(snapshots.at(-1)[1].connected,true,'fast result must appear before slow host finishes');
  assert.equal(snapshots.at(-1)[0].connected,null);
  slow({ok:false,error:{message:'SSH timeout'}}); await refresh;
  assert.equal(get('servers-refresh').disabled,false);
  assert.equal(snapshots.at(-1)[0].status_error,'SSH timeout');
  let late = [];
  responder = (action) => action === 'state'
    ? {ok:true,servers:['a','b','c','d','e'].map(name=>({name}))}
    : new Promise(resolve=>late.push(resolve));
  const cancelled=ctx.refreshServers(); await tick();
  assert.equal(late.length,4,'concurrency stays bounded');
  assert.equal(get('servers-refresh').disabled,false,'stop control remains available');
  ctx.toggleServerRefresh(); await cancelled;
  assert.equal(get('servers-refresh').textContent,'刷新');
  assert(snapshots.at(-1).every(s=>s.status_label==='已停止检测'));
  const countAfterStop=snapshots.length;
  late.forEach(resolve=>resolve({ok:true,connected:true})); await tick();
  assert.equal(snapshots.length,countAfterStop,'late responses must not repaint stopped results');
  assert.equal(late.length,4,'stopped run must not check queued hosts');
  late=[];
  const timed=ctx.refreshServers(); await tick();
  const deadline=[...timers.values()].find(timer=>timer.ms===20000);
  assert(deadline,'each batch has a fixed 20-second deadline');
  deadline.fn(); await timed;
  assert.equal(get('servers-refresh').textContent,'刷新');
  assert(snapshots.at(-1).every(s=>s.status_label==='检测超时'));
  late.forEach(resolve=>resolve({ok:true,connected:true})); await tick();
  let workspace;
  responder = () => new Promise(resolve => {workspace=resolve;});
  const before = requests.length;
  const load = ctx.refreshWorkspacePanel(); ctx.refreshWorkspacePanel();
  assert.equal(requests.length,before+1);
  workspace({ok:true,space:{},workspaces:[]}); await load;
  const old = get('workspaceout').innerHTML;
  responder = () => ({ok:false,error:{code:'connection_error',message:'local service lost'}});
  await ctx.refreshWorkspacePanel();
  assert.equal(get('workspaceout').innerHTML,old,'failed refresh preserves previous data');
  assert(get('workspace-status').textContent.includes('无法连接本机服务'));
  assert(!get('workspace-status').textContent.includes('Gateway'));
  assert.equal(get('workspace-refresh').disabled,false);
  get('mname').dataset.orig = 'cl9'; get('mname').value = 'cl9'; get('mroute').value = 'vlab';
  get('mtarget').value = 'user@host'; get('mport').value = '22';
  ctx.document.querySelectorAll = () => [get('server-save')];
  let saveResult;
  responder = () => new Promise(resolve => {saveResult=resolve;});
  const beforeSave = requests.length;
  const saving = ctx.saveServerModal(); ctx.saveServerModal();
  assert.equal(requests.length,beforeSave+1,'saving twice must not submit two requests');
  assert.equal(get('server-save').disabled,true);
  saveResult({ok:false,error:{message:'Vlab timeout'}}); await saving;
  assert.equal(get('server-save').disabled,false);
  assert.equal(get('server-save-status').textContent,'Vlab timeout');
  assert.equal(get('mtarget').value,'user@host','failure keeps the form for retry');
  responder = () => ({ok:true});
  ctx.refreshServers = () => new Promise(() => {});
  await ctx.saveServerModal();
  assert.equal(get('server-save').disabled,false,'successful save must not wait for status refresh');
  console.log('connection refresh: pending, stale, dirty, deduplication, incremental status and recovery passed');
})().catch(error => {console.error(error); process.exitCode=1;});
