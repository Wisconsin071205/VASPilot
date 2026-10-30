"""VASP domain: immutable scientific calculation plans.

A plan is built deterministically from (local input directory, server,
remote directory, job parameters). The same inputs always produce the same
``plan_id`` and ``plan_hash``; any change to servers, files, scripts or steps
changes the hash and thereby invalidates every approval bound to it.

The plan document contains everything the approver must see:
files with SHA-256, the rendered job script, the ordered single-stage steps and a risk
summary. Plans never embed secrets or POTCAR content.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..core.errors import ValidationError
from ..core.hashing import file_sha256, obj_sha256, text_sha256
from ..hpc.jobscript import render_job_script
from ..hpc.vasp import validate_inputs

STEP_TYPES = ("mkdir", "upload", "validate", "submit", "monitor", "progress",
              "download", "parse")

_JOB_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_WALLTIME_RE = re.compile(r"^\d{1,5}(:\d{1,2}){0,2}$")
_PARTITION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
_VASP_EXEC_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]{0,15}$")

# Files required to make a scientific verdict locally.  CONTCAR is useful for
# a relax but is optional for a static calculation.
DEFAULT_DOWNLOAD_FILES = ("INCAR", "OSZICAR", "OUTCAR", "CONTCAR")
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
               job_name: str = "rsci", partition: str = "",
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
    local_validation: dict[str, Any] = {}
    files: list[dict[str, Any]] = []
    for name in required:
        local = source / name
        if not local.is_file():
            raise ValidationError(
                f"input directory is missing required file {name}")
        if name == "POTCAR":
            local_validation[name] = local.stat().st_size
        else:
            try:
                local_validation[name] = local.read_text(
                    encoding="utf-8", errors="replace")
            except OSError as exc:
                raise ValidationError(f"cannot read input file {name}: {exc}") \
                    from exc
        files.append({
            "name": name,
            "sha256": file_sha256(local),
            "size": local.stat().st_size,
            "local_path": str(local),
            "remote_path": f"{remote_dir.rstrip('/')}/{name}",
        })
    preflight = validate_inputs(local_validation,
                                require_potcar=not skip_potcar)
    if not preflight["ok"]:
        raise ValidationError("local VASP input validation failed: " +
                              "; ".join(str(error)
                                        for error in preflight["errors"]))

    custom_script_path = source / "run.job.sh"
    if custom_script_path.is_file():
        # a project-authored job script is adopted verbatim; its hash is
        # frozen into the plan exactly like the rendered one
        try:
            script = custom_script_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValidationError(
                f"custom run.job.sh is not readable UTF-8 text: {exc}") from exc
        if not script.strip():
            raise ValidationError("custom run.job.sh is empty")
        script_source = "custom"
    else:
        script = render_job_script(
            scheduler=scheduler, job_name=job_name,
            partition=partition, ntasks=ntasks, walltime=walltime,
            vasp_executable=vasp_executable)
        script_source = "rendered"
    script_entry = {
        "name": "run.job.sh",
        "content_sha256": text_sha256(script),
        "remote_path": f"{remote_dir.rstrip('/')}/run.job.sh",
        "source": script_source,
    }
    files.append({
        "name": "run.job.sh",
        "sha256": script_entry["content_sha256"],
        "size": len(script.encode("utf-8")),
        "local_path": str(custom_script_path) if script_source == "custom"
                      else None,
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
    downloads: list[str] = []
    for name in download_files:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", name):
            raise ValidationError(f"download filename {name!r} is invalid")
        if name not in downloads:
            downloads.append(name)
    # The local scientific verdict must use the actual INCAR.  These files
    # cannot be omitted by a caller-customized output list.
    for required_output in ("INCAR", "OSZICAR", "OUTCAR"):
        if required_output not in downloads:
            downloads.append(required_output)
    optional_downloads = [name for name in downloads if name == "CONTCAR"]
    steps.append({"id": "download", "type": "download", "path": remote_dir,
                  "files": downloads, "optional_files": optional_downloads})
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
            "this is a single-stage VASP job submission; multi-stage recipes "
            "are not expanded or chained by the execution engine",
        ],
    }
    if skip_potcar:
        risk["level"] = "high"
        risk["notes"].append(
            "POTCAR is not uploaded by this plan; remote validation must find "
            "an already assembled POTCAR in the run directory")
    if script_source == "custom":
        risk["notes"].append(
            "job script comes verbatim from the input directory "
            "(project-authored run.job.sh)")

    body = {
        "schema": "rsci.plan/1",
        "server": server,
        "remote_dir": remote_dir.rstrip("/") if remote_dir != "/" else "/",
        "files": [{k: v for k, v in entry.items() if k != "content"}
                  for entry in files],
        "job_script": {"name": "run.job.sh",
                       "content_sha256": script_entry["content_sha256"],
                       "source": script_source,
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

FILE_WHITELIST = ("INCAR", "KPOINTS", "POSCAR", "POTCAR", "run.job.sh")
REQUIRED_INPUTS = ("INCAR", "KPOINTS", "POSCAR")

INCAR_RELAX = """SYSTEM = relax
ISTART = 0
ICHARG = 2
ENCUT  = 520
PREC   = Accurate
EDIFF  = 1E-5
EDIFFG = -0.02
ISMEAR = 0
SIGMA  = 0.05
IBRION = 2
ISIF   = 3
NSW    = 60
ISYM   = 0
NELM   = 200
LWAVE  = .FALSE.
LCHARG = .FALSE.
"""

INCAR_SCF = """SYSTEM = scf
ISTART = 0
ICHARG = 2
ENCUT  = 520
PREC   = Accurate
EDIFF  = 1E-5
ISMEAR = 0
SIGMA  = 0.05
IBRION = -1
NSW    = 0
ISYM   = 0
NELM   = 200
LWAVE  = .FALSE.
LCHARG = .TRUE.
"""

KPOINTS_AUTO = """Automatic mesh
0
Gamma
2 2 2
0 0 0
"""

# A fake Fe cell is dangerous: it looks runnable and can silently submit the
# wrong material.  Templates deliberately leave POSCAR empty so the project
# cannot become runnable until the user supplies a real structure.
POSCAR_PLACEHOLDER = ""

TEMPLATES: dict[str, dict[str, str]] = {
    "relax": {
        "description": "结构优化（ISIF=3, IBRION=2, NSW=60）",
        "INCAR": INCAR_RELAX,
        "KPOINTS": KPOINTS_AUTO,
        "POSCAR": POSCAR_PLACEHOLDER,
    },
    "scf": {
        "description": "静态单点计算（NSW=0；本次作业只提交这一阶段）",
        "INCAR": INCAR_SCF,
        "KPOINTS": KPOINTS_AUTO,
        "POSCAR": POSCAR_PLACEHOLDER,
    },
    "blank": {
        "description": "空白（全部自填）",
        "INCAR": "",
        "KPOINTS": "",
        "POSCAR": "",
    },
}


class VaspDomain:
    kind = "vasp"
    title = "VASP 材料计算"

    def validate_project(self, directory: Path, metadata: dict) -> dict:
        files = {}
        for name in FILE_WHITELIST:
            path = directory / name
            if path.is_file():
                files[name] = path.stat().st_size if name == "POTCAR" else path.read_text(encoding="utf-8", errors="replace")
        result = validate_inputs(files, require_potcar=False)
        from ..integrations.materials import validate_vasp_directory
        materials = validate_vasp_directory(directory)
        result["materials_backend"] = materials
        if materials.get("enabled") and materials.get("errors"):
            result.setdefault("errors", []).extend(materials["errors"])
            result["ok"] = False
        if "POTCAR" not in files:
            result.setdefault("warnings", []).append(
                "本地项目没有 POTCAR；提交前请补充本地 POTCAR，或在远端预置已组装 POTCAR 后使用 --skip-potcar")
        result["ready_for_single_stage"] = bool(result.get("ok") and "POTCAR" in files)
        result["potcar_remote"] = str(metadata.get("potcar_remote") or "")
        result["domain"] = self.kind
        return result

    def validate_remote(self, client, path: str, server: str) -> dict:
        return client.vasp_validate(path, server=server)

    def progress(self, client, path: str, server: str) -> dict:
        return client.vasp_progress(path, server=server)

    def parse(self, execution_state: str, files: dict) -> dict:
        from ..hpc.vasp import scientific_status
        return scientific_status(scheduler_state=execution_state, files=files)

    def conclusion(self, execution_state: str, analysis: dict) -> dict:
        scientific = bool(analysis.get("scientific_converged"))
        terminal_failures = {"FAILED", "TIMEOUT", "CANCELLED", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED"}
        return {"status": "completed" if execution_state == "COMPLETED" and scientific else
                "failed" if execution_state in terminal_failures else "needs_review",
                "scientific_converged": scientific}
