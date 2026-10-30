const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const html = fs.readFileSync(process.argv[2], 'utf8');
for (const m of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(m[1]);
const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
const md = {esc}; vm.createContext(md);
vm.runInContext(html.slice(html.indexOf('function safeMD('), html.indexOf('function addMsg(')), md);
for (const separator of ['|---|---|', '| :--- | ---: |']) {
  const rendered = md.safeMD('| 列 | 均值 |\n'+separator+'\n| `signal_a` | 5 |\n| signal_b | 6 |');
  assert.match(rendered, /<thead><tr><th scope="col">列<\/th><th scope="col">均值<\/th><\/tr><\/thead>/);
  assert.equal((rendered.match(/<td>/g)||[]).length, 4);
  assert.equal((rendered.match(/<th /g)||[]).length, 2);
}
assert.match(md.safeMD('| Head |\n|---|'), /<th scope="col">Head<\/th>/);
assert.equal(md.safeMD(''), "<div style='height:5px'></div>");
assert(!md.safeMD('| <script> |\n|---|\n| <img onerror=bad> |').includes('<script>'));
assert(!md.safeMD('```\n| code |\n|---|\n```').includes('<table'));
const nodes = new Map(), timers = new Map(), pending = [], calls = [];
let timerId=0, requests = async () => ({ok:true,run:null});
function element() {
  const fields = new Map();
  const node = {dataset:{},style:{},children:[],className:'',hidden:false,disabled:false,textContent:'',
    querySelector(selector) { if (!fields.has(selector)) fields.set(selector,{dataset:{},disabled:false,hidden:false,style:{},textContent:''}); return fields.get(selector); },
    querySelectorAll(selector) {
      if (selector === '.msg') return this.children.filter(child => child.classList.contains('msg'));
      return selector.includes(':not') ? [this.querySelector('.details')] : [this.querySelector('.wf-approve'),this.querySelector('.details')];
    },
    get firstChild() { return this.children[0] || null; },
    get nextSibling() { return this.parentElement?.children[this.parentElement.children.indexOf(this) + 1] || null; },
    get firstElementChild() { return this.firstChild; },
    get nextElementSibling() { return this.nextSibling; },
    set innerHTML(value) { this.markup=value; for (const child of [...this.children]) child.remove(); },
    get innerHTML() { return this.markup || ''; },
    appendChild(child) { return this.insertBefore(child,null); },
    insertBefore(child,next) {
      if (child.parentElement) child.remove();
      const index=next ? this.children.indexOf(next) : this.children.length;
      assert(index >= 0, 'insertBefore target must belong to this parent');
      this.children.splice(index,0,child); child.parentElement=this;
      if (child.id) nodes.set(child.id,child);
      if (child.id?.startsWith('wf-chat-')) {
        nodes.set('wf-phrase-'+child.id.slice(8),child.querySelector('input'));
        nodes.set('wf-status-'+child.id.slice(8),child.querySelector('.status'));
      }
      return child;
    },
    remove() {
      if (this.parentElement) this.parentElement.children.splice(this.parentElement.children.indexOf(this),1);
      this.parentElement=null; nodes.delete(this.id);
    }};
  node.classList={contains:name=>node.className.split(' ').includes(name),add:name=>node.className+=' '+name};
  return node;
}
const log = element(); log.scrollHeight=500;
nodes.set('chatlog',log);
const ctx={CURRENT_SESSION:'A',SESSION_OPEN_VERSION:1,SESSION_LOADING:false,WORKFLOW_CHAT_TIMERS:{},esc,
  document:{getElementById:id=>nodes.get(id),createElement:()=>element()},
  api:(action,body)=>{calls.push(action);return requests(action,body);},
  setInterval:fn=>{timers.set(++timerId,fn);return timerId;},clearInterval:id=>timers.delete(id),
  taskReason:s=>s,taskPhase:s=>s,toast(){},refreshWorkflowCenter(){},
  loadSessionsbusy:false,loadSessions:async()=>{},safeMD:md.safeMD,
  closeSessionRename(){},saveChatDraft(){},detachChatView(){},restoreChatDraft(){},
  restoreResearchWorkflowSelection(){},showActiveResearchWorkflow(){},addResearchWorkflowLabel(){},
  setChatNotice(){},applyProjectContext(){},renderContextUsage(){},refreshContextUsage:async()=>{}};
vm.createContext(ctx);
vm.runInContext(html.slice(html.indexOf('function followChatScroll('),html.indexOf('document.getElementById("chatlog").addEventListener("scroll"')),ctx);
vm.runInContext(html.slice(html.indexOf('const WORKFLOW_STEP_LABELS'),html.indexOf('/* ---- 轻量安全 Markdown')),ctx);
vm.runInContext(html.slice(html.indexOf('function addMsg('),html.indexOf('/* ===================== 配置',html.indexOf('function addMsg('))),ctx);
vm.runInContext(html.slice(html.indexOf('async function openSession('),html.indexOf('async function newSession(')),ctx);
nodes.set('sesshint',element());
const pid='0123456789abcdef', plan={plan_id:pid,domain:'general',files:[],steps:[],execution:{},outputs:[]};
const flush = () => new Promise(resolve=>setImmediate(resolve));
(async()=>{
  ctx.addWorkflowPlanCard(log,{chat_plan:plan}); await flush();
  const card=nodes.get('wf-chat-'+pid), input=card.querySelector('input'), approve=card.querySelector('.wf-approve');
  assert.equal(approve.disabled,false); assert.equal(approve.hidden,false);
  input.value='keep draft'; ctx.addWorkflowPlanCard(log,{plan});
  assert.equal(nodes.get(card.id),card); assert.equal(input.value,'keep draft');
  requests=async()=>({ok:true,plan,run:{status:'completed',domain:'general'}});
  await [...timers.values()][0]();
  assert.equal(approve.hidden,true);assert.equal(input.disabled,true);assert.equal(timers.size,0);
  assert.match(card.querySelector('.status').textContent,/科学结论/);
  card.remove();
  requests=()=>new Promise(resolve=>pending.push(resolve));
  const recovering=ctx.restoreWorkflowCards([pid],'A',1);
  ctx.CURRENT_SESSION='B'; ctx.SESSION_OPEN_VERSION=2;
  pending.shift()({ok:true,plan,run:null}); await recovering;
  assert(!nodes.has(card.id),'old session must not insert cards in new session');
  ctx.CURRENT_SESSION='A';ctx.SESSION_OPEN_VERSION=3;
  requests=async()=>({ok:true,plan,run:null});
  await ctx.restoreWorkflowCards([pid,pid],'A',3);await flush();
  const fresh=nodes.get(card.id);assert(fresh);assert.notEqual(fresh,card);
  requests=async()=>({ok:false,error:{message:'offline'}});
  await [...timers.values()][0]();assert.equal(fresh.querySelector('.wf-approve').disabled,true);
  requests=async()=>({ok:true,plan,run:null});await [...timers.values()][0]();
  assert.equal(fresh.querySelector('.wf-approve').disabled,false);
  calls.length=0; requests=()=>new Promise(resolve=>pending.push(resolve));
  fresh.querySelector('input').value='approve '+pid;
  const click=ctx.approveAndRunWorkflow(pid); await ctx.approveAndRunWorkflow(pid);
  assert.deepEqual(calls,['workflow.get']);
  pending.shift()({ok:false});await click;
  assert(!calls.includes('workflow.approve'),'failed state check cannot authorize execution');
  // Resolve the read-only refresh started by finally and remove this session.
  ctx.CURRENT_SESSION='B';pending.shift()({ok:true,plan,run:null});await flush();
  assert.equal(timers.size,0);
  // Reopen an archived turn followed by later replies. Both card kinds must
  // remain below their original reply even when histories arrive separately.
  const rows=[
    {role:'user',content:'prepare a plan',request_id:'first',at:'2026-10-29T01:00:00Z'},
    {role:'assistant',content:'prepared '+pid,request_id:'first',at:'2026-10-29T01:00:00Z'},
    {role:'user',content:'submit two jobs',request_id:'second',at:'2026-10-29T02:00:00Z'},
    {role:'assistant',content:'please review',request_id:'second',at:'2026-10-29T02:00:00Z'},
    {role:'user',content:'a later question',request_id:'latest',at:'2026-10-29T03:00:00Z'},
    {role:'assistant',content:'latest reply',request_id:'latest',at:'2026-10-29T03:00:00Z'}
  ];
  const entries=[
    {id:'ps-newer',session_id:'A',status:'uncertain',created_at:'2026-10-29T02:20:00Z',chat_anchor:{request_id:'second'}},
    {id:'ps-older',session_id:'A',status:'submitting',created_at:'2026-10-29T02:10:00Z',chat_anchor:{request_id:'second'}}
  ];
  requests=async action=>action==='chat.load' ? {ok:true,archived_messages:rows.slice(0,2),messages:rows.slice(2),
    workflow_plans:[pid],workflow_plan_anchors:{[pid]:{request_id:'first',message_index:1}}} :
    action==='submit.history' ? {ok:true,entries} : {ok:true,plan,run:{status:'completed',domain:'general'}};
  calls.length=0;
  await ctx.openSession('A');await flush();
  const order=()=>log.children.map(node=>node.id || node.querySelector('.bubble').dataset.acc);
  assert.deepEqual(order(),['prepare a plan','prepared '+pid,'wf-chat-'+pid,
    'submit two jobs','please review','cf-ps-older','cf-ps-newer','a later question','latest reply']);
  assert.equal(nodes.get('wf-chat-'+pid).querySelector('.wf-approve').hidden,true);
  assert(!nodes.get('cf-ps-newer').innerHTML.includes('onclick="confirmSubmit'));
  assert(calls.every(action=>['chat.load','workflow.get','submit.history'].includes(action)), 'reopening must only read');
  // Repeated history polling updates in place without duplicates or forced scroll.
  log.dataset.follow='false';log.scrollTop=73;
  await ctx.pollPendingCards();
  ctx.addConfirmCard(log,{...entries[1],status:'uncertain'});
  assert.equal(log.scrollTop,73);
  assert.equal(log.children.filter(node=>node.id?.startsWith('cf-')).length,2);
  const timeline=order();
  await ctx.openSession('A');await flush();assert.deepEqual(order(),timeline);
  // Legacy records: ID mentions beat timestamps; submissions belong to the
  // user/assistant pair even when the reply's timestamp is later than the card.
  log.innerHTML='';log._workflowAnchors={};log.dataset.follow='false';
  for (const [i,row] of rows.entries()) ctx.addMsg(row.role==='user'?'user':'bot',row.content,{...row,message_index:i});
  log.children[3].dataset.at='2026-10-29T02:30:00Z';
  log.scrollTop=91;
  ctx.addWorkflowPlanCard(log,{plan,prepared_at:'2026-10-29T03:30:00Z',run:{status:'completed'}});
  ctx.addConfirmCard(log,{...entries[0],chat_anchor:{}});
  ctx.addConfirmCard(log,{...entries[1],chat_anchor:{}});
  assert.deepEqual(order(),timeline);
  assert.equal(log.scrollTop,91);
  const unknown=element();unknown.id='unknown-card';
  ctx.placeChatCard(log,unknown,{},'missing','invalid-date');
  assert.equal(log.firstChild,unknown,'unplaceable old records must not cover the latest reply');
  assert.equal(ctx.chatCardMessage(log,{message_index:1},'', ''),log.querySelectorAll('.msg')[1]);
  // A plan load failure remains at its anchor, including on retry.
  const damaged='ffffffffffffffff';
  log._workflowAnchors[damaged]={request_id:'first'};
  requests=async()=>({ok:false,error:{message:'missing plan'}});
  await ctx.restoreWorkflowCards([damaged],'A',ctx.SESSION_OPEN_VERSION);
  assert(order().indexOf('wf-chat-'+damaged)<order().indexOf('submit two jobs'));
  nodes.get('wf-chat-'+damaged).remove();
  requests=async()=>({ok:true,plan:{...plan,plan_id:damaged},run:{status:'completed'}});
  await ctx.restoreWorkflowCards([damaged],'A',ctx.SESSION_OPEN_VERSION);await flush();
  assert(order().indexOf('wf-chat-'+damaged)<order().indexOf('submit two jobs'));
  // A delayed history response from the previous opening cannot pollute the
  // new view, including switching away and back to the same session.
  requests=()=>new Promise(resolve=>pending.push(resolve));
  const oldPoll=ctx.pollPendingCards();
  ctx.SESSION_OPEN_VERSION++;
  pending.shift()({ok:true,entries:[{...entries[0],id:'ps-stale'}]});await oldPoll;
  assert(!nodes.has('cf-ps-stale'));
  ctx.CURRENT_SESSION='B';ctx.addConfirmCard(log,{...entries[0],id:'ps-other'});
  assert(!nodes.has('cf-ps-other'));
  await flush();
  assert.equal(timers.size,0);
  assert.match(html, /\["workflow_prepare", "research_prepare"\]\.includes\(ev.tool\)/);
  assert.match(html, /restoreWorkflowCards\(r.workflow_plans, id, version\)/);
  assert.match(html, /restoreWorkflowCards\(r.workflow_plans, view.session_id/);
})().catch(error=>{console.error(error);process.exitCode=1;});
