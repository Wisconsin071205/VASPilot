"""Slurm/PBS command builders and output parsers.

The builders construct the exact argument vectors the Vlab gateway executes;
nothing model-supplied ever reaches them except identifiers that already
passed :mod:`vaspilot.core.validation` (numeric job ids, safe script names).
"""

from __future__ import annotations

import shlex
from typing import Any

from ..core.errors import SchedulerError
from ..core.validation import valid_filename, valid_job_id

# Normalized scheduler states. Scheduler states say NOTHING about scientific
# convergence; they only describe queue lifecycle.
PENDING_STATES = {"PENDING", "Q", "H", "S", "W", "E", "CONFIGURING"}
RUNNING_STATES = {"RUNNING", "R", "COMPLETING", "E"}
DONE_STATES = {"COMPLETED", "C", "F", "DONE"}
FAILED_STATES = {"FAILED", "TIMEOUT", "CANCELLED", "NODE_FAIL", "OUT_OF_MEMORY",
                 "PREEMPTED", "BOOT_FAIL", "DEADLINE"}


def _normalize(raw: str) -> str:
    code = raw.strip().upper()
    for known in FAILED_STATES:
        if code.startswith(known[:4]):
            return known
    if code.startswith("R"):
        return "RUNNING"
    if code.startswith("PD") or code.startswith("P") or code == "Q":
        return "PENDING"
    if code.startswith("C") or code == "F":
        return "COMPLETED"
    if code.startswith("CG"):
        return "COMPLETING"
    return code or "UNKNOWN"


def build_status(job_id: str, *, scheduler: str) -> list[str]:
    job_id = valid_job_id(job_id)
    if scheduler == "slurm":
        return ["squeue", "-h", "-j", job_id, "-o", "%i|%T|%M|%L|%P|%N"]
    return ["qstat", "-f", job_id]

def build_query(job_id: str, *, scheduler: str) -> list[str]:
    """History query: works whether the job is still queued or finished."""
    job_id = valid_job_id(job_id)
    if scheduler == "slurm":
        return ["sacct", "-j", job_id, "-n", "-o",
                "JobID,State,Elapsed,AllocCPUS,ExitCode", "-X"]
    # PBS Pro: -x includes finished jobs; stock PBS ignores it harmlessly.
    return ["qstat", "-x", "-f", job_id]


def build_submit(directory: str, script: str, *, scheduler: str) -> list[str]:
    name = valid_filename(script)
    if ".." in directory or "\x00" in directory:
        raise SchedulerError("submit directory failed validation")
    if scheduler == "slurm":
        return ["sbatch", "--parsable", f"{directory}/{name}"]
    return ["qsub", "-o", "/dev/null", "-e", "/dev/null", f"{directory}/{name}"]


def build_cancel(job_id: str, *, scheduler: str) -> list[str]:
    job_id = valid_job_id(job_id)
    return ["scancel", job_id] if scheduler == "slurm" else ["qdel", job_id]


def parse_squeue(raw: str) -> list[dict[str, Any]]:
    """Parse ``squeue -h -j ID -o '%i|%T|%M|%L|%P|%N'`` output."""
    jobs = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = [f.strip() for f in line.split("|")]
        if len(fields) < 2:
            continue
        jobs.append({
            "job_id": fields[0].split(".")[0],
            "state": _normalize(fields[1]),
            "elapsed": fields[2] if len(fields) > 2 else "",
            "time_limit": fields[3] if len(fields) > 3 else "",
            "partition": fields[4] if len(fields) > 4 else "",
            "nodes": fields[5] if len(fields) > 5 else "",
        })
    return jobs


def parse_sacct(raw: str) -> list[dict[str, Any]]:
    """Parse ``sacct -j ID -n -o JobID,State,Elapsed,AllocCPUS,ExitCode -X``."""
    jobs = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = [f.strip() for f in line.split("|")]
        if len(fields) < 2 or not fields[0]:
            continue
        exit_code = fields[4] if len(fields) > 4 else ""
        jobs.append({
            "job_id": fields[0].split(".")[0],
            "state": _normalize(fields[1]),
            "elapsed": fields[2] if len(fields) > 2 else "",
            "cpus": fields[3] if len(fields) > 3 else "",
            "exit_code": exit_code,
        })
    return jobs


def parse_qstat(raw: str) -> list[dict[str, Any]]:
    """Parse PBS ``qstat -f`` / ``qstat`` output into normalized rows."""
    jobs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Tabular form: "12345.server user queue 0 R run"
        head = stripped.split()
        if head[0][:1].isdigit() and len(head) >= 4 and len(head[0].split(".")) == 2:
            jobs.append({"job_id": head[0].split(".")[0], "state": _normalize(head[3])})
            continue
        # Full form: "Job id: 12345.server" or "Job Id: ..."
        low = stripped.lower()
        if low.startswith("job id") and ":" in stripped:
            job_id = stripped.split(":", 1)[1].strip().split()[0]
            current = {"job_id": job_id.split(".")[0], "state": "UNKNOWN"}
            jobs.append(current)
            continue
        if current is not None and " = " in stripped:
            key, value = stripped.split(" = ", 1)
            key = key.strip().lower()
            if key == "job_state":
                current["state"] = _normalize(value.strip())
            elif key == "exec_host":
                current["nodes"] = value.strip()[:120]
    return jobs


def merge_job_state(rows: list[dict[str, Any]]) -> str:
    """Collapse normalized rows into one lifecycle state."""
    if not rows:
        return "COMPLETED"
    states = [r.get("state", "UNKNOWN") for r in rows]
    for state in states:
        if state in RUNNING_STATES:
            return "RUNNING"
    for state in states:
        if state in PENDING_STATES:
            return "PENDING"
    for state in states:
        if state in FAILED_STATES:
            return state
    return states[0]
