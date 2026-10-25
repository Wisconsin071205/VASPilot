"""Domain-independent plan integrity, with a legacy VASP builder entry point."""
from __future__ import annotations

from typing import Any
from ..core.errors import ValidationError
from ..core.hashing import obj_sha256

# Compatibility constants for existing VASP callers.
DEFAULT_DOWNLOAD_FILES = ("INCAR", "OSZICAR", "OUTCAR", "CONTCAR")
DEFAULT_MONITOR_TIMEOUT_MIN = 2880
STEP_TYPES = ("mkdir", "upload", "validate", "submit", "monitor", "progress", "download", "parse")


def build_plan(**kwargs) -> dict[str, Any]:
    """Legacy VASP API; keep existing plan bytes and approval hashes stable."""
    from ..domains.vasp import build_plan as build_vasp_plan
    return build_vasp_plan(**kwargs)


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
