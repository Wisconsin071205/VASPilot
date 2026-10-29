const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const html = fs.readFileSync(process.argv[2], 'utf8');
// Compile every inline script as well as exercising the idle view in isolation.
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
const elements = new Map(), storage = new Map();
function get(id) {
  if (!elements.has(id)) elements.set(id, {value: '', innerHTML: '', disabled: false});
  return elements.get(id);
}
let now = Math.floor(Date.now() / 60000) * 60000;
const requests = [], copies = [];
const ctx = {
  document: {getElementById: get},
  localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
  STATE: {servers: []}, CURRENT_METRICS: {}, TREND: {}, CFG: {}, MVIEW: 'idle',
  Date: {now: () => now},
  esc: value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char])),
  badge: (label, tone) => `<span class="badge ${tone}">${label}</span>`, toast() {},
  navigator: {clipboard: {writeText: async text => copies.push(text)}},
  api: async (action, body) => {requests.push({action, body}); return {ok: true, buckets: []};},
};
vm.createContext(ctx);
const start = html.indexOf('/* ---- 空闲算力（');
const end = html.indexOf('/* ===================== 设置：', start);
assert(start > 0 && end > start);
vm.runInContext(html.slice(start, end), ctx);
function server(name, cpu, gpus = [], connected = true) {
  ctx.STATE.servers.push({name, connected});
  ctx.CURRENT_METRICS[name] = {ok: true, cpu, gpus,
    mem: {available_gb: 120, total_gb: 128}, load: {one: 1.2}};
}
function render() {ctx.renderIdlePool(); return get('idlegrid').innerHTML;}
function card(name, type = 'CPU') {
  const result = render().split('<div class="msrv">').find(part => part.includes(`${ctx.esc(name)} · ${type}`));
  assert(result, `${name} ${type} card exists`);
  return result;
}
const gpu = {index: 0, name: 'A100 <test>', mem_total_gb: 80, mem_used_gb: 10, util_pct: 2, temp_c: 35};
server('cpu-only', {cores: 32, usage_pct: 5});
server('hybrid', {cores: 64, usage_pct: 2}, [gpu]);
server('busy', {cores: 32, usage_pct: 80});
server('unknown', {cores: 16, usage_pct: null});
server('offline', {cores: 128, usage_pct: 0}, [], false);
server('failed', {cores: 128, usage_pct: 0});
ctx.CURRENT_METRICS.failed.ok = false;
server('quoted-"<node>', {cores: 8, usage_pct: 0});
const reportedCpus = [
  ['cl9', 12, 61.1], ['cluster9', 12, 59.1],
  ['shuangyiliu', 24, 12.7], ['precision', 4, 88.3],
];
for (const [name, cores, usage_pct] of reportedCpus) server(name, {cores, usage_pct});

(async () => {
  // An old shared 10% limit remains GPU-only; it must not reject spare CPU cores.
  storage.set('vp_idle_cfg', JSON.stringify({vram: 24, util: 10, min: 0, sort: 'vram'}));
  ctx.restoreIdleInputs();
  assert.equal(get('idleType').value, 'all');
  assert.equal(get('idleCores').value, 0);
  assert.equal(get('idleVram').value, 24);
  assert.equal(get('idleUtil').value, 10);
  assert.equal(get('idleCpuUtil').value, 100);
  assert.match(card('cpu-only'), /30\.4 \/ 32 核/);
  assert.match(card('cpu-only'), /120 \/ 128 GB/);
  assert.match(card('cpu-only'), /badge ok/);
  assert.match(card('busy'), /符合条件/);
  assert.match(card('unknown'), /badge warn/);
  assert.match(card('unknown'), /\? \/ 16 核/);
  assert.match(card('hybrid', 'GPU 0'), /70 GB/);
  assert.match(card('hybrid', 'GPU 0'), /A100 &lt;test&gt;/);
  assert(!render().includes('offline ·'));
  assert(!render().includes('failed ·'));
  assert(render().includes('quoted-&quot;&lt;node&gt;'));
  assert(!render().includes('NaN'));
  assert(!render().includes('不可用'));
  for (const [name] of reportedCpus) assert.match(card(name), /符合条件/);
  assert.match(card('shuangyiliu'), /21 \/ 24 核/);
  get('idleCpuUtil').value = 10;
  for (const [name, , usage] of reportedCpus) {
    const result = card(name);
    assert.match(result, /未达条件/);
    assert(result.includes(`利用率 ${usage}% 超过上限 10%`));
  }
  get('idleCpuUtil').value = 100;
  get('idleUtil').value = 1;
  assert.match(card('hybrid'), /符合条件/, 'GPU usage threshold never filters CPU');
  assert.match(card('hybrid', 'GPU 0'), /利用率 2% 超过上限 1%/);
  get('idleUtil').value = 10;

  get('idleType').value = 'cpu'; ctx.idleTypeChanged();
  assert(get('idleVram').disabled);
  assert(get('idleUtil').disabled);
  assert(!get('idleCores').disabled);
  assert(!get('idleCpuUtil').disabled);
  assert.equal(get('idleSort').value, 'cores');
  assert(!render().includes('· GPU'));
  assert(render().indexOf('hybrid · CPU') < render().indexOf('cpu-only · CPU'));
  get('idleCores').value = 31;
  assert.match(card('cpu-only'), /未达条件/);
  assert.match(card('cpu-only'), /估算空闲 30\.4 核，低于要求的 31 核/);
  assert.match(card('hybrid'), /badge ok/);
  get('idleVram').value = 1000;
  assert.match(card('hybrid'), /badge ok/, 'GPU constraints do not reject CPU');
  ctx.restoreIdleInputs();
  assert.equal(get('idleCores').value, 31);
  assert.equal(get('idleType').value, 'cpu');
  get('idleCpuUtil').value = 75;
  render(); ctx.restoreIdleInputs();
  assert.equal(get('idleCpuUtil').value, 75, 'keep an explicitly chosen CPU threshold');
  assert.equal(get('idleUtil').value, 10, 'CPU setting must not overwrite GPU setting');

  get('idleType').value = 'gpu'; get('idleVram').value = 24; ctx.idleTypeChanged();
  assert(get('idleCores').disabled);
  assert(get('idleCpuUtil').disabled);
  assert(!get('idleVram').disabled);
  assert(!get('idleUtil').disabled);
  assert(!render().includes('· CPU'));
  assert.match(card('hybrid', 'GPU 0'), /badge ok/, 'CPU thresholds do not reject GPU');
  ctx.CURRENT_METRICS.hybrid.gpus[0].util_pct = null;
  assert.match(card('hybrid', 'GPU 0'), /badge warn/, 'missing usage is not zero usage');
  ctx.CURRENT_METRICS.hybrid.gpus[0].util_pct = 2;

  // Duration must be recent, continuous, and based on the worst sample per minute.
  get('idleType').value = 'all'; get('idleCores').value = 0; get('idleMin').value = 3;
  get('idleCpuUtil').value = 10;
  const buckets = [2, 1, 0].map(i => ({t: now / 1000 - i * 60, c: [2, 5], g: {'0': [1, 3]}}));
  ctx.TREND.hybrid = {buckets};
  assert.match(card('hybrid'), /badge ok/);
  assert.match(card('hybrid', 'GPU 0'), /badge ok/);
  assert.match(card('cpu-only'), /badge warn/, 'unknown history cannot prove duration');
  buckets[1].c = [2, 80];
  assert.match(card('hybrid'), /未达条件/);
  assert.match(card('hybrid'), /连续低利用率仅 1 分钟，少于要求的 3 分钟/);
  assert.match(card('hybrid', 'GPU 0'), /badge ok/, 'CPU and GPU history remain separate');
  get('idleCpuUtil').value = 100;
  assert.match(card('hybrid'), /符合条件/, 'CPU duration uses the CPU-specific threshold');
  get('idleCpuUtil').value = 10;
  buckets[1].c = [null, null];
  assert.match(card('hybrid'), /badge warn/);
  buckets[1].c = [2, 5];
  ctx.TREND.hybrid.buckets = [buckets[0], buckets[2]];
  assert.match(card('hybrid'), /badge warn/, 'history gaps are not idle minutes');
  ctx.TREND.hybrid.buckets = buckets;
  now += 180000;
  assert.match(card('hybrid'), /badge warn/, 'stale history cannot prove current idleness');
  get('idleMin').value = 0;
  assert.match(card('hybrid'), /badge ok/, 'snapshot mode does not need history');
  get('idleCpuUtil').value = 100; ctx.CURRENT_METRICS.busy.cpu.usage_pct = 100;
  assert.match(card('busy'), /未达条件/, 'fully busy CPUs have no idle cores');
  assert.match(card('busy'), /当前没有估算空闲核数/);

  await ctx.copyIdle({dataset: {desc: 'cpu-only CPU：估算空闲 30.4 / 32 核'}});
  assert.equal(copies[0], 'cpu-only CPU：估算空闲 30.4 / 32 核');
  ctx.ensureIdleHistory();
  await new Promise(resolve => setImmediate(resolve));
  const firstCount = requests.length;
  ctx.ensureIdleHistory();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(requests.length, firstCount, 'reuse history during the same minute');
  now += 60000;
  ctx.ensureIdleHistory();
  await new Promise(resolve => setImmediate(resolve));
  assert(requests.length > firstCount, 'refresh history as live metrics advance');

  ctx.STATE.servers = [];
  assert.match(render(), /暂无 CPU \/ GPU资源数据/);
  console.log('idle resources: CPU/GPU cards, thresholds, sorting, saved settings, missing data, continuous history and refresh passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
