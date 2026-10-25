"""Portable, confined project paths and atomic file writes.

The project boundary does not pretend to sandbox arbitrary Python code.
These checks protect the file-management and result-collection APIs.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path, PurePosixPath

from .errors import ValidationError

GENERAL_DIRS = ("data", "scripts", "notes", "results", "references")
MAX_TEXT_BYTES = 256 * 1024
MAX_IMPORT_BYTES = 16 * 1024 * 1024
MAX_PROJECT_FILES = 2000
_DEVICES = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.I)
TEXT_SUFFIXES = {".csv", ".tsv", ".txt", ".json", ".jsonl", ".md", ".py",
                 ".r", ".sh", ".yaml", ".yml", ".toml", ".ipynb", ".svg", ".bib"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".pdf", ".xlsx", ".parquet", ".npy", ".npz"}


def relative_name(value: str) -> str:
    name = str(value)
    if not name or len(name) > 240 or "\\" in name or name.startswith("/"):
        raise ValidationError("project file must be a relative path using /")
    parts = name.split("/")
    if len(parts) > 12 or any(not p or p.startswith(".") or p.endswith((".", " "))
                             or _DEVICES.match(p) or re.search(r'[<>:"|?*\x00-\x1f]', p)
                             for p in parts):
        raise ValidationError("unsafe or non-portable project file path")
    if PurePosixPath(name).name.upper().startswith("POTCAR"):
        raise ValidationError("POTCAR is restricted to the VASP metadata/copy interface")
    return name


def general_name(value: str) -> str:
    name = relative_name(value)
    if name == "README.md":
        return name
    if "/" not in name or name.split("/", 1)[0] not in GENERAL_DIRS:
        raise ValidationError("use data/, scripts/, notes/, results/ or references/ inside the project")
    if PurePosixPath(name).suffix.lower() not in TEXT_SUFFIXES | BINARY_SUFFIXES:
        raise ValidationError("unsupported research file type")
    return name


def is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def confined_path(root: Path, name: str) -> Path:
    """Reject every symlink/junction, including links to a sibling project."""
    root = Path(root)
    if is_link(root):
        raise ValidationError("project root cannot be a symlink or junction")
    candidate = root
    for part in str(name).split("/"):
        if part in ("", ".", "..") or "\\" in part or ":" in part:
            raise ValidationError("invalid relative project path")
        candidate = candidate / part
        if is_link(candidate):
            raise ValidationError("project files cannot traverse symlinks or junctions")
    resolved = candidate.resolve()
    if root.resolve() not in resolved.parents:
        raise ValidationError("project file escapes its project root")
    return candidate


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".rsci-write-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
