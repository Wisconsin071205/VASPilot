"""VESTA 导出的 `.vasp`：切标注、体检、摘要。全是文本上的纯函数。

VESTA 导出的文件本身就是 POSCAR 格式，所以这里做的不是格式转换而是体检：
元素符号行必须在（VASPKIT task 103 靠它挑赝势），原子数必须和坐标块对得上，
用户写在下面的那段话必须干净地摘出来。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from ..core.errors import ValidationError

_SYMBOL_RE = re.compile(r"^[A-Z][a-z]?$")


@dataclass(frozen=True)
class Poscar:
    comment: str
    scale: float
    lattice: tuple[tuple[float, float, float], ...]
    symbols: tuple[str, ...]
    counts: tuple[int, ...]
    selective: bool
    coord_mode: str
    positions: tuple[str, ...]
    text: str

    @property
    def natoms(self) -> int:
        return sum(self.counts)


def _floats(line: str, *, label: str) -> tuple[float, float, float]:
    parts = line.split()
    if len(parts) < 3:
        raise ValidationError(f"{label} needs three numbers, got {line.strip()!r}")
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError as exc:
        raise ValidationError(f"{label} is not numeric: {line.strip()!r}") from exc


def _scan(lines: list[str]) -> dict[str, Any]:
    """Read the header and report where the coordinate block starts."""
    if len(lines) < 8:
        raise ValidationError("not a POSCAR: the file has fewer than 8 lines")
    try:
        scale = float(lines[1].split()[0])
    except (IndexError, ValueError) as exc:
        raise ValidationError(
            f"line 2 must be the scaling factor, got {lines[1].strip()!r}") from exc
    lattice = tuple(_floats(lines[i], label=f"the lattice vector on line {i + 1}")
                    for i in (2, 3, 4))

    tokens = lines[5].split()
    if not tokens or all(token.isdigit() for token in tokens):
        raise ValidationError(
            "this file has no element-symbol line (the old VASP 4 layout). "
            "VASPKIT picks pseudopotentials from that line, so export from "
            "VESTA in the VASP 5 format instead.")
    if not all(_SYMBOL_RE.fullmatch(token) for token in tokens):
        raise ValidationError(
            f"line 6 is not a list of element symbols: {lines[5].strip()!r}")
    symbols = tuple(tokens)

    count_tokens = lines[6].split()
    if not count_tokens or not all(token.isdigit() for token in count_tokens):
        raise ValidationError(
            f"line 7 must be the atom counts, got {lines[6].strip()!r}")
    counts = tuple(int(token) for token in count_tokens)
    if len(counts) != len(symbols):
        raise ValidationError(
            f"{len(symbols)} element symbols but {len(counts)} counts")
    if any(count <= 0 for count in counts):
        raise ValidationError("every atom count must be positive")

    index = 7
    selective = lines[index].strip()[:1].upper() == "S"
    if selective:
        index += 1
    if index >= len(lines):
        raise ValidationError("the coordinate-mode line is missing")
    marker = lines[index].strip()[:1].upper()
    if marker == "D":
        coord_mode = "Direct"
    elif marker in ("C", "K"):
        coord_mode = "Cartesian"
    else:
        raise ValidationError(f"unknown coordinate mode {lines[index].strip()!r}")

    return {"scale": scale, "lattice": lattice, "symbols": symbols,
            "counts": counts, "selective": selective,
            "coord_mode": coord_mode, "coords_start": index + 1}


def split_annotation(text: str) -> tuple[str, str]:
    """Cut a VESTA export into (POSCAR text, whatever was typed below it).

    POSCAR has no comment syntax -- as far as VASP is concerned anything
    after the coordinates is the velocity block -- so the only sound way to
    find the end of the structure is to count atoms.
    """
    lines = text.splitlines()
    scan = _scan(lines)
    start = scan["coords_start"]
    natoms = sum(scan["counts"])
    end = start + natoms
    if end > len(lines):
        raise ValidationError(
            f"the file declares {natoms} atoms but only "
            f"{max(0, len(lines) - start)} coordinate lines follow")
    return "\n".join(lines[:end]) + "\n", "\n".join(lines[end:]).strip()


def parse_poscar(text: str) -> Poscar:
    lines = text.splitlines()
    scan = _scan(lines)
    start = scan["coords_start"]
    natoms = sum(scan["counts"])
    positions = tuple(lines[start:start + natoms])
    if len(positions) != natoms:
        raise ValidationError(
            f"the file declares {natoms} atoms but only {len(positions)} "
            "coordinate lines follow")
    for offset, line in enumerate(positions):
        _floats(line, label=f"the coordinates of atom {offset + 1}")
    return Poscar(comment=lines[0].strip(), scale=scan["scale"],
                  lattice=scan["lattice"], symbols=scan["symbols"],
                  counts=scan["counts"], selective=scan["selective"],
                  coord_mode=scan["coord_mode"], positions=positions,
                  text="\n".join(lines[:start + natoms]) + "\n")


def _determinant(rows: tuple[tuple[float, float, float], ...]) -> float:
    (a, b, c), (d, e, f), (g, h, i) = rows
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def _norm(vector: tuple[float, float, float]) -> float:
    return math.sqrt(sum(component * component for component in vector))


def _angle(u: tuple[float, float, float], v: tuple[float, float, float]) -> float:
    dot = sum(a * b for a, b in zip(u, v))
    cosine = dot / (_norm(u) * _norm(v))
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def structure_summary(poscar: Poscar) -> dict[str, Any]:
    """The numbers a human needs to spot a bad export before anything runs."""
    raw_volume = abs(_determinant(poscar.lattice))
    if raw_volume <= 0.0:
        raise ValidationError("the three lattice vectors are coplanar")
    if poscar.scale < 0.0:
        # VASP reads a negative scaling factor as the target cell volume.
        volume = -poscar.scale
        factor = (volume / raw_volume) ** (1.0 / 3.0)
    else:
        factor = poscar.scale
        volume = raw_volume * factor ** 3
    vectors = tuple(tuple(component * factor for component in row)
                    for row in poscar.lattice)

    totals: dict[str, int] = {}
    for symbol, count in zip(poscar.symbols, poscar.counts):
        totals[symbol] = totals.get(symbol, 0) + count

    return {
        "formula": "".join(symbol if count == 1 else f"{symbol}{count}"
                           for symbol, count in totals.items()),
        "elements": tuple(totals),
        "counts": totals,
        "natoms": poscar.natoms,
        "abc": tuple(_norm(vector) for vector in vectors),
        "angles": (_angle(vectors[1], vectors[2]),
                   _angle(vectors[0], vectors[2]),
                   _angle(vectors[0], vectors[1])),
        "volume": volume,
        "coord_mode": poscar.coord_mode,
        "selective_dynamics": poscar.selective,
    }
