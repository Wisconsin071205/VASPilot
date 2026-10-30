const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const elements = new Map();
function element() {
  const listeners = {}, children = new Map();
  return {value:'', innerHTML:'', textContent:'', style:{}, disabled:false,
    listeners, addEventListener(event, fn) {listeners[event] = fn;},
    querySelector(selector) {if (!children.has(selector)) children.set(selector, element()); return children.get(selector);},
    querySelectorAll() {return [];}, appendChild() {}, classList:{contains(){return false;}}};
}
function get(id) {if (!elements.has(id)) elements.set(id, element()); return elements.get(id);}
get('fleet-state').value = 'all';
const esc = value => String(value ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
let finishSlow, requests = [];
const slow = new Promise(resolve => {finishSlow = resolve;});
const old = {job_id:'10', server:'slow', name:'old-job', state:'RUNNING', last_seen:'yesterday'};
const ctx = {window:{}, document:{getElementById:get, createElement:element, hidden:false},
  esc, fmtLocalDateTime:String, fmtBytes:String, PROJECTS:[], setInterval(){},
  api: async (action, body) => {
    requests.push({action, body});
    if (action === 'collection.list') return {ok:true, tasks:[]};
    if (body.refresh === false) return {ok:true, servers:[{server:'slow'}, {server:'fast'}], jobs:[old]};
    if (body.server === 'slow') return slow;
    return {ok:true, servers:[{server:'fast',checked_at:'today'}], jobs:[
      {server:'fast', job_id:'10', name:'<img src=x onerror=alert(1)>', state:'FAILED', last_seen:'today'}]};
  }};
ctx.loadCollections = (...args) => ctx.window.loadCollections(...args);
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), ctx);
(async () => {
  const refresh = ctx.window.loadRemoteJobs();
  await new Promise(resolve => setImmediate(resolve));
  let table = get('fleet-table').querySelector('tbody').innerHTML;
  assert(table.includes('old-job'), 'cached slow-server data must remain visible');
  assert(table.includes('&lt;img'), 'fast server must render before slow server returns');
  assert(!table.includes('<img'), 'remote names must never become HTML');
  await ctx.window.loadRemoteJobs();
  assert.equal(requests.filter(r => r.body?.server === 'fast').length, 1, 'double click must not duplicate refresh');
  finishSlow({ok:false,error:{message:'connection timeout'}});
  await refresh;
  table = get('fleet-table').querySelector('tbody').innerHTML;
  assert(table.includes('最后已知状态'));
  assert(get('fleet-status').textContent.includes('connection timeout'));
  get('fleet-server').value = 'fast';
  get('fleet-server').listeners.change();
  table = get('fleet-table').querySelector('tbody').innerHTML;
  assert(!table.includes('old-job'), 'server filter must isolate same-number jobs');
  assert.equal(get('fleet-refresh').disabled, false);
  console.log('remote-center frontend: incremental refresh, stale data, escaping and filtering passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
