"""MCP Apps resource for the read-only VASPilot fleet dashboard.

The widget deliberately contains no remote write controls.  It only invokes
the named ``fleet_snapshot`` and ``vasp_progress`` MCP tools, so all remote
access remains inside the existing gateway and server-root checks.
"""

from __future__ import annotations


FLEET_DASHBOARD_RESOURCE_URI = "ui://vaspilot/fleet-dashboard.html"


# The component is intentionally framework-free and self-contained: Codex
# loads MCP Apps resources in an isolated iframe, so no network/CDN asset is
# necessary.  All scheduler data is assigned through textContent, never HTML.
FLEET_DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>VASPilot 集群作业面板</title>
  <style>
    :root { color-scheme: dark; font-family: ui-sans-serif, system-ui, "Microsoft YaHei", sans-serif;
      --bg:#111715; --card:#18201d; --line:#2b3731; --muted:#9ba8a1; --text:#ecf5ee;
      --green:#92d4ae; --red:#ee9186; --amber:#e4bc73; --blue:#8cbcf2; }
    * { box-sizing: border-box; }
    body { margin:0; background:var(--bg); color:var(--text); }
    main { padding:18px; max-width:1240px; margin:auto; }
    header { display:flex; gap:14px; align-items:flex-start; justify-content:space-between; border-bottom:1px solid var(--line); padding-bottom:15px; }
    h1 { font-size:22px; margin:2px 0 5px; letter-spacing:.02em; }
    .eyebrow { margin:0; font-size:11px; letter-spacing:.24em; color:var(--muted); font-weight:700; }
    .muted { color:var(--muted); font-size:13px; }
    .actions { display:flex; gap:8px; align-items:center; flex-wrap:wrap; justify-content:flex-end; }
    button { background:#1d2923; color:var(--text); border:1px solid #3a4c42; padding:8px 11px; border-radius:8px; cursor:pointer; font:inherit; }
    button:hover { border-color:var(--green); } button:disabled { opacity:.5; cursor:wait; }
    .live { display:inline-flex; gap:6px; align-items:center; padding:6px 9px; border:1px solid var(--line); border-radius:999px; font-size:12px; color:var(--muted); }
    .dot { width:8px; height:8px; border-radius:50%; background:var(--green); display:inline-block; }
    #summary { display:flex; gap:8px; flex-wrap:wrap; margin:15px 0; }
    .chip { padding:7px 10px; border:1px solid var(--line); border-radius:999px; color:var(--muted); font-size:13px; }
    .chip strong { color:var(--text); margin-left:4px; }
    #grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:14px; }
    .card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:15px; min-height:230px; }
    .card-head { display:flex; justify-content:space-between; gap:10px; align-items:flex-start; }
    .name { display:flex; gap:8px; align-items:center; font-size:21px; font-weight:750; }
    .status { font-size:12px; border-radius:999px; padding:5px 8px; white-space:nowrap; }
    .online { color:var(--green); background:#1e3329; } .offline { color:var(--red); background:#3b2927; }
    .reconnect { color:var(--amber); background:#393220; }
    .auth { font-size:11px; border-radius:999px; padding:4px 7px; color:var(--muted); border:1px solid var(--line); }
    .scheduler { margin:5px 0 13px; font-size:13px; color:var(--muted); }
    .counts { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:13px; }
    .count { background:#121916; padding:10px; border-radius:9px; } .count b { display:block; font-size:19px; } .count span { font-size:11px; color:var(--muted); }
    .jobs { min-height:37px; border-top:1px solid var(--line); padding-top:9px; font-size:12px; }
    .job { display:grid; grid-template-columns:1fr auto; gap:7px; padding:6px 0; border-bottom:1px solid #26322c; }
    .job:last-child { border-bottom:0; } .job-detail { color:var(--muted); font-size:11px; padding-top:2px; }
    .state { font-size:11px; border-radius:5px; padding:3px 5px; height:max-content; } .running{color:var(--green);background:#1e3329}.pending{color:var(--amber);background:#393220}.failed{color:var(--red);background:#3b2927}.unknown{color:var(--muted);background:#26322c}
    .case { border-top:1px solid var(--line); margin-top:10px; padding-top:10px; }
    .case-row { display:flex; gap:7px; } input { min-width:0; flex:1; background:#111815; color:var(--text); border:1px solid #334238; border-radius:7px; padding:7px; font:inherit; font-size:12px; }
    .case-result { font-size:12px; color:var(--muted); padding-top:7px; word-break:break-word; }
    .error { color:var(--red); }.empty { color:var(--muted); font-size:13px; padding:12px 0; }
    @media (max-width:600px) { main { padding:12px; } header { display:block; } .actions { justify-content:flex-start; margin-top:12px; } }
  </style>
</head>
<body>
  <main>
    <header>
      <div><p class="eyebrow">VASPILOT · READ-ONLY FLEET MONITOR</p><h1>远端集群作业面板</h1><div id="updated" class="muted">等待首个快照…</div></div>
      <div class="actions"><span class="live"><i class="dot"></i><span id="live-label">LIVE</span></span><button id="refresh" type="button">刷新</button></div>
    </header>
    <div id="summary"></div><section id="grid"><p class="empty">正在读取已注册服务器…</p></section>
  </main>
  <script>
  (() => {
    const grid = document.getElementById('grid'); const summary = document.getElementById('summary');
    const refreshButton = document.getElementById('refresh'); const updated = document.getElementById('updated');
    const liveLabel = document.getElementById('live-label'); let pending = new Map(), nextId = 1, timer = null;
    let settings = { servers: undefined, refresh_seconds: 30 }, latest = null;

    function request(method, params) { const id = nextId++; window.parent.postMessage({jsonrpc:'2.0', id, method, params}, '*'); return new Promise((resolve,reject) => pending.set(id,{resolve,reject})); }
    function text(value) { return value === undefined || value === null || value === '' ? '—' : String(value); }
    function stateClass(value) { const v=String(value||'').toUpperCase(); return v.includes('RUN') ? 'running' : (v.includes('PEND') || v==='Q' ? 'pending' : (v.includes('FAIL') || v.includes('CANCEL') || v.includes('TIMEOUT') ? 'failed':'unknown')); }
    function el(tag, className, value) { const node=document.createElement(tag); if(className) node.className=className; if(value !== undefined) node.textContent=value; return node; }
    function addChip(label, value) { const chip=el('span','chip'); chip.textContent=label+' '; const strong=el('strong','',text(value)); chip.append(strong); summary.append(chip); }
    function render(payload) {
      const snapshot = payload && (payload.snapshot || payload); if (!snapshot || !Array.isArray(snapshot.servers)) return;
      latest=snapshot; settings.refresh_seconds = Number(payload.refresh_seconds || settings.refresh_seconds || 30);
      summary.replaceChildren(); addChip('在线', (snapshot.connected||0)+'/'+(snapshot.total||0));
      const active = snapshot.servers.reduce((total,s) => total + Number(s.active_jobs||0),0); addChip('活动作业',active); addChip('刷新间隔',settings.refresh_seconds+' 秒');
      updated.textContent = '最近快照：'+text(snapshot.generated_at)+' · 调度器状态与科学收敛分开显示';
      grid.replaceChildren();
      if (!snapshot.servers.length) { grid.append(el('p','empty','没有已注册服务器。')); return; }
      snapshot.servers.forEach(server => grid.append(serverCard(server)));
      schedule();
    }
    function connState(server) { if(server.connected) return {t:'在线',c:'online'};
      const st=String(server.reconnect_state||''); const err=String(server.last_connect_error||'');
      if(server.auth_mode==='key'){
        if(st==='host_key_failed'||err.includes('host_key')) return {t:'主机指纹异常（已停止自动重连）',c:'offline'};
        if(err.includes('key_rejected')) return {t:'公钥被拒绝',c:'offline'};
        if(err.includes('network')) return {t:'网络不可达',c:'offline'};
        if(st==='waiting_backoff') return {t:'自动重连中 · 等待退避 '+(server.retry_in||0)+'s',c:'reconnect'};
        if(st==='retrying') return {t:'自动重连中',c:'reconnect'}; }
      return {t:'离线 · 需要登录',c:'offline'}; }
    function serverCard(server) {
      const card=el('article','card'); const head=el('div','card-head'); const title=el('div'); const name=el('div','name'); name.append(el('i','dot')); name.append(document.createTextNode(text(server.server))); title.append(name); title.append(el('div','scheduler','调度器 '+text(server.scheduler_detected || server.scheduler || 'auto')+' · 认证 '+(server.auth_mode==='key'?'密钥':'交互'))); head.append(title);
      const conn=connState(server); const status=el('span','status '+conn.c,conn.t); head.append(status); card.append(head);
      const counts=el('div','counts'); const first=el('div','count'); first.append(el('b','',text(server.active_jobs||0))); first.append(el('span','','运行/排队作业')); const second=el('div','count'); second.append(el('b','',server.states && server.states.length ? server.states.join(' · ') : '—')); second.append(el('span','','当前状态')); counts.append(first,second); card.append(counts);
      const jobs=el('div','jobs'); const rows=Array.isArray(server.jobs)?server.jobs:[]; if(rows.length){ rows.slice(0,20).forEach(job=>{const row=el('div','job');const left=el('div'); left.append(el('div','', '作业 '+text(job.job_id)+(job.name?' · '+job.name:''))); left.append(el('div','job-detail',[job.elapsed && '已运行 '+job.elapsed,job.partition && '队列 '+job.partition,job.nodes && '节点 '+job.nodes].filter(Boolean).join(' · ') || '调度器未提供更多字段')); row.append(left,el('span','state '+stateClass(job.state),text(job.state)));jobs.append(row);}); } else { jobs.append(el('div','empty', server.error || server.jobs_error || '当前没有活动作业')); } card.append(jobs);
      const caseBox=el('div','case'); const row=el('div','case-row'); const input=document.createElement('input'); input.placeholder='计算目录，例如 /.../00_relax'; input.setAttribute('aria-label',text(server.server)+' 计算目录'); const button=el('button','', '科学进度'); button.type='button'; const result=el('div','case-result',''); button.onclick=async()=>{ if(!input.value.trim()) { result.textContent='请输入该服务器已绑定根目录内的计算目录。'; return; } button.disabled=true; result.textContent='读取 VASP 收敛信息…'; try { const reply=await request('tools/call',{name:'vasp_progress',arguments:{server:server.server,directory:input.value.trim()}}); const data=reply && reply.structuredContent; result.textContent=progressText(data); } catch(error) { result.textContent='无法读取：'+String(error && error.message || error); result.classList.add('error'); } finally { button.disabled=false; } }; row.append(input,button); caseBox.append(row,result); card.append(caseBox); return card;
    }
    function progressText(data) { if(!data) return '未得到科学进度。'; const state=data.scientific_state || data.state || '未知'; const bits=['科学状态 '+state]; if(data.ionic_steps !== undefined) bits.push('离子步 '+data.ionic_steps); if(data.electronic_steps !== undefined) bits.push('电子步 '+data.electronic_steps); if(data.converged !== undefined) bits.push(data.converged?'已收敛':'未确认收敛'); return bits.join(' · '); }
    async function refresh() { refreshButton.disabled=true; liveLabel.textContent='读取中'; try { const reply=await request('tools/call',{name:'fleet_snapshot',arguments:{servers:settings.servers}}); render(reply && reply.structuredContent); liveLabel.textContent='LIVE'; } catch(error) { liveLabel.textContent='刷新失败'; updated.textContent='无法刷新：'+String(error && error.message || error); } finally { refreshButton.disabled=false; } }
    function schedule() { if(timer) clearInterval(timer); const seconds=Math.min(300,Math.max(10,Number(settings.refresh_seconds)||30)); timer=setInterval(refresh,seconds*1000); }
    refreshButton.onclick=refresh;
    window.addEventListener('message',(event)=>{ if(event.source!==window.parent)return; const message=event.data; if(!message||message.jsonrpc!=='2.0')return; if(message.id!==undefined&&pending.has(message.id)){const item=pending.get(message.id);pending.delete(message.id);message.error?item.reject(message.error):item.resolve(message.result);return;} const params=message.params||{}; if(message.method==='ui/initialize'){ const input=params.toolInput||params.input||{}; settings={...settings,...input}; const result=params.toolResult||params.toolOutput||params; render(result.structuredContent||result); } if(message.method==='ui/notifications/tool-input'){settings={...settings,...params};} if(message.method==='ui/notifications/tool-result'){render(params.structuredContent||params);} },{passive:true});
  })();
  </script>
</body>
</html>"""


def resource_definition() -> dict:
    """Return the standards-compliant resource catalog entry."""
    return {
        "uri": FLEET_DASHBOARD_RESOURCE_URI,
        "name": "VASPilot Fleet Dashboard",
        "description": "Read-only multi-server scheduler and VASP progress monitor",
        "mimeType": "text/html;profile=mcp-app",
        "_meta": {"ui": {"prefersBorder": True}},
    }


def resource_contents() -> dict:
    """Return the isolated iframe source for ``resources/read``."""
    return {
        "contents": [{
            "uri": FLEET_DASHBOARD_RESOURCE_URI,
            "mimeType": "text/html;profile=mcp-app",
            "text": FLEET_DASHBOARD_HTML,
            "_meta": {"ui": {"prefersBorder": True}},
        }]
    }
