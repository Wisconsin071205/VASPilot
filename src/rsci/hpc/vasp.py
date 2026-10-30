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


def _finite_float(value: str) -> float | None:
    try:
        number = float(value.replace("D", "E").replace("d", "e"))
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _parse_poscar(text: str) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 8:
        return None, ["POSCAR is too short to contain a lattice and coordinates"]
    title = lines[0].lower()
    if any(marker in title for marker in ("placeholder", "replace", "替换", "粘贴")):
        errors.append("POSCAR still contains a template/placeholder structure")

    scale = _finite_float(lines[1].split()[0]) if lines[1].split() else None
    if scale is None or scale == 0:
        errors.append("POSCAR scale must be one finite non-zero number")
    lattice: list[list[float]] = []
    for row in lines[2:5]:
        values = row.split()
        if len(values) < 3 or any(_finite_float(value) is None
                                  for value in values[:3]):
            errors.append("POSCAR lattice vectors must contain 3 finite numbers")
            continue
        lattice.append([float(value.replace("D", "E").replace("d", "e"))
                        for value in values[:3]])
    if len(lattice) == 3:
        determinant = (
            lattice[0][0] * (lattice[1][1] * lattice[2][2] -
                             lattice[1][2] * lattice[2][1]) -
            lattice[0][1] * (lattice[1][0] * lattice[2][2] -
                             lattice[1][2] * lattice[2][0]) +
            lattice[0][2] * (lattice[1][0] * lattice[2][1] -
                             lattice[1][1] * lattice[2][0]))
        if abs(determinant) < 1e-12:
            errors.append("POSCAR lattice vectors are linearly dependent")

    cursor = 5
    first = lines[cursor].split()
    counts: list[int] = []
    species: list[str] = []
    if first and all(re.fullmatch(r"[+-]?\d+", token) for token in first):
        counts = [int(token) for token in first]
    else:
        species = first
        cursor += 1
        if cursor < len(lines):
            tokens = lines[cursor].split()
            if tokens and all(re.fullmatch(r"[+-]?\d+", token)
                              for token in tokens):
                counts = [int(token) for token in tokens]
    if not counts:
        errors.append("POSCAR must contain integer species counts")
        return None, errors
    if species and len(species) != len(counts):
        errors.append("POSCAR species and count columns have different lengths")
    if any(number <= 0 for number in counts):
        errors.append("POSCAR species counts must be positive integers")
    cursor += 1
    if cursor < len(lines) and lines[cursor].lower().startswith("s"):
        cursor += 1
    if cursor >= len(lines) or lines[cursor].lower()[:1] not in {"d", "c", "k"}:
        errors.append("POSCAR must specify Direct or Cartesian coordinates")
        return None, errors
    cursor += 1
    atom_count = sum(counts)
    if len(lines) < cursor + atom_count:
        errors.append(f"POSCAR is missing coordinate rows (expected {atom_count})")
    for row in lines[cursor:cursor + atom_count]:
        values = row.split()
        if len(values) < 3 or any(_finite_float(value) is None
                                  for value in values[:3]):
            errors.append("POSCAR coordinate rows must contain 3 finite numbers")
            break
    return ({"species": species, "counts": counts,
             "n_atoms": atom_count} if not errors else None), errors


def validate_poscar(text: str) -> dict[str, Any]:
    """Validate one POSCAR without requiring the other VASP input files."""
    structure, errors = _parse_poscar(str(text or ""))
    return {"ok": not errors, "errors": errors,
            "structure": structure or {}}


def _parse_kpoints(text: str) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 4:
        return None, ["KPOINTS must contain a title, mode, mesh and shift"]

    # A few existing projects omit the human-readable title.  Keep these
    # importable but report the issue; newly generated plans should still use
    # the standard five-line form.
    titleless = bool(lines[0].split() and
                     re.fullmatch(r"[+-]?\d+", lines[0].split()[0]) and
                     lines[1].lower() in {"gamma", "monkhorst-pack", "line-mode",
                                           "cartesian", "reciprocal"})
    offset = -1 if titleless else 0
    if titleless:
        warnings.append("KPOINTS title line is missing")
    mode_line = lines[1 + offset].split()
    try:
        mode = int(mode_line[0])
    except (IndexError, ValueError):
        return None, ["KPOINTS second line must be the mesh mode integer"]
    if mode < 0:
        errors.append("KPOINTS mesh mode must be non-negative")
    if mode == 0:
        style_index = 2 + offset
        mesh_index = 3 + offset
        if len(lines) <= mesh_index:
            errors.append("KPOINTS automatic mode is missing its mesh")
        else:
            style = lines[style_index].lower()
            if style in {"line-mode", "line"}:
                reciprocal_index = mesh_index
                if len(lines) <= reciprocal_index or \
                        lines[reciprocal_index].lower() not in {
                            "reciprocal", "cartesian"}:
                    errors.append("KPOINTS line mode requires Reciprocal or Cartesian")
                point_lines = lines[reciprocal_index + 1:]
                if len(point_lines) < 2 or any(
                        len(row.split()) < 3 or
                        any(_finite_float(token) is None
                            for token in row.split()[:3])
                        for row in point_lines):
                    errors.append("KPOINTS line mode needs finite point pairs")
            elif style not in {"gamma", "monkhorst-pack", "monkhorst", "automatic"}:
                errors.append("KPOINTS automatic mode requires Gamma or Monkhorst-Pack")
            else:
                mesh = lines[mesh_index].split()
                if len(mesh) < 3 or any(not re.fullmatch(r"\d+", token)
                                        or int(token) <= 0 for token in mesh[:3]):
                    errors.append("KPOINTS mesh must contain 3 positive integers")
                shift_index = mesh_index + 1
                if len(lines) > shift_index:
                    shift = lines[shift_index].split()
                    if len(shift) < 3 or any(_finite_float(token) is None
                                             for token in shift[:3]):
                        errors.append("KPOINTS shift must contain 3 finite numbers")
    elif mode > 0:
        style_index = 2 + offset
        style = lines[style_index].lower() if len(lines) > style_index else ""
        if style not in {"reciprocal", "cartesian", "line-mode", "line"}:
            errors.append("KPOINTS explicit mode requires Reciprocal or Cartesian")
        point_start = style_index + 1
        if len(lines) < point_start + mode:
            errors.append(f"KPOINTS is missing explicit points (expected {mode})")
        for row in lines[point_start:point_start + mode]:
            values = row.split()
            if len(values) < 3 or any(_finite_float(value) is None
                                      for value in values[:3]):
                errors.append("KPOINTS points must contain 3 finite numbers")
                break
    return ({"mode": mode, "titleless": titleless}, errors + warnings)


def validate_inputs(files: dict[str, Any], *, require_potcar: bool = True) -> dict[str, Any]:
    """Deterministic VASP preflight on a directory listing / file map.

    ``files`` maps filenames to either file size (int) or text content (str).
    Returns ``{"ok": bool, "errors": [...], "warnings": [...], "incar": {...}}``.
    """
    errors: list[str] = []
    warnings: list[str] = []

    def has(name: str) -> bool:
        value = files.get(name)
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return value > 0
        return isinstance(value, str) and bool(value.strip())

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
        assignments = [line for line in content.splitlines()
                       if "=" in line and line.split("=", 1)[1].strip()]
        if not assignments:
            errors.append("INCAR contains no non-empty TAG = VALUE assignments")
        nsw = incar.get_int("NSW", -1)
        if "NSW" in incar.values and nsw < 0:
            errors.append("INCAR NSW must be a non-negative integer")
        if "NELM" in incar.values and incar.get_int("NELM", -1) <= 0:
            errors.append("INCAR NELM must be a positive integer")
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
    kpoints_data: dict[str, Any] | None = None
    if isinstance(kpoints, str) and kpoints.strip():
        kpoints_data, kpoints_errors = _parse_kpoints(kpoints)
        for message in kpoints_errors:
            (warnings if "title line" in message else errors).append(message)

    poscar = files.get("POSCAR")
    structure: dict[str, Any] | None = None
    if isinstance(poscar, str) and poscar.strip():
        structure, poscar_errors = _parse_poscar(poscar)
        errors.extend(poscar_errors)

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "incar": incar_data,
        "kpoints": kpoints_data or {},
        "structure": structure or {},
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
    nelm_hits = 0
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
                nelm_hits += 1
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
        # only the step the run ended on decides the result; an early
        # relaxation step that ran out of NELM is routine and is counted
        "electronic_reached_nelm": bool(
            steps and 0 < nelm <= steps[-1].electronic_steps),
        "nelm_hits": nelm_hits,
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
                "e0": _fortran_float(e0.group(1)) if e0 else None,
                "electronic": rows})
            last_rows, rows, current_de = rows, 0, None
            continue
        match = _ELECTRONIC_ROW.match(line)
        if match:
            rows += 1
            current_de = _fortran_float(match.group(3))

    known = [row["e0"] for row in energies if row["e0"] is not None]
    status = scientific_status(scheduler_state="UNKNOWN", files=files)
    status.pop("completed", None)
    nsw = incar.get_int("NSW", 0)
    ionic_step = energies[-1]["step"] if energies else 0
    # VASP closes OUTCAR with its timing block; a run that was killed or
    # crashed never gets there
    elapsed = re.search(r"Elapsed time \(sec\):\s*([-+0-9.eEdD]+)", outcar)
    finished = "General timing and accounting" in outcar or bool(elapsed)
    return {
        **status,
        "ionic_step": ionic_step,
        "nsw": nsw,
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
        "finished_normally": finished,
        "elapsed_seconds": _fortran_float(elapsed.group(1)) if elapsed else None,
        # a relaxation that used up NSW without meeting EDIFFG
        "ionic_exhausted": bool(nsw and ionic_step >= nsw
                                and not status["ionic_converged"]),
        "total_electronic": sum(row["electronic"] for row in energies) + rows,
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
        "nelm_hits": osz["nelm_hits"],
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
