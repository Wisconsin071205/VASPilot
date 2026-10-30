"""Typed research projects with confined files, atomic writes and legacy migration."""
from __future__ import annotations

import json
import os
import secrets
import shutil
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any
import re

from ..core.config import _atomic_write_json
from ..core.errors import ValidationError
from ..core.hashing import file_sha256
from ..core.locking import reentrant_file_lock
from ..core.project_paths import (
    GENERAL_DIRS, MAX_IMPORT_BYTES, MAX_PROJECT_FILES, MAX_TEXT_BYTES,
    TEXT_SUFFIXES, atomic_bytes, confined_path, general_name, is_link,
)
from ..domains import domain_for
from ..domains.vasp import FILE_WHITELIST, REQUIRED_INPUTS, TEMPLATES  # compatibility

PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with reentrant_file_lock(self.index_path.with_suffix(".lock")):
            return method(self, *args, **kwargs)
    return call


class ProjectStore:
    def __init__(self, *, projects_dir: Path, index_path: Path, audit: Any = None):
        self.root, self.index_path = Path(projects_dir), Path(index_path)
        self.audit = audit

    def _record(self, event, **fields):
        if self.audit is not None:
            self.audit.record(event, **fields)

    def _load_index(self):
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise ValidationError("project index is damaged; preserved for manual recovery") from exc
        if not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()):
            raise ValidationError("project index has an unsupported shape; no records changed")
        return data

    def _save_index(self, index):
        _atomic_write_json(self.index_path, index)

    def _project_dir(self, name):
        if not PROJECT_NAME_RE.fullmatch(name or ""):
            raise ValidationError("project name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
        if name.endswith(".") or re.match(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", name, re.I):
            raise ValidationError("project name is reserved or not portable")
        return confined_path(self.root, name)

    def _metadata(self, name):
        directory = self._project_dir(name)
        if not directory.is_dir():
            raise ValidationError(f"project {name!r} not found")
        index = self._load_index()
        meta = dict(index.get(name) or {})
        if "kind" not in meta:
            # Every directory predating typed projects is VASP, even an empty
            # draft. This is an explicit, locked, additive metadata migration.
            meta.update(kind="vasp", schema="rsci.project/2", migrated_from="legacy-vasp")
            index[name] = meta
            self._save_index(index)
            self._record("project.migrate", outcome="ok", project=name, kind="vasp")
        domain_for(meta["kind"])
        if not meta.get("project_id"):
            meta["project_id"] = secrets.token_hex(16)
            index[name] = meta
            self._save_index(index)
        return meta

    @_locked
    def kind(self, name):
        return self._metadata(name)["kind"]

    def _file(self, name, fname, *, kind=None):
        kind = kind or self._metadata(name)["kind"]
        if kind == "general":
            fname = general_name(fname)
        elif fname not in FILE_WHITELIST:
            raise ValidationError(f"file {fname!r} is not one of {FILE_WHITELIST}")
        return confined_path(self._project_dir(name), fname)

    @staticmethod
    def _text(content, fname):
        data = str(content).encode("utf-8")
        if len(data) > MAX_TEXT_BYTES:
            raise ValidationError(f"{fname} exceeds the 256 KiB text cap")
        if b"\0" in data:
            raise ValidationError("text files cannot contain NUL bytes")
        return data

    @_locked
    def create(self, name, files=None, potcar_path=None, potcar_remote="", *, kind=None):
        directory = self._project_dir(name)
        if directory.exists():
            raise ValidationError(f"project {name!r} already exists")
        files = {str(k): str(v) for k, v in (files or {}).items()}
        # Existing integrations that explicitly supply VASP inputs keep their
        # behavior. A name-only creation is now a general research project.
        kind = kind or ("vasp" if potcar_path or potcar_remote or
                        any(k in FILE_WHITELIST for k in files) else "general")
        domain_for(kind)
        if kind != "vasp" and (potcar_path or potcar_remote):
            raise ValidationError("POTCAR requires a VASP project")
        potcar_remote = str(potcar_remote or "").strip()
        if len(potcar_remote.encode("utf-8")) > 512:
            raise ValidationError("potcar_remote path exceeds 512 bytes")
        prepared = {}
        for fname, content in files.items():
            self._file(name, fname, kind=kind)
            if fname == "POTCAR":
                raise ValidationError("POTCAR cannot be written as text; provide a source path")
            data = self._text(content, fname)
            if content.strip():
                prepared[fname] = data
        source = Path(potcar_path).expanduser() if potcar_path else None
        if source is not None and not source.is_file():
            raise ValidationError(f"POTCAR source not found: {source}")
        directory.mkdir(parents=True, exist_ok=False)
        written, potcar = [], None
        try:
            if kind == "general":
                for child in GENERAL_DIRS:
                    (directory / child).mkdir()
            for fname, data in prepared.items():
                path = self._file(name, fname, kind=kind)
                atomic_bytes(path, data)
                written.append({"name": fname, "size": len(data), "sha256": file_sha256(path)})
            if source is not None:
                target = self._file(name, "POTCAR", kind=kind)
                shutil.copyfile(source, target)
                potcar = {"size": target.stat().st_size, "sha256": file_sha256(target)}
            index = self._load_index()
            index[name] = {"kind": kind, "schema": "rsci.project/2", "created_at": _now(),
                           "pinned": False, "potcar_remote": potcar_remote,
                           "project_id": secrets.token_hex(16)}
            self._save_index(index)
        except BaseException:
            # Only this call's newly created, validated directory is removed.
            shutil.rmtree(directory, ignore_errors=True)
            raise
        self._record("project.create", outcome="ok", project=name, kind=kind,
                     files=[w["name"] for w in written])
        return {"name": name, "kind": kind, "path": str(directory), "files": written, "potcar": potcar}

    @_locked
    def list(self):
        if not self.root.is_dir():
            return []
        out = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            meta = self._metadata(entry.name)
            kind = meta["kind"]
            if kind == "vasp":
                present = {name: (self._file(entry.name, name).is_file() and
                                  self._file(entry.name, name).stat().st_size > 0)
                           for name in FILE_WHITELIST}
                missing = [n for n in REQUIRED_INPUTS + ("POTCAR",) if not present[n]]
            else:
                tree = self.files(entry.name)
                present = {row["name"]: True for row in tree}
                missing = []
            out.append({"name": entry.name, "kind": kind, "project_id": meta["project_id"], "path": str(entry.resolve()),
                        "pinned": bool(meta.get("pinned")), "created_at": meta.get("created_at", ""),
                        "potcar_remote": meta.get("potcar_remote", ""),
                        "mtime": datetime.fromtimestamp(entry.stat().st_mtime, timezone.utc).isoformat(),
                        "files": present, "missing": missing, "complete": not missing,
                        "ready_with_remote_potcar": kind == "vasp" and
                        all(present.get(n) for n in REQUIRED_INPUTS) and
                        not present.get("POTCAR") and bool(meta.get("potcar_remote"))})
        return sorted(out, key=lambda row: (not row["pinned"], row["name"].lower()))

    @_locked
    def files(self, name):
        directory = self._project_dir(name)
        kind = self._metadata(name)["kind"]
        rows = []
        candidates = [directory / n for n in FILE_WHITELIST] if kind == "vasp" else directory.rglob("*")
        for path in candidates:
            if is_link(path):
                raise ValidationError(f"symlink/junction is not allowed: {path.name}")
            if not path.is_file():
                continue
            relative = path.relative_to(directory).as_posix()
            if kind == "general":
                # Files made externally are visible as metadata but not given
                # broader read/write permissions than the managed interface.
                try:
                    self._file(name, relative)
                except ValidationError:
                    continue
            rows.append({"name": relative, "size": path.stat().st_size,
                         "sha256": file_sha256(path),
                         "text": (kind == "vasp" and relative != "POTCAR") or
                         (kind == "general" and path.suffix.lower() in TEXT_SUFFIXES)})
            if len(rows) > MAX_PROJECT_FILES:
                raise ValidationError("project exceeds the 2000-file listing limit")
        return sorted(rows, key=lambda row: row["name"])

    @_locked
    def pin(self, name, pinned=True):
        meta = self._metadata(name)
        index = self._load_index()
        meta["pinned"] = bool(pinned)
        index[name] = meta
        self._save_index(index)
        return {"name": name, "pinned": bool(pinned)}

    @_locked
    def update(self, name, *, new_name=None, files=None, potcar_path=None,
               potcar_remote=None, remove_potcar=False):
        source = self._project_dir(name)
        meta = self._metadata(name)
        target_name = str(new_name or name).strip()
        target = self._project_dir(target_name)
        if target_name != name and target.exists():
            raise ValidationError(f"project {target_name!r} already exists")
        files = {str(k): str(v) for k, v in (files or {}).items()}
        for fname, content in files.items():
            self._file(name, fname)
            if fname == "POTCAR":
                raise ValidationError("POTCAR cannot be written as text")
            self._text(content, fname)
        if meta["kind"] != "vasp" and (potcar_path or potcar_remote or remove_potcar):
            raise ValidationError("POTCAR settings require a VASP project")
        source_potcar = Path(potcar_path).expanduser() if potcar_path else None
        if source_potcar is not None and not source_potcar.is_file():
            raise ValidationError(f"POTCAR source not found: {source_potcar}")
        if potcar_remote is not None and len(str(potcar_remote).encode("utf-8")) > 512:
            raise ValidationError("potcar_remote path exceeds 512 bytes")
        if target_name != name:
            self._check_remote_collections(meta["project_id"])
            source.rename(target)
            index = self._load_index()
            index.pop(name, None)
            index[target_name] = meta
            self._save_index(index)
        written = [self.write_file(target_name, k, v) for k, v in files.items()]
        removed = False
        if remove_potcar and source_potcar is None:
            existing = self._file(target_name, "POTCAR")
            if existing.is_file():
                existing.unlink()
                removed = True
        potcar = self.copy_potcar(target_name, source_potcar) if source_potcar else None
        if potcar_remote is not None and meta["kind"] == "vasp":
            self.set_potcar_remote(target_name, potcar_remote)
        self._record("project.update", outcome="ok", project=target_name, renamed_from=name)
        return {"name": target_name, "kind": meta["kind"], "previous_name": name,
                "path": str(target), "files": written, "potcar": potcar, "potcar_removed": removed}

    @_locked
    def set_potcar_remote(self, name, path):
        meta = self._metadata(name)
        if meta["kind"] != "vasp":
            raise ValidationError("POTCAR requires a VASP project")
        value = str(path or "").strip()
        if len(value.encode("utf-8")) > 512:
            raise ValidationError("path exceeds 512 bytes")
        index = self._load_index()
        meta["potcar_remote"] = value
        index[name] = meta
        self._save_index(index)
        self._record("project.potcar_remote", outcome="ok", project=name, remote_path=value)
        return {"name": name, "potcar_remote": value}

    @_locked
    def delete(self, name):
        directory = self._project_dir(name)
        meta = self._metadata(name)
        self._check_remote_collections(meta["project_id"])
        trash = confined_path(self.root, ".trash")
        trash.mkdir(parents=True, exist_ok=True)
        target = trash / f"{name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"
        shutil.move(str(directory), str(target))
        index = self._load_index()
        index.pop(name, None)
        self._save_index(index)
        self._record("project.delete", outcome="ok", project=name, moved_to=str(target))
        return {"name": name, "moved_to": str(target)}

    def _check_remote_collections(self, project_id):
        for path in (self.index_path.parent / "remote-collections").glob("*.json"):
            state = json.loads(path.read_text(encoding="utf-8"))
            if state.get("project_id") == project_id and state.get("status") not in {"completed", "cancelled"}:
                raise ValidationError("项目仍有结果回收任务，请完成或停止回收后再重命名、删除。")

    @_locked
    def read_file(self, name, fname):
        path = self._file(name, fname)
        if not path.is_file():
            raise ValidationError(f"{fname} not found in project {name!r}")
        result = {"name": fname, "path": str(path.resolve()),
                  "size": path.stat().st_size, "sha256": file_sha256(path)}
        if fname == "POTCAR":
            from ..hpc.vasp import potcar_metadata
            result.update(potcar_metadata(path.read_text(encoding="utf-8", errors="replace")) or {})
        elif (self._metadata(name)["kind"] == "general" and path.suffix.lower() not in TEXT_SUFFIXES):
            result["binary"] = True
        elif path.stat().st_size > MAX_TEXT_BYTES:
            result.update(too_large=True, note="文件超过文本预览上限，可下载完整文件")
        else:
            result["content"] = path.read_text(encoding="utf-8-sig", errors="replace")
        return result

    @_locked
    def write_file(self, name, fname, content, *, expected_sha256=None):
        path = self._file(name, fname)
        if fname == "POTCAR":
            raise ValidationError("POTCAR cannot be written as text; supply a source path")
        if self._metadata(name)["kind"] == "general" and path.suffix.lower() not in TEXT_SUFFIXES:
            raise ValidationError("use file import for binary research files")
        if expected_sha256 == "missing" and path.exists():
            raise ValidationError("file already exists; open it before editing")
        if expected_sha256 not in (None, "missing") and (not path.is_file() or file_sha256(path) != expected_sha256):
            raise ValidationError("file changed since it was opened; reload before saving")
        data = self._text(content, fname)
        atomic_bytes(path, data)
        digest = file_sha256(path)
        self._record("project.write", outcome="ok", project=name, file=fname, sha256=digest, size=len(data))
        return {"name": fname, "path": str(path), "size": len(data), "sha256": digest}

    @_locked
    def import_file(self, name, fname, data: bytes, *, expected_sha256=None):
        if self._metadata(name)["kind"] != "general":
            raise ValidationError("research import requires a general project")
        path = self._file(name, fname)
        if not isinstance(data, bytes) or len(data) > MAX_IMPORT_BYTES:
            raise ValidationError("import exceeds the 16 MiB file limit")
        if path.exists() and (not expected_sha256 or not path.is_file() or file_sha256(path) != expected_sha256):
            raise ValidationError("file already exists; choose a new name or confirm its current hash")
        if path.suffix.lower() in TEXT_SUFFIXES:
            try:
                data.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValidationError("text imports must be UTF-8; convert the file first") from exc
            if b"\0" in data:
                raise ValidationError("text imports cannot contain NUL bytes")
        atomic_bytes(path, data)
        digest = file_sha256(path)
        self._record("project.import", outcome="ok", project=name, file=fname, size=len(data), sha256=digest)
        return {"name": fname, "size": len(data), "sha256": digest}

    @_locked
    def copy_potcar(self, name, potcar_path):
        if self._metadata(name)["kind"] != "vasp":
            raise ValidationError("POTCAR requires a VASP project")
        source = Path(potcar_path).expanduser()
        if not source.is_file():
            raise ValidationError(f"POTCAR source not found: {source}")
        target = self._file(name, "POTCAR")
        # Copy to a private sibling first, never follow an existing target link.
        temporary = target.with_name(".potcar-" + secrets.token_hex(8))
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        result = {"name": "POTCAR", "size": target.stat().st_size, "sha256": file_sha256(target)}
        self._record("project.write", outcome="ok", project=name, **result)
        return result

    @_locked
    def validate(self, name):
        metadata = self._metadata(name)
        # Traverse the managed inventory first so validators never follow links.
        self.files(name)
        result = domain_for(metadata["kind"]).validate_project(self._project_dir(name), metadata)
        result["project"] = name
        return result
