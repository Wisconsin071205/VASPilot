"""VASP input validation and scientific progress parsing.

Scientific status is deliberately kept separate from scheduler state:

  scheduler COMPLETED  -> the queue released the slot; VASP may still have
                          failed, crashed, or not converged
  ionic_converged      -> OUTCAR reports "reached required accuracy" (relax)
                          or a static run finished with electronic SC met
  electronic_reached_nelm -> the last ionic step hit the NELM ceiling

POTCAR is handled metadata-only everywhere: TITEL, ENMAX, version, size and
SHA-256. The pseudopotential bytes never enter logs, tool results or model
context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

DEFAULT_NELM = 60

_REQUIRED_INPUTS = ("INCAR", "KPOINTS", "POSCAR", "POTCAR")
_TEXT_DENYLIST = {
    "POTCAR", "WAVECAR", "CHGCAR", "CHG", "LOCPOT", "PROCAR", "PARCHG",
    "AECCAR0", "AECCAR1", "AECCAR2", "ELFCAR",
}

# Known fatal VASP signatures (detection only; recovery is a separate,
# approval-gated step).
_FATAL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("ZBRENT: fatal", "zbrent_fatal"),
    ("VERY BAD NEWS", "very_bad_news"),
    ("Sub-Space-Matrix is not hermitian", "subspace_not_hermitian"),
    ("BRMIX: very serious problems", "brmix_serious"),
    ("Routine TETIRR", "tetirr_fatal"),
    ("Fatal error detecting kernel malloc", "malloc_fatal"),
    ("p4_error", "mpi_abort"),
    ("ERROR: subspace", "subspace_error"),
    ("edxslt failed to converge", "edxslt_failed"),
    ("HUGE ERROR in star", "star_error"),
)


@dataclass
class Incar:
    values: dict[str, str] = field(default_factory=dict)

    def get_int(self, key: str, default: int) -> int:
        raw = self.values.get(key.upper(), "")
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return default


def parse_incar(text: str) -> Incar:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].split("!", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().upper()
        value = value.split(";", 1)[0].strip()
        if key and value:
            values[key] = value
    return Incar(values)


def read_text_file(directory: dict[str, str], name: str) -> str:
    return directory.get(name, "")


def validate_inputs(files: dict[str, Any], *, require_potcar: bool = True) -> dict[str, Any]:
    """Deterministic VASP preflight on a directory listing / file map.

    ``files`` maps filenames to either file size (int) or text content (str).
    Returns ``{"ok": bool, "errors": [...], "warnings": [...], "incar": {...}}``.
    """
    errors: list[str] = []
    warnings: list[str] = []

    def has(name: str) -> bool:
        return name in files and (
            isinstance(files[name], int) and files[name] >= 0
            or isinstance(files[name], str))

    for name in _REQUIRED_INPUTS:
        if name == "POTCAR" and not require_potcar:
            continue
        if not has(name):
            errors.append(f"missing required input {name}")

    incar_data: dict[str, Any] = {}
    content = files.get("INCAR")
    if isinstance(content, str) and content.strip():
        incar = parse_incar(content)
        incar_data = incar.values
        nsw = incar.get_int("NSW", -1)
        if nsw == 0:
            warnings.append("NSW=0: single-point run; ionic convergence is not applicable")
        if incar.get_int("IBRION", -1) == 0 and nsw != 0:
            warnings.append("IBRION=0 with NSW!=0 performs ionic steps without relaxation logic")
        if incar.get_int("NELM", DEFAULT_NELM) > 200:
            warnings.append("NELM>200: electronic steps may stall without diagnosing")
        if incar.values.get("ALGO", "normal").lower() == "all":
            warnings.append("ALGO=All can be unstable for metallic systems")
    elif has("INCAR"):
        errors.append("INCAR present but empty")

    kpoints = files.get("KPOINTS")
    if isinstance(kpoints, str) and kpoints.strip():
        lines = [ln.strip() for ln in kpoints.splitlines() if ln.strip()]
        if len(lines) >= 4:
            try:
                mode = int(lines[1].split()[0])
                if mode == 0 and "gamma" not in lines[2].lower():
                    errors.append("KPOINTS mode 0 requires explicit mesh or Gamma line")
            except (ValueError, IndexError):
                errors.append("KPOINTS second line must be the mesh mode integer")

    poscar = files.get("POSCAR")
    if isinstance(poscar, str) and poscar.strip():
        lines = poscar.splitlines()
        if len(lines) >= 7:
            try:
                counts = [int(x) for x in lines[6].split()]
                if any(n <= 0 for n in counts):
                    errors.append("POSCAR species counts must be positive integers")
            except ValueError:
                errors.append("POSCAR line 7 must be integer species counts")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "incar": incar_data,
    }


@dataclass
class IonicStep:
    step: int
    energy_ev: float | None = None
    energy_zero_ev: float | None = None
    electronic_steps: int = 0


# An electronic row as VASP writes it: the algorithm label (DAV:, RMM:, CG :,
# ...) then the step number, E and dE. Fortran may print a D exponent.
_ELECTRONIC_ROW = re.compile(
    r"^(?:[A-Za-z]{1,4}\s*:)?\s*(\d+)\s+([-+0-9.eEdD]+)(?:\s+([-+0-9.eEdD]+))?")


def _fortran_float(text: str | None) -> float | None:
    if not text:
        return None
    try:
        return float(text.replace("D", "E").replace("d", "e"))
    except ValueError:
        return None


def parse_oszicar(text: str, *, nelm: int = DEFAULT_NELM) -> dict[str, Any]:
    """Parse OSZICAR into ionic steps + electronic-step bookkeeping."""
    steps: list[IonicStep] = []
    electronic_reached_nelm = False
    electronic_rows = 0
    f_re = re.compile(r"F=\s*([-+0-9.eEdD]+)")
    e0_re = re.compile(r"E0=\s*([-+0-9.eEdD]+)")
    last_row_was_ionic = True
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "TOTEN" in line or "E0=" in line or "F=" in line:
            # ionic step closing row
            step_m = re.match(r"^(\d+)", line)
            step = int(step_m.group(1)) if step_m else (steps[-1].step + 1 if steps else 1)
            f_match = f_re.search(line)
            e0_match = e0_re.search(line)
            current = IonicStep(
                step=step,
                energy_ev=float(f_match.group(1)) if f_match else None,
                energy_zero_ev=float(e0_match.group(1)) if e0_match else None,
                electronic_steps=electronic_rows if not last_row_was_ionic else 0,
            )
            steps.append(current)
            if 0 < nelm <= electronic_rows:
                electronic_reached_nelm = True
            electronic_rows = 0
            last_row_was_ionic = True
        elif _ELECTRONIC_ROW.match(line):
            electronic_rows += 1
            last_row_was_ionic = False

    return {
        "ionic_steps": len(steps),
        "steps": [
            {
                "step": s.step,
                "energy_ev": s.energy_ev,
                "energy_zero_ev": s.energy_zero_ev,
                "electronic_steps": s.electronic_steps,
            }
            for s in steps[-80:]
        ],
        "last_ionic": vars(steps[-1]) if steps else None,
        "electronic_reached_nelm": electronic_reached_nelm,
    }


def _max_force(outcar: str) -> float | None:
    """Largest atomic force (eV/Angstrom) in the last TOTAL-FORCE block."""
    start = outcar.rfind("TOTAL-FORCE (eV/Angst)")
    if start < 0:
        return None
    lines = outcar[start:].splitlines()[1:]
    largest: float | None = None
    dashes = 0
    for line in lines:
        if line.strip().startswith("---"):
            dashes += 1
            if dashes == 2:
                break
            continue
        parts = line.split()
        if dashes == 1 and len(parts) == 6:
            try:
                fx, fy, fz = (float(v) for v in parts[3:])
            except ValueError:
                continue
            size = (fx * fx + fy * fy + fz * fz) ** 0.5
            largest = size if largest is None else max(largest, size)
    return largest


def live_progress(files: dict[str, Any]) -> dict[str, Any]:
    """Where a (possibly running) job is, from INCAR + the tails of
    OSZICAR and OUTCAR: the step under way, its electronic iterations, the
    energy trend and the forces, next to the convergence verdict.

    Step numbers are read from the rows themselves, so a tail that starts
    mid-file still reports the true ionic step.
    """
    incar = parse_incar(files.get("INCAR") or "")
    oszicar = files.get("OSZICAR") or ""
    outcar = files.get("OUTCAR") or ""

    def number(key: str, default: float) -> float:
        value = _fortran_float(incar.values.get(key))
        return default if value is None else value

    ediff = number("EDIFF", 1e-4)
    energies: list[dict[str, Any]] = []
    rows, current_de, last_rows = 0, None, 0
    for raw in oszicar.splitlines():
        line = raw.strip()
        if "E0=" in line or "F=" in line:
            step = re.match(r"^(\d+)", line)
            e0 = re.search(r"E0=\s*([-+0-9.eEdD]+)", line)
            energies.append({
                "step": int(step.group(1)) if step else len(energies) + 1,
                "e0": _fortran_float(e0.group(1)) if e0 else None})
            last_rows, rows, current_de = rows, 0, None
            continue
        match = _ELECTRONIC_ROW.match(line)
        if match:
            rows += 1
            current_de = _fortran_float(match.group(3))

    known = [row["e0"] for row in energies if row["e0"] is not None]
    status = scientific_status(scheduler_state="UNKNOWN", files=files)
    status.pop("completed", None)
    return {
        **status,
        "ionic_step": energies[-1]["step"] if energies else 0,
        "nsw": incar.get_int("NSW", 0),
        "nelm": incar.get_int("NELM", DEFAULT_NELM),
        "ediff": ediff,
        # VASP's default EDIFFG is ten times EDIFF (an energy criterion)
        "ediffg": number("EDIFFG", ediff * 10),
        "current_electronic": rows,
        "current_de": current_de,
        "last_electronic_steps": last_rows,
        "last_e0": known[-1] if known else None,
        "delta_e": known[-1] - known[-2] if len(known) >= 2 else None,
        "energies": energies[-60:],
        "max_force": _max_force(outcar),
    }


def parse_outcar_tail(text: str) -> dict[str, Any]:
    """Extract convergence markers and fatal signatures from OUTCAR text."""
    lower = text.lower()
    ionic_converged = "reached required accuracy" in lower
    errors = []
    for pattern, code in _FATAL_PATTERNS:
        if pattern.lower() in lower:
            errors.append(code)
    nelml_marker = re.search(r"number of electronic steps\s+\(NELM\)\s*=\s*(\d+)", lower)
    if "the electronic self-consistency was not achieved" in lower:
        errors.append("electronic_selfconsistency_failed")
    return {
        "ionic_converged": ionic_converged,
        "nelm": int(nelml_marker.group(1)) if nelml_marker else None,
        "error_signatures": errors,
        "has_outcar": bool(text.strip()),
    }


def scientific_status(*, scheduler_state: str, files: dict[str, Any],
                      nelm_default: int = DEFAULT_NELM) -> dict[str, Any]:
    """Full scientific assessment of one calculation directory.

    ``files`` maps filename -> text content (or empty string when absent).
    """
    oszicar = files.get("OSZICAR") or ""
    outcar = files.get("OUTCAR") or ""
    incar_text = files.get("INCAR") or ""

    nelm = DEFAULT_NELM
    if incar_text.strip():
        nelm = parse_incar(incar_text).get_int("NELM", DEFAULT_NELM)
    nsw = parse_incar(incar_text).get_int("NSW", 0) if incar_text.strip() else 0

    osz = parse_oszicar(oszicar, nelm=nelm)
    out = parse_outcar_tail(outcar)
    produced = [name for name in
                ("CONTCAR", "OUTCAR", "OSZICAR", "DOSCAR", "EIGENVAL", "vasprun.xml")
                if (files.get(name) or "").strip()]

    electronic_ok = not osz["electronic_reached_nelm"] and \
        "electronic_selfconsistency_failed" not in out["error_signatures"]
    ionic_ok: bool
    if nsw == 0:
        # static run: scientific completion = electronic SC met in last step
        ionic_ok = osz["ionic_steps"] >= 1 and electronic_ok
    else:
        ionic_ok = out["ionic_converged"]

    scheduler_done = str(scheduler_state).upper() in {
        "COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "OUT_OF_MEMORY"}

    scientific_converged = bool(ionic_ok and electronic_ok and not out["error_signatures"])

    return {
        "scheduler_state": scheduler_state,
        "scheduler_done": scheduler_done,
        # Explicit separation required by spec: scheduler COMPLETED is NOT
        # scientific convergence.
        "scientific_converged": scientific_converged,
        "ionic_converged": ionic_ok,
        "electronic_converged": electronic_ok,
        "electronic_reached_nelm": osz["electronic_reached_nelm"],
        "ionic_steps": osz["ionic_steps"],
        "last_ionic": osz["last_ionic"],
        "error_signatures": out["error_signatures"],
        "produced_files": produced,
        "completed": scheduler_done and scientific_converged,
    }


def potcar_metadata(potcar_text: str) -> dict[str, Any] | None:
    """Extract only TITEL / ENMAX / size from POTCAR text. Bytes never leave."""
    titel = re.search(r"TITEL\s*=\s*([^\n]+)", potcar_text)
    if titel is None:
        titel = re.search(r"TITEL\s+([^=\n]+)", potcar_text)
    enmax = re.search(r"ENMAX\s*=\s*([-+0-9.eE]+)", potcar_text)
    lexch = re.search(r"LEXCH\s*=\s*(\w+)", potcar_text)
    if not titel:
        return None
    return {
        "titel": titel.group(1).strip(),
        "enmax_ev": float(enmax.group(1)) if enmax else None,
        "functional": lexch.group(1) if lexch else "",
        "size_bytes": len(potcar_text.encode("utf-8", errors="replace")),
    }


def denied_text_file(name: str) -> bool:
    """True when a file must never be read as text through tool results."""
    return name.upper() in _TEXT_DENYLIST
