"""Confined, versioned storage shared by the research services."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from ..core.config import _atomic_write_json
from ..core.errors import ValidationError
from ..core.hashing import obj_sha256
from ..core.locking import reentrant_file_lock
from ..core.project_paths import confined_path
from ..workflow.research import project_store


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{16,64}", value):
        raise ValidationError("invalid research identifier")
    return value


def read(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("expected object")
        return data
    except (OSError, ValueError) as exc:
        raise ValidationError(f"记录缺失或损坏，已保留：{Path(path).name}") from exc


def save(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, data)


def project(config, name):
    store = project_store(config)
    with reentrant_file_lock(store.index_path.with_suffix(".lock")):
        meta = store._metadata(name)
        if meta["kind"] != "general":
            raise ValidationError("此功能需要通用科研项目")
        return store, store._project_dir(name), meta["project_id"]


def resolve_project(config, project_id):
    identifier(project_id)
    store = project_store(config)
    with reentrant_file_lock(store.index_path.with_suffix(".lock")):
        for name, meta in store._load_index().items():
            if meta.get("project_id") == project_id:
                project(config, name)
                return name
    raise ValidationError("原项目已删除或身份发生变化；任务不会绑定到同名新项目")


def location(config, name, relative):
    _, root, _ = project(config, name)
    return confined_path(root, relative)


def fingerprint(value):
    return obj_sha256(value)
