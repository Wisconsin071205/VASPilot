const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const html = fs.readFileSync(process.argv[2], 'utf8');
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
const nodes = new Map(), log = {children:[]};
const ctx = {CURRENT_SESSION:'review',
  document:{getElementById:id=>nodes.get(id),createElement:()=>({dataset:{},innerHTML:''})},
  esc:s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  placeChatCard:(log,card)=>{nodes.set(card.id,card);log.children.push(card);}};
vm.createContext(ctx);
vm.runInContext(html.slice(html.indexOf('function addConfirmCard('),html.indexOf('async function confirmSubmit(')),ctx);
const entry={id:'ps-review',session_id:'review',status:'pending',server:'cl9',directory:'/run',script:'run.sh'};
ctx.addConfirmCard(log,entry);
const card=nodes.get('cf-ps-review');
assert(card.innerHTML.includes('未保存脚本快照'));
const script='#!/bin/bash\n#PBS -q short\necho "<img src=x onerror=alert(1)>"\n';
ctx.addConfirmCard(log,{...entry,script_content:script});
assert.equal(log.children.length,1);
assert(card.innerHTML.includes('#PBS -q short'));
assert(card.innerHTML.includes('&lt;img'));
assert(!card.innerHTML.includes('<img'));
assert(!card.innerHTML.includes('未保存脚本快照'));
assert(card.innerHTML.includes('批准提交'));
const rendered=card.innerHTML;
ctx.addConfirmCard(log,{...entry,script_content:script});
assert.equal(card.innerHTML,rendered);
ctx.addConfirmCard(log,{...entry,status:'uncertain',script_content:script});
assert(card.innerHTML.includes('#PBS -q short'));
assert(!card.innerHTML.includes('onclick="confirmSubmit'));
console.log('frozen script preview, in-place recovery and HTML escaping passed');
