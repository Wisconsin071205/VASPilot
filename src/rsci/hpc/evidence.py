"""Organize attributable Slurm snapshots without inventing missing metrics."""
from datetime import datetime, timezone
import json
import re


FIELDS = {
    "sacct": "job_id name cluster partition state submit start end elapsed cpus requested_memory max_rss max_vm exit_code nodes".split(),
    "squeue": "job_id state elapsed time_limit partition reason_or_nodes submit expected_start".split(),
    "sstat": "job_id average_rss max_rss max_vm average_cpu".split(),
}


def organize_evidence(snapshot: dict, *, job_id: str, server: str,
                      directory: str, logs: list[dict], warnings: list[str]) -> dict:
    evidence = dict(snapshot)
    evidence.update(job_id=job_id, server=server, directory=directory,
                    collected_at=snapshot.get("collected_at") or datetime.now(timezone.utc).isoformat(),
                    logs=logs, warnings=[*(snapshot.get("warnings") or []), *warnings])
    tables = {}
    for source, fields in FIELDS.items():
        rows = []
        for line in snapshot.get("sources", {}).get(source, {}).get("raw", "").splitlines():
            values = [value.strip() for value in line.split("|")]
            if len(values) < len(fields):
                continue
            row = dict(zip(fields, values))
            if row["job_id"] == job_id or row["job_id"].startswith(job_id + "."):
                rows.append(row)
        tables[source] = rows
    evidence["records"] = tables
    job = next((row for row in tables["sacct"] if row["job_id"] == job_id), {})
    queue = next((row for row in tables["squeue"] if row["job_id"] == job_id), {})
    summary = {key: job.get(key) or None for key in FIELDS["sacct"] if key != "job_id"}
    for key in ("state", "elapsed", "partition"):
        summary[key] = queue.get(key) or summary.get(key)
    summary["queue_reason_or_nodes"] = queue.get("reason_or_nodes") or None
    summary["queue_wait_seconds"] = None
    # Only actual accounting start times count; squeue start is an estimate.
    try:
        start = datetime.fromisoformat(job.get("start", ""))
        submit = datetime.fromisoformat(job.get("submit", ""))
        if start >= submit:
            summary["queue_wait_seconds"] = int((start - submit).total_seconds())
    except (ValueError, TypeError):
        pass
    evidence["summary"] = summary
    evidence["notes"] = [
        "本材料为集群返回数据的采集快照；平台名称由服务器配置识别，未作平台认证。",
        "MaxRSS 是作业步骤内单个任务的最大常驻内存，不等于整个作业总内存；ReqMem 是申请值。",
        "sstat 为运行中采样，sacct 为记账记录；空值或零值可能来自集群未启用相应统计。",
        "等待时长按 sacct 实际开始时间减提交时间计算，包含保持等等待；排队快照不代表完整排队历史。",
        "日志为所选目录的有界摘录，共用目录中的通用日志需人工核对作业归属。",
    ]
    if not tables["sacct"] and not tables["squeue"]:
        evidence["warnings"].append("未取得匹配此作业 ID 的调度记录，当前材料不足以证明实际运行。")
    if not any(row.get("max_rss") for source in ("sacct", "sstat") for row in tables[source]):
        evidence["warnings"].append("未取得内存占用数据；请在运行中重试，或检查集群记账配置。")
    if not logs:
        evidence["warnings"].append("未取得运行日志摘录。")
    return evidence


def evidence_markdown(evidence: dict) -> str:
    lines = ["# 算力集群实际运行证明", ""]
    for key, label in (("server", "服务器"), ("scheduler", "调度器"),
                       ("job_id", "作业 ID"), ("directory", "计算目录"),
                       ("collected_at", "采集时间（UTC）")):
        lines.append(f"- {label}：{evidence.get(key) or '未获取'}")
    lines += ["", "## 运行摘要", "", "```json",
              json.dumps(evidence["summary"], ensure_ascii=False, indent=2), "```"]
    for title, values in (("数据说明", evidence["notes"]), ("缺失与告警", evidence["warnings"])):
        lines += ["", f"## {title}", "", *[f"- {value}" for value in values]]
    for name, source in evidence.get("sources", {}).items():
        raw = source.get("raw") or source.get("error") or "无记录"
        fence = "`" * max(3, max((len(s) for s in re.findall(r'`+', raw)), default=0) + 1)
        lines += ["", f"## {name} 原始记录（{source.get('status')}）", "",
                  source.get("command", ""), "", fence + "text", raw, fence]
    for log in evidence["logs"]:
        raw = log["content"]
        fence = "`" * max(3, max((len(s) for s in re.findall(r'`+', raw)), default=0) + 1)
        lines += ["", f"## 日志摘录：{log['path']}", "", fence + "text", raw, fence]
    return "\n".join(lines) + "\n"
