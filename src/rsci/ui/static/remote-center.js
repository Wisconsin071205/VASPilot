/* Remote jobs and result collection. All remote/model text is escaped. */
(() => {
  "use strict";
  const el = id => document.getElementById(id);
  const terminal = new Set(["COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "CANCELED", "NODE_FAIL", "OUT_OF_MEMORY", "PREEMPTED", "BOOT_FAIL", "DEADLINE"]);
  const labels = {RUNNING:"运行中", PENDING:"排队中", SUBMITTED:"提交已接收", COMPLETING:"结束处理中", UNKNOWN:"状态待核对",
    COMPLETED:"调度完成", FAILED:"执行失败", TIMEOUT:"超过时限", CANCELLED:"已取消", CANCELED:"已取消",
    NODE_FAIL:"节点故障", OUT_OF_MEMORY:"内存不足", PREEMPTED:"被抢占", HELD:"已挂起", SUSPENDED:"已暂停"};
  const collectionLabels = {queued:"已入队", collecting:"回收中", retrying:"等待重试", blocked:"待处理", completed:"回收完成", cancelled:"已停止回收"};
  let rows = [], loading = false, detailVersion = 0, collectionLoading = false;

  function renderRows() {
    const server = el("fleet-server").value, filter = el("fleet-state").value;
    const visible = rows.filter(row => (!server || row.server === server) &&
      (filter === "all" || (filter === "active" && !terminal.has(row.state) && row.state !== "UNKNOWN") ||
       (filter === "finished" && terminal.has(row.state)) ||
       (filter === "attention" && (row.stale || row.state === "UNKNOWN" || (terminal.has(row.state) && row.state !== "COMPLETED")))));
    el("fleet-table").querySelector("tbody").innerHTML = visible.map((row, index) =>
      `<tr><td>${esc(row.server)}<div class="mono">${esc(row.job_id)}</div></td>
       <td>${esc(labels[row.state] || row.state)}${row.stale ? '<div class="desc">最后已知状态 · 待核对</div>' : ""}
       ${row.last_known_state ? `<div class="desc">上次：${esc(labels[row.last_known_state] || row.last_known_state)}</div>` : ""}</td>
       <td>${esc(row.name || "未命名")}<div class="jobdir">${esc(row.directory || "目录待核对")}</div></td>
       <td>${esc(row.project || (row.project_missing ? "原项目已删除" : "未关联"))}</td>
       <td>${esc(fmtLocalDateTime(row.last_seen))}</td>
       <td>${row.read_only_reason ? `<span class="desc">${esc(row.read_only_reason)}</span>` : `<button class="btn ghost" data-detail="${index}">查看详情</button>`}</td></tr>`).join("") ||
      '<tr><td colspan="6" class="empty">没有符合筛选条件的作业。可刷新服务器或调整筛选。</td></tr>';
    el("fleet-table").querySelectorAll("[data-detail]").forEach(button => {
      button.addEventListener("click", () => openDetail(visible[Number(button.dataset.detail)]));
    });
  }

  window.loadRemoteJobs = async () => {
    if (loading) return;
    loading = true;
    const button = el("fleet-refresh");
    button.disabled = true; button.textContent = "正在核对…";
    el("fleet-status").textContent = "正在查询已配置服务器，保留上次记录…";
    try {
      const result = await api("job.center", {refresh:false}, true);
      if (!result.ok) throw new Error(result.error?.message || "查询失败");
      rows = result.jobs || [];
      const previous = el("fleet-server").value;
      el("fleet-server").innerHTML = '<option value="">全部服务器</option>' + (result.servers || []).map(server =>
        `<option value="${esc(server.server)}">${esc(server.server)}</option>`).join("");
      if ((result.servers || []).some(server => server.server === previous)) el("fleet-server").value = previous;
      renderRows();
      const servers = (result.servers || []).map(server => ({...server, querying:true}));
      const status = () => {el("fleet-status").textContent = servers.map(server =>
        `${server.server}：${server.querying ? "正在核对…" : server.error ? "查询失败，" + server.error : "核对于 " + fmtLocalDateTime(server.checked_at)}${server.warning ? "；历史记录暂不完整：" + server.warning : ""}${server.submission_warning ? "；提交记录读取失败：" + server.submission_warning : ""}`).join("\n") ||
        "尚未配置服务器。请在配置页添加并连接服务器。";};
      status();
      let next = 0;
      const worker = async () => {
        while (next < servers.length) {
          const index = next++, name = servers[index].server;
          const response = await api("job.center", {server:name}, true);
          if (response.ok) {
            rows = rows.filter(row => row.server !== name).concat(response.jobs || []);
            servers[index] = {...response.servers[0], querying:false};
          } else {
            rows = rows.map(row => row.server === name ? {...row, stale:true} : row);
            servers[index] = {...servers[index], querying:false, error:response.error?.message || "查询失败"};
          }
          status(); renderRows();
        }
      };
      await Promise.all([worker(), worker(), worker()]);
    } catch (error) {
      el("fleet-status").textContent = "核对失败，保留上次记录。请检查连接后刷新：" + error.message;
    } finally {
      loading = false; button.disabled = false; button.textContent = "刷新所有服务器";
      loadCollections();
    }
  };

  async function openDetail(row) {
    const version = ++detailVersion, box = el("fleet-detail");
    box.hidden = false;
    box.innerHTML = `<h3>${esc(row.server)} / ${esc(row.job_id)}</h3><p role="status">正在核对工作目录和结果文件…</p>`;
    const result = await api("job.details", {server:row.server, job_id:row.job_id}, true);
    if (version !== detailVersion) return;
    if (!result.ok) {
      box.innerHTML = `<h3>详情暂不可用</h3><p role="alert">${esc(result.error?.message || "查询失败")}。请检查连接后重试，原记录已保留。</p><button class="btn ghost" data-retry>重新核对</button>`;
      box.querySelector("[data-retry]").addEventListener("click", () => openDetail(row));
      return;
    }
    const job = result.job, associated = job.association;
    box.innerHTML = `<div class="row" style="justify-content:space-between"><h3>${esc(row.server)} / ${esc(row.job_id)} · ${esc(labels[job.state] || job.state)}</h3>
      <button class="btn ghost" data-close>收起详情</button></div>
      <p class="mono" style="overflow-wrap:anywhere">${esc(result.directory)}</p>
      ${(result.warnings || []).map(warning => `<p class="desc">${esc(warning)}</p>`).join("")}
      <section class="job-log-panel" aria-label="作业日志">
        <div class="row"><h4>运行日志</h4><button class="btn ghost" data-refresh-logs>重新查找日志</button>
          <button class="btn" data-ai-logs disabled>AI 分析日志</button></div>
        <p class="desc">自动按提交脚本、作业号和常见日志名查找。AI 分析会将日志摘录发送给当前默认模型，建议需审阅后执行。</p>
        <p data-log-status role="status" aria-live="polite">正在查找并读取日志…</p>
        <div data-log-list></div><div data-ai-result aria-live="polite"></div>
      </section>
      <div class="row"><div class="fg"><label for="fleet-project">关联项目</label><select id="fleet-project" ${associated ? "disabled" : ""}>
        <option value="">选择项目…</option>${PROJECTS.map(project => `<option value="${esc(project.name)}" ${associated?.project_id === project.project_id ? "selected" : ""}>${esc(project.name)}</option>`).join("")}</select></div>
        <button class="btn" data-associate ${associated ? "disabled" : ""}>${associated ? "已关联项目" : "确认关联"}</button>
        <button class="btn ghost" data-diagnose>诊断作业</button><button class="btn ghost" data-files>打开远端目录</button></div>
      <p class="desc">关联只登记项目关系，不提交、重跑或取消作业。回收文件将保存到项目的 results/remote 目录。</p>
      <div data-detail-status role="status" aria-live="polite"></div>
      <details><summary>全部脚本、日志与结果文件（${result.files.length}）</summary>
        <div style="max-height:340px;overflow:auto"><table><thead><tr><th>选择回收</th><th>文件</th><th>大小</th><th>查看</th></tr></thead><tbody>
        ${result.files.map((file, index) => `<tr><td><input type="checkbox" data-collect-file="${index}" aria-label="回收 ${esc(file.path.split("/").pop())}"></td>
          <td>${esc(file.path.split("/").pop())}</td><td>${esc(fmtBytes(file.size))}</td><td><button class="btn ghost" data-preview="${index}">文本尾部</button></td></tr>`).join("") || '<tr><td colspan="4" class="empty">目录中未找到可展示的普通文件</td></tr>'}
        </tbody></table></div>
        <button class="btn" data-collect ${!associated || !terminal.has(job.state) ? "disabled" : ""}>回收选中文件</button>
        ${!associated ? '<p class="desc">先关联项目，再选择文件回收。</p>' : !terminal.has(job.state) ? '<p class="desc">作业结束且状态核对后才能回收。</p>' : ""}</details>
      <div data-preview-box></div><div data-diagnosis></div>`;
    box.querySelector("[data-close]").addEventListener("click", () => {++detailVersion; box.hidden = true;});
    box.querySelector("[data-files]").addEventListener("click", () => {
      el("fsrv").value = row.server; el("fpath").value = result.directory;
      document.querySelector('.tab[data-v="files"]').click(); browse();
    });
    const act = async (button, action, body) => {
      button.disabled = true;
      try {
        const response = await api(action, {server:row.server, job_id:row.job_id, ...body}, true);
        if (version === detailVersion && !response.ok) box.querySelector("[data-detail-status]").textContent = response.error?.message || "操作失败，请重试";
        return response;
      } finally {button.disabled = false;}
    };
    let logBusy = false, aiBusy = false, hasLogs = false;
    const logStatus = box.querySelector("[data-log-status]");
    const refreshLogs = box.querySelector("[data-refresh-logs]");
    const aiLogs = box.querySelector("[data-ai-logs]");
    const syncLogButtons = () => {
      refreshLogs.disabled = logBusy || aiBusy;
      aiLogs.disabled = logBusy || aiBusy || !hasLogs;
      aiLogs.textContent = aiBusy ? "正在分析…" : "AI 分析日志";
    };
    const showLogs = response => {
      const logs = response.logs || [];
      hasLogs = logs.length > 0;
      const relation = {job_id_verified:"日志内作业号已核对", filename_job_id:"文件名作业号匹配", directory_only:"归属待核对"};
      box.querySelector("[data-log-list]").innerHTML =
        (response.execution_receipts || []).map(receipt => `<p><strong>${esc(receipt.program)} 退出码 ${esc(receipt.exit_code)}</strong> · ${esc(receipt.end_text)}<br><span class="desc">来自同一份带有本次作业号的日志：${esc(receipt.path)}。程序退出与科学有效性分别判断。</span></p>`).join("") +
        (response.warnings || []).map(text => `<p class="desc">${esc(text)}</p>`).join("") +
        logs.map((log, index) => `<details class="job-log-entry" ${index === 0 ? "open" : ""}>
          <summary>${esc(log.path.split("/").pop())} · ${esc(relation[log.relation] || "归属待核对")}</summary>
          <p class="desc">${esc(log.reason)} · ${esc(log.scope)}${log.truncated ? " · 摘录已截断" : ""}</p>
          <p class="job-log-path mono">${esc(log.path)}</p><pre>${esc(log.content || "（日志为空，尚无输出）")}</pre></details>`).join("") +
        (logs.length ? "" : '<p class="empty">尚未找到可读取的日志。作业可能尚未输出，或脚本把日志写到了其他目录；可查看下方全部文件或重新查找。</p>') +
        ((response.excluded || []).length ? `<details><summary>未用于本次分析的文件（${response.excluded.length}）</summary><ul>${response.excluded.map(item => `<li>${esc(item.path)}：${esc(item.reason)}</li>`).join("")}</ul></details>` : "");
    };
    const fetchLogs = async (analyze = false) => {
      if (logBusy || aiBusy) return;
      if (analyze) aiBusy = true; else logBusy = true;
      syncLogButtons();
      logStatus.textContent = analyze ? "正在核对最新日志并调用默认模型，原始日志仍可查看…" : "正在查找并读取日志…";
      try {
        const response = await api(analyze ? "job.ai_analysis" : "job.logs_collect",
          {server:row.server, job_id:row.job_id}, true, analyze ? 180000 : 90000);
        if (version !== detailVersion) return;
        if (!response.ok) {
          logStatus.textContent = (analyze ? "AI 分析未完成：" : "日志读取未完成：") +
            (response.error?.message || "连接失败") + "。已有日志保留，可重试。";
          return;
        }
        if (!response.analysis) box.querySelector("[data-ai-result]").textContent = "";
        showLogs(response);
        logStatus.textContent = `已读取 ${response.logs.length} 个日志 · ${fmtLocalDateTime(response.checked_at)}`;
        if (response.analysis) {
          const analysis = response.analysis;
          box.querySelector("[data-ai-result]").innerHTML = `<h4>AI 日志分析</h4>
            <p class="desc">${esc(analysis.model)} · ${esc(fmtLocalDateTime(analysis.created_at))} · 基于上方日志摘录，建议尚未执行</p>
            ${analysis.truncated ? '<p class="desc">分析内容较长，仅显示前 24000 字符。</p>' : ""}
            <div class="job-ai-text">${esc(analysis.text)}</div>`;
        }
      } catch (error) {
        if (version === detailVersion) logStatus.textContent = `读取或分析失败：${error.message}。可重试。`;
      } finally {
        logBusy = aiBusy = false;
        if (version === detailVersion) syncLogButtons();
      }
    };
    refreshLogs.addEventListener("click", () => fetchLogs());
    aiLogs.addEventListener("click", () => fetchLogs(true));
    fetchLogs();
    box.querySelector("[data-associate]").addEventListener("click", async event => {
      const project = el("fleet-project").value;
      if (!project) return toast("请选择要关联的项目", "err");
      const response = await act(event.currentTarget, "job.associate", {project});
      if (response.ok && version === detailVersion) {await openDetail(row); loadRemoteJobs();}
    });
    box.querySelectorAll("[data-preview]").forEach(button => button.addEventListener("click", async () => {
      const response = await act(button, "job.logs", {path:result.files[Number(button.dataset.preview)].path});
      if (response.ok && version === detailVersion) box.querySelector("[data-preview-box]").innerHTML =
        `<h4>${esc(response.path.split("/").pop())}</h4><p class="desc">${esc(response.scope)}</p><pre style="max-height:360px;overflow:auto">${esc(response.content)}</pre>`;
    }));
    box.querySelector("[data-diagnose]").addEventListener("click", async event => {
      const response = await act(event.currentTarget, "job.diagnosis", {});
      if (!response.ok || version !== detailVersion) return;
      const d = response.diagnosis;
      box.querySelector("[data-diagnosis]").innerHTML = `<h4>${esc(d.status)}：${esc(d.summary)}</h4>
        <p class="desc">内置规则诊断；建议尚未执行。调度器完成与计算结果正确性分别判断。</p>
        <ul>${d.recommendations.map(text => `<li>${esc(text)}</li>`).join("")}</ul>
        ${(d.matches || []).map(match => `<details><summary>${esc(match.finding)} · ${esc(match.path.split("/").pop())}</summary><pre>${esc(match.excerpt)}</pre></details>`).join("")}
        ${(response.warnings || []).map(text => `<p class="desc">${esc(text)}</p>`).join("")}`;
    });
    box.querySelector("[data-collect]").addEventListener("click", async event => {
      const paths = [...box.querySelectorAll("[data-collect-file]:checked")].map(input => result.files[Number(input.dataset.collectFile)].path);
      if (!paths.length || paths.length > 100) return toast("请选择 1–100 个文件", "err");
      const response = await act(event.currentTarget, "collection.enqueue", {paths});
      if (version === detailVersion && response.ok) box.querySelector("[data-detail-status]").textContent =
        `回收记录已保存：${response.task_id}。可在下方查看状态。`;
      loadCollections();
    });
  }

  window.loadCollections = async () => {
    if (collectionLoading) return;
    collectionLoading = true;
    try {
      const result = await api("collection.list", {}, true);
      if (!result.ok) return;
      el("collection-list").innerHTML = (result.tasks || []).map((task, index) =>
        `<div style="padding:12px 0;border-bottom:1px solid var(--border)">
          <strong>${esc(task.server)} / ${esc(task.job_id)} · ${esc(collectionLabels[task.status] || task.status)}</strong>
          <span> ${task.completed_files} / ${task.files.length} 个文件</span>
          <p class="desc">${esc(task.result_directory)}</p>
          ${task.error ? `<p role="status">${esc(task.error)}</p>` : ""}
          ${task.status === "retrying" ? `<p class="desc">下次重试：${esc(fmtLocalDateTime(task.next_retry_at))}</p>` : ""}
          ${task.status === "completed" ? '<span>已保存文件 SHA-256 · 可在项目结果目录核对</span>' :
            `<div class="row">${!task.worker_active ? `<button class="btn ghost" data-resume="${index}">继续回收</button>` : '<span>后台执行中</span>'}
            ${task.status !== "cancelled" ? `<button class="btn ghost" data-stop="${index}">停止回收</button>` : ""}</div>`}</div>`).join("") || "尚无回收任务。在作业详情中关联项目并选择结果文件。";
      for (const warning of result.attention || []) {
        const note = document.createElement("p"); note.textContent = `${warning.record}：${warning.error}`;
        el("collection-list").appendChild(note);
      }
      el("collection-list").querySelectorAll("[data-resume], [data-stop]").forEach(button => {
        button.addEventListener("click", async () => {
          button.disabled = true;
          const resume = button.hasAttribute("data-resume");
          const task = result.tasks[Number(resume ? button.dataset.resume : button.dataset.stop)];
          try {
            const response = await api(resume ? "collection.resume" : "collection.cancel", {task_id:task.task_id}, true);
            if (!response.ok) toast(response.error?.message || "操作失败", "err");
          } finally {button.disabled = false; loadCollections();}
        });
      });
    } finally {collectionLoading = false;}
  };
  el("fleet-server").addEventListener("change", renderRows);
  el("fleet-state").addEventListener("change", renderRows);
  el("fleet-status").style.whiteSpace = "pre-line";
  setInterval(() => {if (el("v-jobs").classList.contains("on") && !document.hidden) loadCollections();}, 4000);
})();
