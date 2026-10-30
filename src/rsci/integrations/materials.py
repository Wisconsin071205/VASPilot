"""Optional pymatgen validation at the local project boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def validate_vasp_directory(directory: str | Path) -> dict[str, Any]:
    """Parse local VASP inputs with pymatgen when the optional stack exists."""
    try:
        from pymatgen.io.vasp.inputs import Incar, Kpoints, Poscar
    except ImportError:
        return {"enabled": False, "backend": "builtin",
                "detail": "pymatgen 未安装，继续使用内置轻量校验"}

    root = Path(directory)
    parsed: dict[str, Any] = {}
    errors: list[str] = []
    readers = {
        "INCAR": lambda path: {"tags": len(Incar.from_file(path))},
        "KPOINTS": lambda path: {
            "style": str(Kpoints.from_file(path).style)},
        "POSCAR": lambda path: {
            "formula": Poscar.from_file(path).structure.composition.reduced_formula,
            "sites": len(Poscar.from_file(path).structure)},
    }
    for name, reader in readers.items():
        path = root / name
        if not path.is_file():
            continue
        try:
            parsed[name] = reader(path)
        except Exception as exc:
            errors.append(f"pymatgen 无法解析 {name}: {type(exc).__name__}: {exc}")
    return {"enabled": True, "backend": "pymatgen",
            "parsed": parsed, "errors": errors}
