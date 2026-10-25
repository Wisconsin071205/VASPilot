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
  return {dataset:{},style:{},hidden:false,disabled:false,textContent:'',
    querySelector(selector) { if (!fields.has(selector)) fields.set(selector,{disabled:false,hidden:false,style:{},textContent:''}); return fields.get(selector); },
    querySelectorAll(selector) { return selector.includes(':not') ? [this.querySelector('.details')] : [this.querySelector('.wf-approve'),this.querySelector('.details')]; },
    remove() { nodes.delete(this.id); }};
}
const log = {scrollHeight:500,appendChild(card){nodes.set(card.id,card);nodes.set('wf-phrase-'+card.id.slice(8),card.querySelector('input'));nodes.set('wf-status-'+card.id.slice(8),card.querySelector('.status'));}};
nodes.set('chatlog',log);
const ctx={CURRENT_SESSION:'A',SESSION_OPEN_VERSION:1,WORKFLOW_CHAT_TIMERS:{},esc,
  document:{getElementById:id=>nodes.get(id),createElement:()=>element()},
  api:(action,body)=>{calls.push(action);return requests(action,body);},
  setInterval:fn=>{timers.set(++timerId,fn);return timerId;},clearInterval:id=>timers.delete(id),
  taskReason:s=>s,taskPhase:s=>s,toast(){},refreshWorkflowCenter(){}};
vm.createContext(ctx);
vm.runInContext(html.slice(html.indexOf('const WORKFLOW_STEP_LABELS'),html.indexOf('function addConfirmCard(')),ctx);
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
  nodes.delete(card.id);
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
  assert.match(html, /\["workflow_prepare", "research_prepare"\]\.includes\(ev.tool\)/);
  assert.match(html, /restoreWorkflowCards\(r.workflow_plans, id, version\)/);
  assert.match(html, /restoreWorkflowCards\(r.workflow_plans, view.session_id/);
})().catch(error=>{console.error(error);process.exitCode=1;});
