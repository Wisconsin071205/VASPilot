"""Bounded, deterministic analysis of scheduler state and VASP log tails."""

from __future__ import annotations

import re
from typing import Any


_SIGNATURES = (
    (re.compile(r"out of memory|oom-kill|oom_kill", re.I),
     "内存不足（OOM）", "提高内存申请、减少并行进程或检查体系规模。"),
    (re.compile(r"due to time limit|time limit (?:exceeded|reached)|walltime[^\n]*(?:exceeded|limit exceeded)", re.I),
     "超过时限", "核对运行时长和检查点，审阅新的 walltime 申请后再执行。"),
    (re.compile(r"no space left|disk quota exceeded", re.I),
     "磁盘空间或配额不足", "清理计算目录并检查工作区配额。"),
    (re.compile(r"segmentation fault|segfault|signal 11", re.I),
     "程序段错误", "检查程序与数学库版本、并行参数和损坏的输入或重启文件。"),
    (re.compile(r"ModuleNotFoundError|ImportError|command not found|No module named", re.I),
     "依赖或可执行程序缺失", "核对作业脚本使用的环境、模块和程序路径；审批修改后再执行。"),
    (re.compile(r"Permission denied", re.I),
     "文件或程序访问被拒绝", "检查输入、输出目录及程序的访问权限。"),
    (re.compile(r"mpi_abort|mpi_abort|fatal error", re.I),
     "MPI 或运行时致命错误", "查看首个报错进程，核对模块环境与并行启动命令。"),
    (re.compile(r"zbrent|brmix|eddrmm|zhegv|sub-space-matrix", re.I),
     "VASP 数值稳定性错误", "按具体错误检查结构、混合参数、ALGO 与重启文件。"),
)


def analyze_job(*, job_id: str, scheduler_state: str,
                progress: dict[str, Any], logs: list[dict[str, str]],
                domain: str = "vasp") -> dict:
    """Return a UI-ready diagnosis without executing model-generated code."""
    state = str(scheduler_state or "UNKNOWN").upper()
    combined = "\n".join(str(row.get("content") or "") for row in logs)
    findings: list[str] = []
    recommendations: list[str] = []
    matches = []
    for pattern, finding, recommendation in _SIGNATURES:
        if pattern.search(combined) and finding not in findings:
            findings.append(finding)
            recommendations.append(recommendation)
            for log in logs:
                line = next((line for line in str(log.get("content", "")).splitlines()
                             if pattern.search(line)), None)
                if line is not None:
                    matches.append({"finding": finding, "path": log.get("path", ""),
                                    "excerpt": line[:500]})
                    break
    for signature in progress.get("error_signatures") or []:
        label = f"VASP 错误特征：{signature}"
        if label not in findings:
            findings.append(label)
    scientific = progress.get("scientific_converged")
    terminal_failure = state in {
        "FAILED", "TIMEOUT", "CANCELLED", "CANCELED", "NODE_FAIL",
        "OUT_OF_MEMORY", "PREEMPTED", "BOOT_FAIL", "DEADLINE",
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
        summary = "作业尚未开始运行，当前没有可判定的计算结果。"
    elif state == "COMPLETED" and domain != "vasp":
        level, status = "warning", "调度完成，结果待核对"
        summary = "调度器报告完成；需检查输出内容、样本数量和计算判据后使用结果。"
    elif state == "COMPLETED" and scientific is not True:
        level, status = "warning", "调度完成但未确认收敛"
        summary = "调度器已结束作业，但日志不足以证明 VASP 科学收敛。"
        recommendations.append("检查 OUTCAR/OSZICAR 完整性和收敛条件后再使用结果。")
    else:
        level, status = "unknown", "信息不足"
        summary = "尚无足够的调度器或程序日志信息。"
    if not logs:
        recommendations.append("未获取 stdout/stderr 日志，请核对作业脚本中的输出路径。")
    return {
        "ok": True, "job_id": str(job_id), "level": level,
        "status": status, "summary": summary,
        "scheduler_state": state, "scientific_converged": scientific,
        "findings": findings, "recommendations": recommendations, "matches": matches,
        "log_files": [row.get("path", "") for row in logs],
        "engine": "builtin-rules-v1",
    }
