"""Bounded, deterministic analysis of scheduler state and VASP log tails."""

from __future__ import annotations

import re
from typing import Any


_SIGNATURES = (
    (re.compile(r"out of memory|oom-kill|oom_kill", re.I),
     "内存不足（OOM）", "提高内存申请、减少并行进程或检查体系规模。"),
    (re.compile(r"time limit|due to time limit|walltime", re.I),
     "超过时限", "延长 walltime，或从 WAVECAR/CONTCAR 检查点续算。"),
    (re.compile(r"no space left|disk quota exceeded", re.I),
     "磁盘空间或配额不足", "清理计算目录并检查工作区配额。"),
    (re.compile(r"segmentation fault|segfault|signal 11", re.I),
     "程序段错误", "检查 VASP/数学库版本、并行参数和损坏的输入或重启文件。"),
    (re.compile(r"mpi_abort|mpi_abort|fatal error", re.I),
     "MPI 或运行时致命错误", "查看首个报错进程，核对模块环境与并行启动命令。"),
    (re.compile(r"zbrent|brmix|eddrmm|zhegv|sub-space-matrix", re.I),
     "VASP 数值稳定性错误", "按具体错误检查结构、混合参数、ALGO 与重启文件。"),
)


def analyze_job(*, job_id: str, scheduler_state: str,
                progress: dict[str, Any], logs: list[dict[str, str]]) -> dict:
    """Return a UI-ready diagnosis without executing model-generated code."""
    state = str(scheduler_state or "UNKNOWN").upper()
    combined = "\n".join(str(row.get("content") or "") for row in logs)
    findings: list[str] = []
    recommendations: list[str] = []
    for pattern, finding, recommendation in _SIGNATURES:
        if pattern.search(combined) and finding not in findings:
            findings.append(finding)
            recommendations.append(recommendation)
    for signature in progress.get("error_signatures") or []:
        label = f"VASP 错误特征：{signature}"
        if label not in findings:
            findings.append(label)
    scientific = progress.get("scientific_converged")
    terminal_failure = state in {
        "FAILED", "TIMEOUT", "CANCELLED", "CANCELED", "NODE_FAIL",
        "OUT_OF_MEMORY", "PREEMPTED",
    }
    if findings or terminal_failure:
        level, status = "error", "需要处理"
        summary = findings[0] if findings else f"调度器状态为 {state}"
    elif scientific is True:
        level, status = "success", "科学收敛"
        summary = "电子与离子收敛判据均已满足。"
    elif state == "RUNNING":
        level, status = "running", "运行中"
        steps = progress.get("ionic_steps")
        summary = (f"作业仍在运行，已识别 {steps} 个离子步。"
                   if steps is not None else "作业仍在运行，日志中暂未识别到致命错误。")
    elif state in {"PENDING", "QUEUED", "Q"}:
        level, status = "pending", "排队中"
        summary = "作业尚未开始运行，当前没有可判定的 VASP 运行结果。"
    elif state == "COMPLETED" and scientific is not True:
        level, status = "warning", "调度完成但未确认收敛"
        summary = "调度器已结束作业，但日志不足以证明 VASP 科学收敛。"
        recommendations.append("检查 OUTCAR/OSZICAR 完整性和收敛条件后再使用结果。")
    else:
        level, status = "unknown", "信息不足"
        summary = "尚无足够的调度器或 VASP 日志信息。"
    if not logs:
        recommendations.append("未发现常见 stdout/stderr 日志；已仅依据 VASP 进度文件判断。")
    return {
        "ok": True, "job_id": str(job_id), "level": level,
        "status": status, "summary": summary,
        "scheduler_state": state, "scientific_converged": scientific,
        "findings": findings, "recommendations": recommendations,
        "log_files": [row.get("path", "") for row in logs],
        "engine": "builtin-rules-v1",
    }
