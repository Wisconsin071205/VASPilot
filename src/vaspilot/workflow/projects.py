"""Local VASP projects under ``~/.vaspilot/projects``.

A project is one directory holding the input files for a calculation
(INCAR/KPOINTS/POSCAR, an optional POTCAR copied byte-for-byte from a
user-supplied path, and an optional custom run.job.sh). Workflow plans take
``from_dir`` straight from a project directory.

Safety model:
  - project names are strict identifiers; resolved paths must stay inside
    the projects root
  - only whitelisted file names may be written
  - POTCAR content is never read, returned or logged — the file is copied
    verbatim and only its metadata (size/sha256/TITEL/ENMAX) is exposed
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.errors import ValidationError
from ..core.hashing import file_sha256, text_sha256

PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
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

POSCAR_PLACEHOLDER = """<替换为你的结构> bulk placeholder cell
1.0
 10.0000000000  0.0000000000  0.0000000000
  0.0000000000 10.0000000000  0.0000000000
  0.0000000000  0.0000000000 10.0000000000
Fe
1
Direct
 0.0000000000  0.0000000000  0.0000000000
"""

TEMPLATES: dict[str, dict[str, str]] = {
    "relax": {
        "description": "结构优化（ISIF=3, IBRION=2, NSW=60）",
        "INCAR": INCAR_RELAX,
        "KPOINTS": KPOINTS_AUTO,
        "POSCAR": POSCAR_PLACEHOLDER,
    },
    "scf": {
        "description": "静态单点计算（NSW=0，留 CHGCAR 供后续 DOS）",
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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProjectStore:
    def __init__(self, *, projects_dir: Path, index_path: Path,
                 audit: Any = None) -> None:
        self.root = Path(projects_dir)
        self.index_path = Path(index_path)
        self.audit = audit

    # -- index ------------------------------------------------------------------
    def _load_index(self) -> dict[str, dict[str, Any]]:
        try:
            with open(self.index_path, "r", encoding="utf-8-sig") as handle:
                import json
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {}

    def _save_index(self, index: dict[str, dict[str, Any]]) -> None:
        import json
        import os
        import tempfile
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="." + self.index_path.name + ".",
                                   dir=str(self.index_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(index, handle, ensure_ascii=False, indent=2,
                          sort_keys=True)
            os.replace(tmp, self.index_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _record(self, event: str, **fields: Any) -> None:
        if self.audit is not None:
            try:
                self.audit.record(event, **fields)
            except Exception:
                pass

    # -- paths --------------------------------------------------------------------
    def _project_dir(self, name: str) -> Path:
        if not PROJECT_NAME_RE.fullmatch(name or ""):
            raise ValidationError(
                f"project name {name!r} must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}")
        candidate = (self.root / name).resolve()
        root = self.root.resolve()
        if root != candidate and root not in candidate.parents:
            raise ValidationError("project path escapes the projects root")
        return candidate

    # -- create / list / delete -----------------------------------------------------
    def create(self, name: str, files: dict[str, str] | None = None,
               potcar_path: str | Path | None = None,
               potcar_remote: str = "") -> dict[str, Any]:
        directory = self._project_dir(name)
        if directory.exists():
            raise ValidationError(f"project {name!r} already exists")
        potcar_remote = str(potcar_remote or "").strip()
        if len(potcar_remote.encode("utf-8")) > 512:
            raise ValidationError("potcar_remote path exceeds 512 bytes")
        files = {str(k): str(v) for k, v in (files or {}).items()
                 if str(v).strip()}
        for fname in files:
            if fname not in FILE_WHITELIST:
                raise ValidationError(
                    f"file {fname!r} is not one of {FILE_WHITELIST}")
            if fname == "POTCAR":
                raise ValidationError(
                    "POTCAR cannot be written as text; provide potcar_path "
                    "so it is copied byte-for-byte instead")
        directory.mkdir(parents=True, exist_ok=False)
        written: list[dict[str, Any]] = []
        try:
            for fname in ("INCAR", "KPOINTS", "POSCAR"):
                content = files.get(fname, "")
                if content:
                    path = directory / fname
                    path.write_text(content, encoding="utf-8", newline="\n")
                    written.append({"name": fname, "size": path.stat().st_size})
            for fname, content in files.items():
                if fname in REQUIRED_INPUTS or fname == "POTCAR":
                    continue
                path = directory / fname
                path.write_text(content, encoding="utf-8", newline="\n")
                written.append({"name": fname, "size": path.stat().st_size})
            potcar: dict[str, Any] | None = None
            if potcar_path:
                source = Path(potcar_path).expanduser()
                if not source.is_file():
                    raise ValidationError(f"POTCAR source not found: {source}")
                shutil.copyfile(source, directory / "POTCAR")
                target = directory / "POTCAR"
                potcar = {"size": target.stat().st_size,
                          "sha256": file_sha256(target)}
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        index = self._load_index()
        index[name] = {"created_at": _now(), "pinned": False,
                       "potcar_remote": potcar_remote}
        self._save_index(index)
        self._record("project.create", outcome="ok", project=name,
                     files=[w["name"] for w in written],
                     potcar=bool(potcar))
        return {"name": name, "path": str(directory), "files": written,
                "potcar": potcar}

    def list(self) -> list[dict[str, Any]]:
        index = self._load_index()
        out: list[dict[str, Any]] = []
        if not self.root.is_dir():
            return out
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            meta = index.get(entry.name, {})
            present = {name: (entry / name).is_file()
                       for name in FILE_WHITELIST}
            missing = [name for name in REQUIRED_INPUTS + ("POTCAR",)
                       if not present.get(name)]
            out.append({
                "name": entry.name,
                "path": str(entry.resolve()),
                "pinned": bool(meta.get("pinned")),
                "created_at": meta.get("created_at", ""),
                "potcar_remote": meta.get("potcar_remote", ""),
                "mtime": datetime.fromtimestamp(
                    entry.stat().st_mtime, timezone.utc).isoformat(
                        timespec="seconds"),
                "files": present,
                "missing": missing,
                "complete": not [n for n in REQUIRED_INPUTS
                                 if not present.get(n)],
            })
        out.sort(key=lambda item: (not item["pinned"],
                                   item["name"].lower()))
        return out

    def pin(self, name: str, pinned: bool = True) -> dict[str, Any]:
        directory = self._project_dir(name)
        if not directory.is_dir():
            raise ValidationError(f"project {name!r} not found")
        index = self._load_index()
        meta = index.get(name, {"created_at": _now()})
        meta["pinned"] = bool(pinned)
        index[name] = meta
        self._save_index(index)
        return {"name": name, "pinned": meta["pinned"]}

    def set_potcar_remote(self, name: str, path: str) -> dict[str, Any]:
        """Record where the POTCAR lives on the HPC side (library path).

        Pure metadata: nothing is read or copied until a human-approved /
        audited remote command assembles it into the run directory.
        """
        directory = self._project_dir(name)
        if not directory.is_dir():
            raise ValidationError(f"project {name!r} not found")
        path = str(path or "").strip()
        if len(path.encode("utf-8")) > 512:
            raise ValidationError("path exceeds 512 bytes")
        index = self._load_index()
        meta = index.get(name, {"created_at": _now()})
        meta["potcar_remote"] = path
        index[name] = meta
        self._save_index(index)
        self._record("project.potcar_remote", outcome="ok",
                     project=name, remote_path=path)
        return {"name": name, "potcar_remote": path}

    def delete(self, name: str) -> dict[str, Any]:
        directory = self._project_dir(name)
        if not directory.is_dir():
            raise ValidationError(f"project {name!r} not found")
        trash = self.root / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        target = trash / f"{name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        shutil.move(str(directory), str(target))
        index = self._load_index()
        index.pop(name, None)
        self._save_index(index)
        self._record("project.delete", outcome="ok", project=name,
                     moved_to=str(target))
        return {"name": name, "moved_to": str(target)}

    # -- file access ------------------------------------------------------------------
    def read_file(self, name: str, fname: str) -> dict[str, Any]:
        if fname not in FILE_WHITELIST:
            raise ValidationError(
                f"file {fname!r} is not one of {FILE_WHITELIST}")
        path = self._project_dir(name) / fname
        if not path.is_file():
            raise ValidationError(f"{fname} not found in project {name!r}")
        if fname == "POTCAR":
            # metadata only: content never enters the model, UI or logs
            meta: dict[str, Any] = {"name": fname, "size": path.stat().st_size,
                                    "sha256": file_sha256(path)}
            from ..hpc.vasp import potcar_metadata
            parsed = potcar_metadata(
                path.read_text(encoding="utf-8", errors="replace"))
            if parsed:
                meta.update(parsed)
            return meta
        return {"name": fname, "size": path.stat().st_size,
                "content": path.read_text(encoding="utf-8", errors="replace")}

    def write_file(self, name: str, fname: str, content: str) -> dict[str, Any]:
        if fname not in FILE_WHITELIST:
            raise ValidationError(
                f"file {fname!r} is not one of {FILE_WHITELIST}")
        if fname == "POTCAR":
            raise ValidationError(
                "POTCAR cannot be written as text; supply a source path at "
                "project creation so it is copied byte-for-byte")
        directory = self._project_dir(name)
        if not directory.is_dir():
            raise ValidationError(f"project {name!r} not found")
        if len(content.encode("utf-8")) > 262144:
            raise ValidationError(f"{fname} exceeds the 256 KiB text cap")
        path = directory / fname
        path.write_text(str(content), encoding="utf-8", newline="\n")
        self._record("project.write", outcome="ok", project=name,
                     file=fname, sha256=text_sha256(content),
                     size=path.stat().st_size)
        return {"name": fname, "path": str(path),
                "size": path.stat().st_size, "sha256": text_sha256(content)}

    def copy_potcar(self, name: str, potcar_path: str | Path) -> dict[str, Any]:
        """Attach a POTCAR by byte-copy from a user-supplied local path."""
        directory = self._project_dir(name)
        if not directory.is_dir():
            raise ValidationError(f"project {name!r} not found")
        source = Path(potcar_path).expanduser()
        if not source.is_file():
            raise ValidationError(f"POTCAR source not found: {source}")
        shutil.copyfile(source, directory / "POTCAR")
        target = directory / "POTCAR"
        self._record("project.write", outcome="ok", project=name,
                     file="POTCAR", size=target.stat().st_size,
                     sha256=file_sha256(target))
        return {"name": "POTCAR", "size": target.stat().st_size,
                "sha256": file_sha256(target)}

    # -- validation -----------------------------------------------------------------
    def validate(self, name: str) -> dict[str, Any]:
        directory = self._project_dir(name)
        if not directory.is_dir():
            raise ValidationError(f"project {name!r} not found")
        from ..hpc.vasp import validate_inputs
        files: dict[str, Any] = {}
        for fname in FILE_WHITELIST:
            path = directory / fname
            if not path.is_file():
                continue
            if fname == "POTCAR":
                files[fname] = path.stat().st_size
            else:
                files[fname] = path.read_text(encoding="utf-8",
                                              errors="replace")
        result = validate_inputs(files, require_potcar=False)
        result["project"] = name
        return result
