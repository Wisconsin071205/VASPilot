const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const html = fs.readFileSync(process.argv[2], 'utf8');
const elements = new Map();
let focused = '';
function get(id) {
  if (!elements.has(id)) {
    const classes = new Set();
    elements.set(id, {value:'', textContent:'', innerHTML:'', dataset:{}, hidden:false,
      disabled:false, checked:false, focus(){focused=id;}, addEventListener(){}, setAttribute(){},
      classList:{add(c){classes.add(c);}, remove(c){classes.delete(c);}, contains(c){return classes.has(c);}}});
  }
  return elements.get(id);
}
const controls = ['mname', 'mtarget', 'mport', 'mroute', 'server-save', 'server-delete',
  'server-delete-cancel', 'server-delete-confirm-button'].map(get);
const requests = [], notifications = [], renders = [], picks = [];
let responder, stops = 0, refreshes = 0;
const ctx = {document:{getElementById:get, querySelectorAll:()=>controls, querySelector:()=>get('filebody')},
  setTimeout(){}, confirm(){throw new Error('use explicit in-app confirmation');},
  toast:(...args)=>notifications.push(args), STATE:{servers:[]}, PREV_CONN:new Set(['cl9']),
  api:async(action, body, silent)=>{requests.push({action,body,silent}); return responder(action,body);},
  stopServerRefresh(){stops++;}, refreshServers(){refreshes++; return new Promise(()=>{});},
  pickFileServer(force){picks.push(force);},
  renderServerState(state){
    renders.push(state); ctx.STATE = state;
    for (const id of ['fsrv','jsrv','wsrv']) {
      if (!state.servers.some(s=>s.name===get(id).value)) get(id).value = state.servers[0]?.name || '';
    }
  }};
vm.createContext(ctx);
const start = html.indexOf('/* ===================== 服务器 添加/编辑');
vm.runInContext(html.slice(start, html.indexOf('/* ===================== 会话（记忆）', start)), ctx);
const entry = {name:'cl9', target:'user@cl9', port:22, route:'vlab', remote_root:'/home/cl9', scheduler:'slurm'};
function edit() {
  ctx.STATE = {servers:[entry, {name:'other', remote_root:'/home/other'}], providers:[]};
  ctx.openServerModal(entry);
  for (const id of ['fsrv','jsrv','wsrv']) get(id).value='cl9';
  get('fpath').value='/home/cl9/old';
  get('filebody').dataset.loaded='1';
}
(async () => {
  assert.match(html, /id="server-delete"[^>]*hidden/);
  ctx.openServerModal(null);
  assert.equal(get('server-delete').hidden,true,'adding a server has no delete action');
  ctx.requestServerDelete();
  await ctx.deleteServerModal();
  assert.equal(get('server-delete-confirm').hidden,true);
  assert.equal(requests.length,0);

  edit(); get('mname').value='unsaved-rename';
  assert.equal(get('server-delete').hidden,false);
  await ctx.deleteServerModal();
  assert.equal(requests.length,0,'deleting requires opening the confirmation first');
  ctx.requestServerDelete();
  assert.equal(requests.length,0,'the first click only opens the confirmation');
  assert.equal(get('server-delete-confirm').hidden,false);
  assert.equal(get('server-actions').hidden,true);
  assert.equal(focused,'server-delete-cancel','default keyboard focus must be on cancel');
  assert(get('server-delete-title').textContent.includes('「cl9」'));
  assert(!get('server-delete-title').textContent.includes('unsaved-rename'));
  ctx.cancelServerDelete();
  assert.equal(requests.length,0,'cancelling confirmation must not mutate anything');
  assert.equal(get('modalback').classList.contains('on'),true);
  assert.equal(get('mname').value,'unsaved-rename');
  assert.equal(get('server-delete-confirm').hidden,true);
  assert.equal(get('server-actions').hidden,false);
  await ctx.deleteServerModal();
  assert.equal(requests.length,0,'cancel also revokes the pending deletion');

  ctx.requestServerDelete();
  let finish;
  responder=()=>new Promise(resolve=>{finish=resolve;});
  get('mport').disabled=true;
  const deleting=ctx.deleteServerModal();
  await ctx.deleteServerModal(); await ctx.saveServerModal(); ctx.openServerModal(null);
  assert.equal(requests.length,1,'delete and save must not overlap or submit twice');
  assert.equal(requests[0].action,'server.remove');
  assert.equal(requests[0].body.server,'cl9','delete targets the saved identity');
  assert.equal(get('server-delete-confirm-button').textContent,'删除中…');
  assert(controls.every(c=>c.disabled));
  finish({ok:false,error:{message:'Vlab 暂时不可用，请重试'}}); await deleting;
  assert.equal(get('modalback').classList.contains('on'),true);
  assert.equal(get('server-save-status').textContent,'Vlab 暂时不可用，请重试');
  assert.equal(get('server-delete-confirm').hidden,false,'failure keeps confirmation open for retry');
  assert.equal(get('server-delete').disabled,false);
  assert.equal(get('server-save').disabled,false);
  assert.equal(get('mport').disabled,true,'restore the original disabled state');
  assert.equal(get('mname').value,'unsaved-rename');
  assert.equal(renders.length,0,'failure preserves the server list');

  responder=()=>({ok:true,removed:'cl9'});
  await ctx.deleteServerModal();
  assert.equal(get('modalback').classList.contains('on'),false);
  assert.equal(stops,1,'stop stale refreshes before updating the server list');
  assert.equal(renders.at(-1).servers.map(s=>s.name).join(','),'other');
  assert.equal(ctx.PREV_CONN.has('cl9'),false);
  for (const id of ['fsrv','jsrv','wsrv']) assert.equal(get(id).value,'other');
  assert.equal(get('fpath').value,'','discard the deleted server directory');
  assert.equal(get('fup').disabled,true);
  assert.equal(get('filebody').dataset.loaded,undefined);
  assert.equal(picks.length,1);
  assert.equal(refreshes,1,'refresh after removal without holding the dialog open');
  assert.equal(get('server-delete-confirm-button').textContent,'确认删除');

  edit(); ctx.STATE.servers=[entry];
  ctx.requestServerDelete();
  await ctx.deleteServerModal();
  assert.equal(ctx.STATE.servers.length,0,'the final server can be removed');
  assert.equal(get('fsrv').value,'');
  assert.equal(picks.length,1,'no directory lookup when no servers remain');

  edit(); responder=()=>new Promise(resolve=>{finish=resolve;});
  const saving=ctx.saveServerModal();
  const before=requests.length;
  ctx.requestServerDelete();
  await ctx.deleteServerModal();
  assert.equal(requests.length,before,'delete is blocked while saving');
  assert.equal(get('server-delete-confirm').hidden,true);
  finish({ok:true}); await saving;
  console.log('server removal: confirmation, identity, cancellation, busy states, recovery and list refresh passed');
})().catch(error=>{console.error(error); process.exitCode=1;});
