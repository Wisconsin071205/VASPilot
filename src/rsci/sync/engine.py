"""Bidirectional WebDAV synchronization for portable RSCI data.

What is synchronized
--------------------
* ``settings.json`` — non-secret settings (the WebDAV password, provider API
  keys, SSH credentials and provider probe cache stay local)
* ``servers.json`` — the connection catalog (targets/paths, no passwords)
* ``projects.json`` — project index metadata
* ``chat/*.json`` — conversation logs
* ``skills/**`` — user-authored agent skills
* ``jobs/*.json`` — local job ledger snapshots

What is intentionally never synchronized: ``approval.key``, ``approvals.json``,
``server_credentials``, ``provider_keys``, audit logs, workflow run state and
anything outside the whitelist above.  A malicious WebDAV server therefore
cannot overwrite the local credential vault through this feature.

Conflict policy
---------------
The last successful remote token (ETag) and local content hash are stored in
``~/.rsci/webdav/state.json``.  When only one side changed, that side wins.
When both changed, JSON stores are merged deterministically; the raw losing
copies are written under ``~/.rsci/webdav/conflicts`` so no data is silently
dropped.  Other files use the newer modification time and also keep a backup.
Deletions are propagated for chat/skill files only; configuration is restored
from the other side instead of being deleted by a transient remote hiccup.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.errors import ConfigError, ValidationError
from ..core.locking import try_file_lock
from .webdav import WebDAVClient, WebDAVError, normalize_url, validate_remote_dir

SYNC_VERSION = 1
ALLOWED_EXACT = {"settings.json", "servers.json", "projects.json"}
CHAT_RE = re.compile(r"^chat/[A-Za-z0-9][A-Za-z0-9_-]{2,63}\.json$")
JOB_RE = re.compile(r"^jobs/[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.json$")
SKILL_RE = re.compile(r"^skills/(?:[^/\\]+/)*[^/\\]+$")
RESERVED_SETTINGS_KEYS = {"webdav", "server_credentials", "provider_keys",
                          "provider_probes"}
# The remote copy must not be able to replace a local machine-specific path.
LOCAL_VLAB_KEYS = {"identity_file", "workspace_gateway_path"}

_LOCAL_GLOBS = ("chat/*.json", "jobs/*.json", "skills/**/*")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _portable_settings(settings: dict) -> bytes:
    copy = {k: v for k, v in settings.items() if k not in RESERVED_SETTINGS_KEYS}
    return json.dumps(copy, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


def _parse_json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return None


def _json_ts(value: Any) -> float:
    try:
        text = str(value or "")
        if not text:
            return 0.0
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def is_allowed_rel(rel: str) -> bool:
    """Only these remote paths may be written into the local config home."""
    rel = str(rel or "").replace("\\", "/").strip("/")
    if rel in ALLOWED_EXACT:
        return True
    if CHAT_RE.fullmatch(rel) or JOB_RE.fullmatch(rel):
        return True
    if SKILL_RE.fullmatch(rel) and ".." not in rel.split("/"):
        return True
    return False


def _deletion_propagates(rel: str) -> bool:
    return rel.startswith("chat/") or rel.startswith("skills/")


class SyncEngine:
    def __init__(self, config, *, client_factory=None) -> None:
        self.config = config
        self._client_factory = client_factory

    # -- connection ----------------------------------------------------------
    def _client(self) -> WebDAVClient:
        cfg = self.config.webdav()
        if not cfg.get("url"):
            raise ValidationError("请先在「配置 → WebDAV 同步」填写 WebDAV 地址")
        if not cfg.get("username"):
            raise ValidationError("请先在「配置 → WebDAV 同步」填写用户名")
        password = self.config.webdav_password()
        if not password:
            raise ValidationError("尚未保存 WebDAV 密码；请填写后保存，"
                                  "或设置 RSCI_WEBDAV_PASSWORD 环境变量")
        if self._client_factory is not None:
            return self._client_factory(cfg["url"], cfg["username"], password)
        return WebDAVClient(cfg["url"], cfg["username"], password)

    def _profile(self, cfg: dict | None = None) -> str:
        cfg = cfg or self.config.webdav()
        raw = "|".join([
            normalize_url(cfg.get("url", "")),
            str(cfg.get("username", "")),
            validate_remote_dir(cfg.get("remote_dir", "")),
        ])
        return _sha(raw.encode("utf-8"))[:24]

    def test(self, *, url: str | None = None, username: str | None = None,
             password: str | None = None, remote_dir: str | None = None) -> dict:
        """Validate an endpoint (saved values, or an unsaved form draft)."""
        cfg = self.config.webdav()
        use_url = normalize_url(url) if url else normalize_url(cfg.get("url", ""))
        use_user = str(username if username is not None else cfg.get("username", ""))
        use_password = str(password or "") or self.config.webdav_password()
        if not use_url:
            raise ValidationError("请先填写 WebDAV 地址")
        if not use_user:
            raise ValidationError("请先填写 WebDAV 用户名")
        if not use_password:
            raise ValidationError("请先填写 WebDAV 密码")
        root = validate_remote_dir(remote_dir if remote_dir is not None
                                   else cfg.get("remote_dir", ""))
        client = (self._client_factory(use_url, use_user, use_password)
                  if self._client_factory is not None
                  else WebDAVClient(use_url, use_user, use_password))
        entry = client.stat(root)
        parent = None
        top_level: list[str] = []
        if entry is None and "/" in root:
            parent = root.rsplit("/", 1)[0]
        if entry is None:
            # Seafile exposes one collection per library at the DAV root and
            # usually refuses MKCOL there.  Listing the libraries lets the UI
            # suggest "webdav/RSCI-sync" instead of failing only on sync.
            try:
                top_level = [item.rel for item in client.list_dir("")
                             if item.is_dir][:30]
            except WebDAVError:
                top_level = []
        suggestion = ""
        first_segment = root.split("/", 1)[0]
        if entry is None and top_level and first_segment not in top_level:
            favourite = next((name for name in top_level
                              if name.lower() in ("webdav", "rsci", "personal")),
                             top_level[0])
            suggestion = f"{favourite}/{root}"
        return {
            "reachable": True,
            "url": client.base_url,
            "username": use_user,
            "remote_dir": root,
            "remote_dir_exists": entry is not None,
            "will_create": entry is None,
            "is_collection": bool(entry.is_dir) if entry else None,
            "parent": parent,
            "top_level": top_level,
            "suggested_remote_dir": suggestion,
            "tested_at": _now(),
        }

    # -- state ---------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            with open(self.config.webdav_state_path, "r", encoding="utf-8-sig") as handle:
                raw = json.load(handle)
        except (FileNotFoundError, OSError, ValueError):
            raw = {}
        profiles = raw.get("profiles") if isinstance(raw, dict) else None
        profile = self._profile()
        data = profiles.get(profile) if isinstance(profiles, dict) else None
        if not isinstance(data, dict):
            data = {}
        data.setdefault("files", {})
        data.setdefault("last_sync_at", "")
        return data

    def _save_state(self, state: dict) -> None:
        path = self.config.webdav_state_path
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                raw = json.load(handle)
        except (FileNotFoundError, OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        profiles = raw.get("profiles")
        if not isinstance(profiles, dict):
            profiles = {}
        profiles[self._profile()] = state
        raw["version"] = SYNC_VERSION
        raw["profiles"] = profiles
        _atomic_bytes(path, json.dumps(raw, ensure_ascii=False, indent=2,
                                       sort_keys=True).encode("utf-8"))

    # -- local inventory -----------------------------------------------------
    def _local_files(self) -> dict[str, bytes]:
        root = self.config.home
        files: dict[str, bytes] = {}
        settings_path = self.config.settings_path
        if settings_path.is_file():
            try:
                settings = self.config.load_settings()
                files["settings.json"] = _portable_settings(settings)
            except Exception:
                pass
        for name in ("servers.json", "projects.json"):
            path = root / name
            if path.is_file():
                try:
                    files[name] = path.read_bytes()
                except OSError:
                    pass
        for pattern in _LOCAL_GLOBS:
            for path in sorted(root.glob(pattern)):
                if not path.is_file() or path.name.startswith("."):
                    continue
                rel = path.relative_to(root).as_posix()
                if not is_allowed_rel(rel):
                    continue
                try:
                    if path.is_symlink():
                        continue
                    path.resolve().relative_to(root.resolve())
                    files[rel] = path.read_bytes()
                except (OSError, ValueError):
                    continue
        return files

    def _local_path(self, rel: str) -> Path:
        path = self.config.home / rel
        # is_allowed_rel already rejects traversal; this is a second guard
        # against odd names and symlinked parents.
        if ".." in Path(rel).parts:
            raise ValidationError("同步路径非法")
        try:
            path.parent.resolve().relative_to(self.config.home.resolve())
        except ValueError as exc:
            raise ValidationError("同步路径越过本机配置目录") from exc
        if path.is_symlink():
            raise ValidationError(f"同步目标 {rel} 是符号链接，已拒绝覆盖")
        return path

    # -- remote inventory ----------------------------------------------------
    def _remote_files(self, client: WebDAVClient, remote_dir: str) -> dict[str, Any]:
        base = remote_dir.strip("/")
        entries = client.list_tree(base)
        out = {}
        for rel, entry in entries.items():
            if entry.is_dir:
                continue
            if rel == base:
                continue
            child = rel[len(base) + 1:] if rel.startswith(base + "/") else rel
            if is_allowed_rel(child):
                entry.rel = child
                out[child] = entry
        return out

    # -- conflict backups ----------------------------------------------------
    def _backup(self, rel: str, data: bytes, side: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        safe = rel.replace("/", "__").replace("\\", "__")
        path = self.config.webdav_conflicts_dir / f"{stamp}-{safe}.{side}"
        _atomic_bytes(path, data)
        return str(path)

    # -- conflict merges -----------------------------------------------------
    def _merge_chat(self, local: dict, remote: dict) -> dict:
        newer = remote if _json_ts(remote.get("updated_at")) >= _json_ts(
            local.get("updated_at")) else local
        older = local if newer is remote else remote
        merged = dict(older)
        merged.update(newer)
        merged["created_at"] = min(
            [x for x in (local.get("created_at"), remote.get("created_at")) if x] or [""])
        merged["updated_at"] = max(
            [x for x in (local.get("updated_at"), remote.get("updated_at")) if x] or [""])

        def rows(document: dict) -> list[dict]:
            return list(document.get("archived_messages") or []) + list(
                document.get("messages") or [])

        combined: dict[tuple, dict] = {}
        order: list[tuple] = []
        for row in rows(local) + rows(remote):
            if not isinstance(row, dict):
                continue
            key = (
                row.get("role", ""),
                row.get("request_id") or "",
                row.get("at") or "",
                hashlib.sha256(str(row.get("content") or "").encode("utf-8")).hexdigest()
                if not row.get("request_id") else "",
            )
            current = combined.get(key)
            if current is None:
                combined[key] = dict(row)
                order.append(key)
            else:
                # Same request/role: keep the richer final answer.
                if len(str(row.get("content") or "")) > len(
                        str(current.get("content") or "")):
                    current.update(row)
                if str(row.get("status") or "") == "completed":
                    current["status"] = "completed"
        all_rows = [combined[k] for k in order]
        # Same-second user/assistant pairs must keep the user first; Python's
        # stable sort then preserves the original order for equal keys.
        all_rows.sort(key=lambda row: (str(row.get("at") or ""),
                                       0 if row.get("role") == "user" else 1))
        merged["messages"] = all_rows
        merged["archived_messages"] = []
        summary = max((str(local.get("context_summary") or ""),
                       str(remote.get("context_summary") or "")), key=len)
        merged["context_summary"] = summary
        plans = []
        for document in (local, remote):
            for plan in document.get("workflow_plans") or []:
                if plan not in plans:
                    plans.append(plan)
        merged["workflow_plans"] = plans
        anchors = {}
        for document in (local, remote):
            if isinstance(document.get("workflow_plan_anchors"), dict):
                anchors.update(document["workflow_plan_anchors"])
        merged["workflow_plan_anchors"] = anchors
        return merged

    def _merge_settings(self, local: dict, remote: dict, *, prefer_remote: bool,
                        authoritative: bool = False) -> dict:
        if authoritative:
            # A one-sided remote change is authoritative for non-secret keys,
            # so deleting a provider/search setting on one device propagates.
            # Local secret blobs (WebDAV password, API keys, SSH credentials)
            # are reserved and must survive the replacement.
            merged = {k: v for k, v in local.items()
                      if k in RESERVED_SETTINGS_KEYS}
            merged.update({k: v for k, v in remote.items()
                           if k not in RESERVED_SETTINGS_KEYS})
            local_vlab = local.get("vlab")
            if isinstance(merged.get("vlab"), dict) and isinstance(local_vlab, dict):
                merged["vlab"] = {
                    **merged["vlab"],
                    **{k: v for k, v in local_vlab.items()
                       if k in LOCAL_VLAB_KEYS},
                }
            return merged
        merged = dict(local)
        for key, value in remote.items():
            if key in RESERVED_SETTINGS_KEYS:
                continue
            if key == "providers" and isinstance(value, list) and isinstance(
                    merged.get("providers"), list):
                by_id = {}
                for item in merged["providers"] + value:
                    if not isinstance(item, dict):
                        continue
                    pid = str(item.get("id") or "")
                    if not pid:
                        continue
                    if pid not in by_id or prefer_remote:
                        by_id[pid] = item
                merged["providers"] = list(by_id.values())
            elif key == "vlab" and isinstance(value, dict) and isinstance(
                    merged.get("vlab"), dict):
                current = dict(value)
                current.update({k: v for k, v in merged["vlab"].items()
                                if k in LOCAL_VLAB_KEYS})
                if not prefer_remote:
                    current.update({k: v for k, v in merged["vlab"].items()
                                    if k not in LOCAL_VLAB_KEYS})
                merged["vlab"] = current
            elif key == "potcar_libraries" and isinstance(value, dict) and isinstance(
                    merged.get(key), dict):
                current = dict(merged[key])
                if prefer_remote:
                    current.update(value)
                else:
                    current = {**value, **current}
                merged[key] = current
            elif key not in merged or prefer_remote:
                merged[key] = value
        return merged

    @staticmethod
    def _merge_named_dict(local: dict, remote: dict, *, prefer_remote: bool) -> dict:
        merged = dict(local)
        for name, value in remote.items():
            if name not in merged or prefer_remote:
                merged[name] = value
        return merged

    def _merge_servers(self, local_data: bytes, remote_data: bytes,
                       *, prefer_remote: bool) -> bytes:
        local = _parse_json(local_data)
        remote = _parse_json(remote_data)
        if not isinstance(local, dict) or not isinstance(remote, dict):
            return remote_data if prefer_remote else local_data
        merged = dict(local)
        local_servers = {s.get("name"): s for s in local.get("servers", [])
                         if isinstance(s, dict) and s.get("name")}
        remote_servers = {s.get("name"): s for s in remote.get("servers", [])
                          if isinstance(s, dict) and s.get("name")}
        for name, item in remote_servers.items():
            if name not in local_servers or prefer_remote:
                local_servers[name] = item
        merged["servers"] = list(local_servers.values())
        if not merged.get("default") or (prefer_remote and remote.get("default")):
            merged["default"] = remote.get("default", merged.get("default", ""))
        return json.dumps(merged, ensure_ascii=False, indent=2,
                          sort_keys=True).encode("utf-8")

    def _merge_projects(self, local_data: bytes, remote_data: bytes,
                        *, prefer_remote: bool) -> bytes:
        local = _parse_json(local_data)
        remote = _parse_json(remote_data)
        if not isinstance(local, dict) or not isinstance(remote, dict):
            return remote_data if prefer_remote else local_data
        merged = self._merge_named_dict(local, remote,
                                        prefer_remote=prefer_remote)
        return json.dumps(merged, ensure_ascii=False, indent=2,
                          sort_keys=True).encode("utf-8")

    def _merge_jobs(self, local_data: bytes, remote_data: bytes,
                    *, prefer_remote: bool) -> bytes:
        local = _parse_json(local_data)
        remote = _parse_json(remote_data)
        if not isinstance(local, dict) or not isinstance(remote, dict):
            return remote_data if prefer_remote else local_data
        merged = self._merge_named_dict(local, remote,
                                        prefer_remote=prefer_remote)
        return json.dumps(merged, ensure_ascii=False, indent=2,
                          sort_keys=True).encode("utf-8")

    def _resolve_conflict(self, rel: str, local_data: bytes, remote_data: bytes,
                          *, prefer_remote: bool) -> tuple[bytes, dict]:
        """Return (chosen/merged bytes, conflict metadata)."""
        backups = [self._backup(rel, local_data, "local"),
                   self._backup(rel, remote_data, "remote")]
        local_json = _parse_json(local_data)
        remote_json = _parse_json(remote_data)
        merged: bytes | None = None
        kind = "newer"
        if rel == "settings.json" and isinstance(local_json, dict) and isinstance(
                remote_json, dict):
            merged = json.dumps(
                self._merge_settings(local_json, remote_json,
                                     prefer_remote=prefer_remote),
                ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
            kind = "merged-settings"
        elif CHAT_RE.fullmatch(rel) and isinstance(local_json, dict) and isinstance(
                remote_json, dict) and local_json.get("session_id") == remote_json.get(
                "session_id"):
            merged = json.dumps(self._merge_chat(local_json, remote_json),
                                ensure_ascii=False, indent=2).encode("utf-8")
            kind = "merged-chat"
        elif rel == "servers.json" and isinstance(local_json, dict) and isinstance(
                remote_json, dict):
            merged = self._merge_servers(local_data, remote_data,
                                         prefer_remote=prefer_remote)
            kind = "merged-servers"
        elif rel == "projects.json" and isinstance(local_json, dict) and isinstance(
                remote_json, dict):
            merged = self._merge_projects(local_data, remote_data,
                                          prefer_remote=prefer_remote)
            kind = "merged-projects"
        elif JOB_RE.fullmatch(rel) and isinstance(local_json, dict) and isinstance(
                remote_json, dict):
            merged = self._merge_jobs(local_data, remote_data,
                                      prefer_remote=prefer_remote)
            kind = "merged-jobs"
        if merged is None:
            merged = remote_data if prefer_remote else local_data
        return merged, {"path": rel, "kind": kind,
                        "winner": "remote" if prefer_remote else "local",
                        "backups": backups}

    # -- apply remote bytes locally -----------------------------------------
    def _apply_remote(self, rel: str, data: bytes, *, prefer_remote: bool,
                      local_data: bytes | None,
                      authoritative: bool = False) -> bytes:
        """Write a remote file locally and return the canonical synced bytes."""
        from contextlib import ExitStack

        if not is_allowed_rel(rel):
            raise ValidationError(f"拒绝同步未授权的路径：{rel}")
        with ExitStack() as stack:
            if CHAT_RE.fullmatch(rel):
                # Never overwrite a session while the chat runtime holds its
                # execution lock.  The next sync will retry the same file.
                if not stack.enter_context(try_file_lock(
                        self._local_path(rel).with_suffix(".turn.lock"))):
                    raise ValidationError(
                        "该对话正在生成回答，本次未覆盖；请等待回答结束后再同步")
            if rel == "settings.json":
                remote = _parse_json(data)
                if not isinstance(remote, dict):
                    raise ValidationError("远端 settings.json 格式无效")
                local = self.config.load_settings()
                merged = self._merge_settings(
                    local, remote, prefer_remote=prefer_remote,
                    authoritative=authoritative)
                self.config.save_settings(merged)
                return _portable_settings(merged)
            if local_data is not None and (
                    rel in ("servers.json", "projects.json") or
                    JOB_RE.fullmatch(rel)):
                if authoritative:
                    pass
                elif rel == "servers.json":
                    data = self._merge_servers(local_data, data,
                                               prefer_remote=prefer_remote)
                elif rel == "projects.json":
                    data = self._merge_projects(local_data, data,
                                                prefer_remote=prefer_remote)
                else:
                    data = self._merge_jobs(local_data, data,
                                            prefer_remote=prefer_remote)
            path = self._local_path(rel)
            _atomic_bytes(path, data)
            return path.read_bytes()

    # -- one-file state machine ---------------------------------------------
    def _sync_one(self, rel: str, *, client: WebDAVClient, remote_dir: str,
                  local_data: bytes | None, remote_entry, state: dict,
                  result: dict, ensured_dirs: set[str] | None = None) -> None:
        ensured_dirs = ensured_dirs if ensured_dirs is not None else set()
        entry_state = state["files"].get(rel)
        if not isinstance(entry_state, dict):
            entry_state = {}
        local_hash = _sha(local_data) if local_data is not None else ""
        remote_token = remote_entry.token if remote_entry is not None else ""
        prev_local = str(entry_state.get("local_hash") or "")
        prev_remote = str(entry_state.get("remote_token") or "")
        prev_remote_present = bool(entry_state.get("remote_present"))
        local_present = local_data is not None
        remote_present = remote_entry is not None

        local_changed = (local_hash != prev_local) if local_present else bool(prev_local)
        remote_changed = (remote_token != prev_remote) if remote_present \
            else prev_remote_present
        has_previous = bool(entry_state)

        remote_path = f"{remote_dir}/{rel}"

        def remember(local_bytes: bytes | None, remote_stat) -> None:
            state["files"][rel] = {
                "local_hash": _sha(local_bytes) if local_bytes is not None else "",
                "remote_token": remote_stat.token if remote_stat is not None else "",
                "remote_present": remote_stat is not None,
                "synchronized_at": _now(),
                "size": len(local_bytes) if local_bytes is not None else 0,
            }

        def ensure_parent() -> None:
            parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
            remote_parent = (f"{remote_dir}/{parent}".strip("/")
                             if parent else remote_dir)
            if remote_parent not in ensured_dirs:
                client.ensure_dir(remote_parent)
                ensured_dirs.add(remote_parent)

        def upload(data: bytes) -> None:
            nonlocal local_data
            ensure_parent()
            client.put(remote_path, data)
            stat = client.stat(remote_path)
            remember(data, stat)
            result["uploaded"].append(rel)
            result["bytes_up"] += len(data)
            local_data = data

        def download(remote_data: bytes, *, prefer_remote: bool) -> None:
            nonlocal local_data
            canonical = self._apply_remote(
                rel, remote_data, prefer_remote=prefer_remote,
                local_data=local_data, authoritative=True)
            local_data = canonical
            # Merged configuration (for example local-only server entries)
            # must be published back, otherwise the two sides stay different
            # while the state file claims they are in sync.
            if canonical != remote_data:
                ensure_parent()
                client.put(remote_path, canonical)
                result["uploaded"].append(rel)
                result["bytes_up"] += len(canonical)
            stat = client.stat(remote_path)
            remember(canonical, stat)
            result["downloaded"].append(rel)
            result["bytes_down"] += len(remote_data)

        # 1. only local ------------------------------------------------------
        if local_present and not remote_present:
            if has_previous and prev_remote_present and not local_changed and \
                    _deletion_propagates(rel):
                # The remote file was deleted since the last sync and the
                # local copy is untouched: propagate the deletion.
                self._delete_local(rel)
                state["files"].pop(rel, None)
                result["deleted_local"].append(rel)
                return
            upload(local_data)
            return

        # 2. only remote -----------------------------------------------------
        if not local_present and remote_present:
            if has_previous and prev_local and not remote_changed and \
                    _deletion_propagates(rel):
                # The local file was deleted since the last sync and the
                # remote copy is untouched: propagate the deletion.
                client.delete(remote_path)
                state["files"].pop(rel, None)
                result["deleted_remote"].append(rel)
                return
            download(client.get(remote_path), prefer_remote=True)
            return

        # 3. neither (stale state only) --------------------------------------
        if not local_present and not remote_present:
            state["files"].pop(rel, None)
            return

        # 4. both present ----------------------------------------------------
        if not has_previous:
            if rel == "settings.json" and _parse_json(local_data) == {}:
                # A fresh device has no portable settings yet (only the
                # reserved local WebDAV/secret keys).  Treat the existing
                # remote settings as authoritative instead of manufacturing
                # a conflict that would change the remote ETag.
                download(client.get(remote_path), prefer_remote=True)
                return
            if _sha(local_data) == _sha(client.get(remote_path)):
                remember(local_data, client.stat(remote_path))
                result["skipped"] += 1
                return
            remote_data = client.get(remote_path)
            stat = client.stat(remote_path)
            prefer_remote = bool(stat and stat.mtime) and \
                stat.mtime > self._local_mtime(rel)
            merged, conflict = self._resolve_conflict(
                rel, local_data, remote_data, prefer_remote=prefer_remote)
            result["conflicts"].append(conflict)
            canonical = self._apply_remote(
                rel, merged, prefer_remote=prefer_remote, local_data=local_data)
            ensure_parent()
            client.put(remote_path, canonical)
            remember(canonical, client.stat(remote_path))
            result["uploaded"].append(rel)
            result["bytes_up"] += len(canonical)
            return

        if not local_changed and not remote_changed:
            result["skipped"] += 1
            return

        if local_changed and not remote_changed:
            upload(local_data)
            return

        if remote_changed and not local_changed:
            download(client.get(remote_path), prefer_remote=True)
            return

        # both changed: merge and converge
        remote_data = client.get(remote_path)
        stat = client.stat(remote_path)
        prefer_remote = bool(stat and stat.mtime) and \
            stat.mtime > self._local_mtime(rel)
        merged, conflict = self._resolve_conflict(
            rel, local_data, remote_data, prefer_remote=prefer_remote)
        result["conflicts"].append(conflict)
        canonical = self._apply_remote(
            rel, merged, prefer_remote=prefer_remote, local_data=local_data)
        ensure_parent()
        client.put(remote_path, canonical)
        remember(canonical, client.stat(remote_path))
        result["uploaded"].append(rel)
        result["bytes_up"] += len(canonical)

    def _delete_local(self, rel: str) -> None:
        """Delete a propagated remote deletion, respecting active chat turns."""
        path = self._local_path(rel)
        if CHAT_RE.fullmatch(rel):
            with try_file_lock(path.with_suffix(".turn.lock")) as idle:
                if not idle:
                    raise ValidationError("该对话正在生成回答，本次未删除；"
                                          "请等待回答结束后再同步")
                if path.exists():
                    path.unlink()
            return
        if path.exists():
            path.unlink()

    def _local_mtime(self, rel: str) -> float:
        try:
            return self._local_path(rel).stat().st_mtime
        except OSError:
            return 0.0

    # -- public sync ---------------------------------------------------------
    def sync(self) -> dict:
        cfg = self.config.webdav()
        started = _now()
        result = {
            "ok": False, "busy": False, "started_at": started,
            "finished_at": "", "uploaded": [], "downloaded": [],
            "deleted_local": [], "deleted_remote": [], "conflicts": [],
            "errors": [], "skipped": 0, "bytes_up": 0, "bytes_down": 0,
            "remote_dir": cfg.get("remote_dir", ""), "message": "",
        }
        lock_path = self.config.webdav_lock_path
        with try_file_lock(lock_path) as acquired:
            if not acquired:
                result.update(busy=True, finished_at=_now(),
                              message="已有同步正在运行，请稍后再试")
                return result
            try:
                client = self._client()
                remote_dir = validate_remote_dir(cfg.get("remote_dir", ""))
                client.ensure_dir(remote_dir)
                local = self._local_files()
                remote = self._remote_files(client, remote_dir)
                state = self._load_state()
                ensured_dirs = {remote_dir}
                for rel in sorted(set(local) | set(remote)):
                    try:
                        self._sync_one(
                            rel, client=client, remote_dir=remote_dir,
                            local_data=local.get(rel),
                            remote_entry=remote.get(rel),
                            state=state, result=result,
                            ensured_dirs=ensured_dirs)
                    except (WebDAVError, ValidationError, OSError, ValueError) as exc:
                        result["errors"].append(
                            {"path": rel, "message": str(exc)})
                # Drop state for paths that neither side has any more.
                for rel in list(state["files"]):
                    if rel not in local and rel not in remote:
                        state["files"].pop(rel, None)
                state["last_sync_at"] = _now()
                self._save_state(state)
                result["ok"] = not result["errors"]
                result["finished_at"] = _now()
                if result["errors"]:
                    result["message"] = (
                        f"同步完成但有 {len(result['errors'])} 个文件失败；"
                        "其余文件已处理")
                elif result["conflicts"]:
                    result["message"] = (
                        f"已合并 {len(result['conflicts'])} 个冲突文件；"
                        "原始副本保存在本机 webdav/conflicts")
                else:
                    result["message"] = (
                        f"同步完成：上传 {len(result['uploaded'])}，"
                        f"下载 {len(result['downloaded'])}")
            except Exception as exc:
                result["errors"].append({"path": "", "message": str(exc)})
                result["finished_at"] = _now()
                result["message"] = str(exc)
            finally:
                try:
                    self.config.record_webdav_result(result)
                except Exception:
                    pass
        return result

    def status(self) -> dict:
        cfg = self.config.webdav()
        state = self._load_state()
        cfg["sync_state_files"] = len(state.get("files") or {})
        cfg["state_last_sync_at"] = state.get("last_sync_at", "")
        cfg["conflicts_dir"] = str(self.config.webdav_conflicts_dir)
        return cfg
