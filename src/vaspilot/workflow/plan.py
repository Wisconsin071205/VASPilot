"""Immutable workflow plans.

A plan is built deterministically from (local input directory, server,
remote directory, job parameters). The same inputs always produce the same
``plan_id`` and ``plan_hash``; any change to servers, files, scripts or steps
changes the hash and thereby invalidates every approval bound to it.

The plan document contains everything the approver must see:
files with SHA-256, the rendered job script, the ordered step DAG and a risk
summary. Plans never embed secrets or POTCAR content.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..core.errors import ValidationError
from ..core.hashing import file_sha256, obj_sha256, text_sha256
from ..hpc.jobscript import render_job_script

STEP_TYPES = ("mkdir", "upload", "validate", "submit", "monitor", "progress",
              "download", "parse")

_JOB_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_WALLTIME_RE = re.compile(r"^\d{1,5}(:\d{1,2}){0,2}$")
_PARTITION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
_VASP_EXEC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]{0,15}$")

# Files a finished relax/static run downloads for local analysis.
DEFAULT_DOWNLOAD_FILES = ("CONTCAR", "OSZICAR", "OUTCAR")
DEFAULT_MONITOR_TIMEOUT_MIN = 2880  # two days


def _positive_int(value: Any, *, label: str, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} must be an integer") from exc
    if not 1 <= number <= maximum:
        raise ValidationError(f"{label} must be in 1..{maximum}")
    return number


def build_plan(*, from_dir: str | Path, server: str, remote_dir: str,
               scheduler: str = "slurm",
               job_name: str = "vaspilot", partition: str = "",
               ntasks: int = 8, walltime: str = "24:00:00",
               vasp_executable: str = "vasp_std",
               download_files: tuple[str, ...] = DEFAULT_DOWNLOAD_FILES,
               monitor_timeout_min: int = DEFAULT_MONITOR_TIMEOUT_MIN,
               skip_potcar: bool = False) -> dict[str, Any]:
    """Construct one deterministic, hash-chained plan document."""
    if not _JOB_NAME_RE.fullmatch(job_name or ""):
        raise ValidationError(f"job name {job_name!r} is invalid")
    if partition and not _PARTITION_RE.fullmatch(partition):
        raise ValidationError(f"partition {partition!r} is invalid")
    if not _WALLTIME_RE.fullmatch(walltime or ""):
        raise ValidationError(f"walltime {walltime!r} is invalid")
    if not _VASP_EXEC_RE.fullmatch(vasp_executable or ""):
        raise ValidationError("vasp_executable must be a simple command name")
    ntasks = _positive_int(ntasks, label="ntasks", maximum=4096)
    monitor_timeout_min = _positive_int(
        monitor_timeout_min, label="monitor_timeout_min", maximum=20160)

    source = Path(from_dir).expanduser()
    if not source.is_dir():
        raise ValidationError(f"input directory not found: {source}")
    if not remote_dir.startswith("/") or ".." in remote_dir.split("/"):
        raise ValidationError("remote_dir must be an absolute non-traversing path")

    if scheduler not in ("slurm", "pbs"):
        raise ValidationError("scheduler must be 'slurm' or 'pbs'")

    required = ["INCAR", "KPOINTS", "POSCAR"] + ([] if skip_potcar else ["POTCAR"])
    files: list[dict[str, Any]] = []
    for name in required:
        local = source / name
        if not local.is_file():
            raise ValidationError(
                f"input directory is missing required file {name}")
        files.append({
            "name": name,
            "sha256": file_sha256(local),
            "size": local.stat().st_size,
            "local_path": str(local),
            "remote_path": f"{remote_dir.rstrip('/')}/{name}",
        })

    script = render_job_script(
        scheduler=scheduler, job_name=job_name,
        partition=partition, ntasks=ntasks, walltime=walltime,
        vasp_executable=vasp_executable)
    script_entry = {
        "name": "run.job.sh",
        "content_sha256": text_sha256(script),
        "remote_path": f"{remote_dir.rstrip('/')}/run.job.sh",
    }
    files.append({
        "name": "run.job.sh",
        "sha256": script_entry["content_sha256"],
        "size": len(script.encode("utf-8")),
        "local_path": None,
        "remote_path": script_entry["remote_path"],
        "content": script,
    })

    steps: list[dict[str, Any]] = [
        {"id": "mkdir", "type": "mkdir", "path": remote_dir},
    ]
    for entry in files:
        steps.append({"id": f"upload:{entry['name']}", "type": "upload",
                      "file": entry["name"],
                      "remote_path": entry["remote_path"],
                      "sha256": entry["sha256"]})
    steps.append({"id": "validate", "type": "validate", "path": remote_dir})
    steps.append({"id": "submit", "type": "submit", "path": remote_dir,
                  "script": "run.job.sh",
                  "script_sha256": script_entry["content_sha256"]})
    steps.append({"id": "monitor", "type": "monitor",
                  "job_output": "submit", "timeout_min": monitor_timeout_min})
    steps.append({"id": "progress", "type": "progress", "path": remote_dir})
    downloads = [name for name in download_files
                 if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", name)]
    steps.append({"id": "download", "type": "download", "path": remote_dir,
                  "files": downloads})
    steps.append({"id": "parse", "type": "parse",
                  "sources": ["INCAR", "OSZICAR", "OUTCAR"]})

    risk = {
        "level": "medium",
        "touches_scheduler": True,
        "creates_remote": [remote_dir],
        "overwrites": "nothing (uploads refuse to overwrite existing files)",
        "destructive": "nothing (no remove/purge steps in generated plans)",
        "walltime": walltime,
        "ntasks": ntasks,
        "notes": [
            "scheduler COMPLETED does not imply scientific convergence; the "
            "run records both separately",
        ],
    }

    body = {
        "schema": "vaspilot.plan/1",
        "server": server,
        "remote_dir": remote_dir.rstrip("/") if remote_dir != "/" else "/",
        "files": [{k: v for k, v in entry.items() if k != "content"}
                  for entry in files],
        "job_script": {"name": "run.job.sh",
                       "content_sha256": script_entry["content_sha256"],
                       "scheduler": scheduler,
                       "job_name": job_name, "partition": partition,
                       "ntasks": ntasks, "walltime": walltime,
                       "vasp_executable": vasp_executable},
        "steps": steps,
        "risk_summary": risk,
    }
    plan_hash = obj_sha256(body)
    return {
        **body,
        "plan_id": plan_hash[:16],
        "plan_hash": plan_hash,
        "job_script_content": script,
    }


def plan_files_hash(plan: dict[str, Any]) -> str:
    """Combined digest of every planned file hash (approval binding)."""
    return obj_sha256([entry["sha256"] for entry in plan.get("files", [])])


def verify_plan_integrity(plan: dict[str, Any]) -> None:
    """Recompute the plan hash; any drift invalidates the plan."""
    body = {k: v for k, v in plan.items()
            if k not in ("plan_id", "plan_hash", "job_script_content")}
    expected = obj_sha256(body)
    if expected != plan.get("plan_hash"):
        raise ValidationError(
            "plan_hash mismatch: the plan document was modified after building")
    if plan.get("plan_id") != expected[:16]:
        raise ValidationError("plan_id does not match the plan hash")
