const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync(process.argv[2], 'utf8');
new vm.Script(source);
const elements = new Map();
function element() {
  const children = new Map();
  return {value:'',innerHTML:'',textContent:'',hidden:false,disabled:false,listeners:{},
    querySelector(s) {if(!children.has(s)) children.set(s, element()); return children.get(s);},
    querySelectorAll(){return [];},addEventListener(e,fn){this.listeners[e]=fn;}};
}
function get(id){if(!elements.has(id)) elements.set(id,element()); return elements.get(id);}
const requests = [], box = get('fleet-detail');
const esc = s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const bundle = {ok:true,checked_at:'now',logs:[{path:'/run/a.log',reason:'默认日志',scope:'尾部',relation:'directory_only',content:'<script>bad()</script>'}],warnings:[],excluded:[]};
let failAI = false;
const ctx = {el:get,esc,PROJECTS:[],labels:{UNKNOWN:'状态待核对'},terminal:new Set(),fmtBytes:String,fmtLocalDateTime:String,
  loadRemoteJobs(){},loadCollections(){},
  api:async action=>{requests.push(action);
    if(action==='job.details') return {ok:true,job:{state:'UNKNOWN'},directory:'/run',files:[],warnings:[]};
    if(action==='job.ai_analysis') return failAI ? {ok:false,error:{message:'模型不可用'}} : {...bundle,analysis:{model:'test',created_at:'now',text:'<img src=x>model answer'}};
    return bundle;
  }};
vm.createContext(ctx);
vm.runInContext('let detailVersion = 0;\n' + source.slice(source.indexOf('  async function openDetail('), source.indexOf('  window.loadCollections')),ctx);
const tick = ()=>new Promise(r=>setImmediate(r));
(async()=>{
  await ctx.openDetail({server:'cl9',job_id:'713'}); await tick();
  assert(requests.includes('job.logs_collect'));
  assert(!requests.includes('job.ai_analysis'));
  assert(box.querySelector('[data-log-list]').innerHTML.includes('&lt;script&gt;'));
  const button=box.querySelector('[data-ai-logs]');
  assert.equal(button.disabled,false);
  button.listeners.click(); button.listeners.click(); await tick();
  assert.equal(requests.filter(a=>a==='job.ai_analysis').length,1);
  assert(box.querySelector('[data-ai-result]').innerHTML.includes('&lt;img'));
  failAI=true; button.listeners.click(); await tick();
  assert(box.querySelector('[data-log-status]').textContent.includes('模型不可用'));
  assert(box.querySelector('[data-log-list]').innerHTML.includes('a.log'));
  box.querySelector('[data-refresh-logs]').listeners.click(); await tick();
  assert.equal(box.querySelector('[data-ai-result]').textContent,'');
  console.log('job logs: automatic discovery, explicit AI, duplicate guard, escaping and retry passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
