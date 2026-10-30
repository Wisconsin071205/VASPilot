"""配方：模型唯一被允许产出的结构化东西。

下游全是确定性代码，所以这里就是半懂的意图必须被挡下来的地方。一条都不
联网，校验不过就抛错，绝不带着一份猜出来的意图往服务器走。
"""

from __future__ import annotations

import re
from typing import Any

from ..core.errors import ValidationError

STAGES = ("relax", "static", "band", "dos")
# 后者依赖前者：能带和态密度都要读自洽算出的电荷密度。
STAGE_REQUIRES = {"static": "relax", "band": "static", "dos": "static"}
FUNCTIONALS = {"pbe": "PBE", "pbesol": "PBEsol", "lda": "LDA",
               "scan": "SCAN", "hse06": "HSE06"}
DEFAULT_KSPACING = {"relax": 0.04, "static": 0.03, "band": 0.03, "dos": 0.02}
KSPACING_RANGE = (0.01, 0.5)

_INCAR_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,15}$")
_SERVER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_WALLTIME_RE = re.compile(r"^\d{1,5}(:\d{1,2}){0,2}$")


def _int_in(value: Any, *, label: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} must be an integer") from exc
    if not low <= number <= high:
        raise ValidationError(f"{label} must be in {low}..{high}")
    return number


def _float_in(value: Any, *, label: str, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} must be a number") from exc
    if not low <= number <= high:
        raise ValidationError(f"{label} must be in {low}..{high}")
    return number


def expand_stages(names: list[str], *,
                  structure_relaxed: bool = False) -> tuple[str, ...]:
    """Add the stages the requested ones depend on, in canonical order."""
    requested = tuple(dict.fromkeys(names))
    wanted = set(requested)
    changed = True
    while changed:
        changed = False
        for stage in tuple(wanted):
            needed = STAGE_REQUIRES.get(stage)
            if needed and needed not in wanted:
                wanted.add(needed)
                changed = True
    # "already relaxed" removes the relaxation we inferred, never one that
    # was asked for outright.
    if structure_relaxed and "relax" not in requested:
        wanted.discard("relax")
    return tuple(stage for stage in STAGES if stage in wanted)


def _validate_structure(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("structure must be an object")
    file_name = str(raw.get("file", "")).strip()
    digest = str(raw.get("sha256", "")).strip().lower()
    if not file_name:
        raise ValidationError("structure.file is required")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValidationError("structure.sha256 must be a SHA-256 hex digest")
    return {"file": file_name, "formula": str(raw.get("formula", "")).strip(),
            "sha256": digest}


def validate_recipe(raw: dict[str, Any], *,
                    elements: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("the recipe must be a JSON object")

    structure = _validate_structure(raw.get("structure"))

    functional = str(raw.get("functional", "PBE")).strip()
    canonical = FUNCTIONALS.get(functional.lower())
    if canonical is None:
        raise ValidationError(
            f"unknown functional {functional!r}; supported: "
            + ", ".join(sorted(set(FUNCTIONALS.values()))))

    names = raw.get("stages") or []
    if not isinstance(names, list) or not names:
        raise ValidationError("the recipe must ask for at least one stage")
    unknown = [str(name) for name in names if name not in STAGES]
    if unknown:
        raise ValidationError(
            f"unknown stage(s) {', '.join(map(repr, unknown))}; supported: "
            + ", ".join(STAGES))
    stages = expand_stages(list(names),
                           structure_relaxed=bool(raw.get("structure_relaxed")))
    if not stages:
        raise ValidationError("no stages are left once dependencies resolve")

    kspacing = {stage: DEFAULT_KSPACING[stage] for stage in stages}
    given = raw.get("kspacing") or {}
    if not isinstance(given, dict):
        raise ValidationError("kspacing must map a stage to a spacing")
    low, high = KSPACING_RANGE
    for stage, value in given.items():
        if stage not in STAGES:
            raise ValidationError(f"kspacing names unknown stage {stage!r}")
        try:
            spacing = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"kspacing[{stage}] is not a number") from exc
        if not low <= spacing <= high:
            raise ValidationError(
                f"kspacing[{stage}] = {spacing} is outside {low}..{high}")
        if stage in kspacing:
            kspacing[stage] = spacing

    hubbard: dict[str, dict[str, float]] = {}
    given_u = raw.get("hubbard_u") or {}
    if not isinstance(given_u, dict):
        raise ValidationError("hubbard_u must map an element to its parameters")
    for symbol, params in given_u.items():
        if symbol not in elements:
            raise ValidationError(
                f"Hubbard U names {symbol!r}, which is not in the structure "
                f"({', '.join(elements)})")
        if not isinstance(params, dict):
            raise ValidationError(f"hubbard_u[{symbol}] must be an object")
        hubbard[symbol] = {
            "L": _int_in(params.get("L", 2), label=f"hubbard_u[{symbol}].L",
                         low=-1, high=3),
            "U": _float_in(params.get("U", 0.0), label=f"hubbard_u[{symbol}].U",
                           low=0.0, high=10.0),
            "J": _float_in(params.get("J", 0.0), label=f"hubbard_u[{symbol}].J",
                           low=0.0, high=10.0),
        }

    overrides: dict[str, dict[str, str]] = {}
    given_overrides = raw.get("overrides") or {}
    if not isinstance(given_overrides, dict):
        raise ValidationError("overrides must map a stage to INCAR parameters")
    for stage, values in given_overrides.items():
        if stage not in stages:
            raise ValidationError(
                f"overrides names stage {stage!r}, which this recipe "
                "does not run")
        if not isinstance(values, dict):
            raise ValidationError(f"overrides[{stage}] must be an object")
        cleaned: dict[str, str] = {}
        for key, value in values.items():
            token = str(key).strip().upper()
            if not _INCAR_KEY_RE.fullmatch(token):
                raise ValidationError(f"{key!r} is not an INCAR parameter name")
            cleaned[token] = str(value).strip()
        overrides[stage] = cleaned

    resources = raw.get("resources") or {}
    if not isinstance(resources, dict):
        raise ValidationError("resources must be an object")
    server = str(resources.get("server", "")).strip()
    if not _SERVER_RE.fullmatch(server):
        raise ValidationError("resources.server must be a registered server name")
    walltime = str(resources.get("walltime", "24:00:00")).strip()
    if not _WALLTIME_RE.fullmatch(walltime):
        raise ValidationError(f"resources.walltime {walltime!r} is not HH:MM:SS")
    partition = str(resources.get("partition", "")).strip()

    return {
        "structure": structure,
        "functional": canonical,
        "stages": stages,
        "kspacing": kspacing,
        "spin": bool(raw.get("spin", False)),
        "hubbard_u": hubbard,
        "overrides": overrides,
        "resources": {
            "server": server,
            "partition": partition,
            "ntasks": _int_in(resources.get("ntasks", 8),
                              label="resources.ntasks", low=1, high=4096),
            "walltime": walltime,
        },
    }
