"""Local, non-secret configuration store.

Layout under ``~/.vaspilot`` (override with ``VASPILOT_HOME``):

  settings.json     vlab endpoint, identity-file path, default server,
                    provider list (id/name/protocol/base_url/model/api_key_env)
  servers.json      local mirror of the gateway server catalog (metadata only)
  approval.key      HMAC signing key for approval tokens (0600, auto-created)
  approvals.json    issued approvals + consumed token ids
  runs/<plan>.json  immutable workflow run state
  audit/*.jsonl     append-only audit rows

Invariants:
  - no API key plaintext, password, TOTP seed or private key is ever stored
  - ``api_key_env`` names an environment variable; the key itself is read
    from the process environment at call time only
  - writes are atomic (tempfile + os.replace)
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .errors import ConfigError, ValidationError

PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PROTOCOLS = ("openai-chat-compatible", "openai-responses", "codex-sdk")
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


def default_home() -> Path:
    override = os.environ.get("VASPILOT_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".vaspilot"


def _atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise ConfigError(f"configuration file {path} is unreadable: {exc}") from exc


class ServerEntry:
    """Mirror of one gateway catalog entry (metadata only, never secrets)."""

    __slots__ = ("name", "target", "port", "remote_root", "persist", "scheduler")

    def __init__(self, name: str, target: str, port: int = 22,
                 remote_root: str = "", persist: str = "",
                 scheduler: str = "slurm") -> None:
        self.name = name
        self.target = target
        self.port = int(port)
        self.remote_root = remote_root
        self.persist = persist
        self.scheduler = scheduler

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "target": self.target,
            "port": self.port,
            "remote_root": self.remote_root,
            "persist": self.persist,
            "scheduler": self.scheduler,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ServerEntry":
        return cls(
            name=str(data.get("name", "")),
            target=str(data.get("target", "")),
            port=int(data.get("port", 22) or 22),
            remote_root=str(data.get("remote_root", "") or ""),
            persist=str(data.get("persist", "") or ""),
            scheduler=str(data.get("scheduler", "slurm") or "slurm"),
        )


class ProviderEntry:
    """A model provider registration; the API key lives in an env var only."""

    __slots__ = ("id", "name", "protocol", "base_url", "model", "api_key_env")

    def __init__(self, id: str, name: str, protocol: str, base_url: str,
                 model: str, api_key_env: str = "") -> None:
        self.id = id
        self.name = name
        self.protocol = protocol
        self.base_url = base_url
        self.model = model
        self.api_key_env = api_key_env

    def to_dict(self) -> dict:
        # Never add an api_key field here; plaintext keys must not persist.
        return {
            "id": self.id,
            "name": self.name,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProviderEntry":
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            protocol=str(data.get("protocol", "")),
            base_url=str(data.get("base_url", "")),
            model=str(data.get("model", "")),
            api_key_env=str(data.get("api_key_env", "") or ""),
        )


class Config:
    """Typed accessor over the local configuration directory."""

    def __init__(self, home: str | Path | None = None) -> None:
        self.home = Path(home).expanduser() if home else default_home()

    # -- paths ---------------------------------------------------------------
    @property
    def settings_path(self) -> Path:
        return self.home / "settings.json"

    @property
    def servers_path(self) -> Path:
        return self.home / "servers.json"

    @property
    def approvals_path(self) -> Path:
        return self.home / "approvals.json"

    @property
    def approval_key_path(self) -> Path:
        return self.home / "approval.key"

    @property
    def runs_dir(self) -> Path:
        return self.home / "runs"

    @property
    def audit_dir(self) -> Path:
        return self.home / "audit"

    # -- settings ------------------------------------------------------------
    def load_settings(self) -> dict:
        return _load_json(self.settings_path, {})

    def save_settings(self, settings: dict) -> None:
        _atomic_write_json(self.settings_path, settings)

    def update_settings(self, **changes) -> dict:
        settings = self.load_settings()
        settings.update(changes)
        self.save_settings(settings)
        return settings

    @property
    def vlab(self) -> dict:
        data = self.load_settings().get("vlab") or {}
        return {
            "host": str(data.get("host", "vlab.ustc.edu.cn")),
            "user": str(data.get("user", "ubuntu")),
            "port": int(data.get("port", 22) or 22),
            "identity_file": str(data.get("identity_file", "") or ""),
            "gateway_path": str(data.get("gateway_path", "~/bin/vaspilot-gateway")),
        }

    def set_vlab(self, **changes) -> dict:
        current = self.vlab
        current.update({k: v for k, v in changes.items() if v is not None})
        self.update_settings(vlab=current)
        return current

    def identity_file(self) -> str:
        override = os.environ.get("VASPILOT_IDENTITY_FILE", "").strip()
        if override:
            return override
        return self.vlab["identity_file"]

    # -- servers (local mirror) ----------------------------------------------
    def load_servers(self) -> list[ServerEntry]:
        raw = _load_json(self.servers_path, {"servers": []})
        items = raw.get("servers") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return []
        out = []
        for item in items:
            if isinstance(item, dict) and item.get("name") and item.get("target"):
                out.append(ServerEntry.from_dict(item))
        return out

    def save_servers(self, servers: list[ServerEntry], *, default: str = "") -> None:
        payload = {
            "default": default or self.default_server(),
            "servers": [s.to_dict() for s in servers],
        }
        _atomic_write_json(self.servers_path, payload)

    def default_server(self) -> str:
        data = _load_json(self.servers_path, {"default": ""})
        value = data.get("default") if isinstance(data, dict) else ""
        return value if isinstance(value, str) else ""

    def set_default_server(self, name: str) -> None:
        servers = self.load_servers()
        if name and not any(s.name == name for s in servers):
            raise ConfigError(f"server {name!r} is not registered")
        data = _load_json(self.servers_path, {"servers": []})
        data["default"] = name
        _atomic_write_json(self.servers_path, data)

    def upsert_server(self, entry: ServerEntry) -> None:
        servers = [s for s in self.load_servers() if s.name != entry.name]
        servers.append(entry)
        self.save_servers(servers)

    def remove_server(self, name: str) -> None:
        servers = [s for s in self.load_servers() if s.name != name]
        self.save_servers(servers)
        if self.default_server() == name:
            data = _load_json(self.servers_path, {})
            data["default"] = ""
            _atomic_write_json(self.servers_path, data)

    # -- providers -----------------------------------------------------------
    def load_providers(self) -> list[ProviderEntry]:
        raw = self.load_settings().get("providers")
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw:
            if isinstance(item, dict):
                try:
                    out.append(ProviderEntry.from_dict(item))
                except (TypeError, ValueError):
                    continue
        return out

    def save_providers(self, providers: list[ProviderEntry]) -> None:
        seen = set()
        for provider in providers:
            if not PROVIDER_ID_RE.fullmatch(provider.id):
                raise ValidationError(f"provider id {provider.id!r} is invalid")
            if provider.id in seen:
                raise ValidationError(f"provider id {provider.id!r} is duplicated")
            seen.add(provider.id)
            if provider.protocol not in PROTOCOLS:
                raise ValidationError(
                    f"provider protocol {provider.protocol!r} must be one of {PROTOCOLS}")
        settings = self.load_settings()
        settings["providers"] = [p.to_dict() for p in providers]
        self.save_settings(settings)

    def default_provider(self) -> str:
        value = self.load_settings().get("default_provider")
        return value if isinstance(value, str) else ""

    def set_default_provider(self, pid: str) -> None:
        if not any(p.id == pid for p in self.load_providers()):
            raise ConfigError(f"provider {pid!r} is not registered")
        self.update_settings(default_provider=pid)

    def add_provider(self, entry: ProviderEntry) -> None:
        providers = self.load_providers()
        providers = [p for p in providers if p.id != entry.id]
        providers.append(entry)
        self.save_providers(providers)

    def remove_provider(self, pid: str) -> None:
        providers = [p for p in self.load_providers() if p.id != pid]
        self.save_providers(providers)
        if self.default_provider() == pid:
            settings = self.load_settings()
            settings.pop("default_provider", None)
            self.save_settings(settings)

    # -- approval signing key --------------------------------------------------
    def approval_signing_key(self) -> bytes:
        """Load (or first-create) the per-install approval HMAC key."""
        path = self.approval_key_path
        data = b""
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            pass
        if len(data) >= 32:
            return data
        fresh = secrets.token_bytes(48)
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_BINARY: without it MSVCRT opens in text mode and rewrites \n
        # bytes inside the random key into \r\n, corrupting the HMAC key
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, fresh)
        finally:
            os.close(fd)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        return fresh

    # -- approvals ledger -------------------------------------------------------
    def load_approvals(self) -> dict:
        return _load_json(self.approvals_path, {"issued": [], "consumed": []})

    def save_approvals(self, data: dict) -> None:
        _atomic_write_json(self.approvals_path, data)

    # -- legacy migration --------------------------------------------------------
    def migrate_legacy_local(self, legacy_path: str | Path | None = None) -> dict:
        """Import the pre-1.0 ``local.json`` (models/providers) format.

        Legacy providers carried {id, name, base_url, model} without a
        protocol or api_key_env; they become openai-chat-compatible entries.
        Secret-bearing fields (provider_keys DPAPI blobs, anything
        secret-looking) are dropped, never copied. Idempotent: already-known
        provider ids are skipped.
        """
        path = Path(legacy_path).expanduser() if legacy_path else \
            Path.home() / ".vaspilot" / "local.json"
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                legacy = json.load(handle)
        except (OSError, ValueError):
            return {"migrated": [], "skipped": [], "note": "no legacy file"}
        if not isinstance(legacy, dict):
            return {"migrated": [], "skipped": [], "note": "legacy file malformed"}
        existing = {p.id for p in self.load_providers()}
        migrated, skipped = [], []
        for item in legacy.get("providers") or []:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("id") or "")
            if not pid or not PROVIDER_ID_RE.fullmatch(pid) or pid in existing:
                skipped.append(pid or "(invalid)")
                continue
            entry = ProviderEntry(
                id=pid,
                name=str(item.get("name") or pid)[:40],
                protocol="openai-chat-compatible",
                base_url=str(item.get("base_url") or ""),
                model=str(item.get("model") or ""),
                api_key_env=f"VASPILOT_API_KEY_{pid.upper().replace('-', '_')}")
            if not entry.base_url or not entry.model:
                skipped.append(pid)
                continue
            self.add_provider(entry)
            existing.add(pid)
            migrated.append(pid)
        return {"migrated": migrated, "skipped": skipped,
                "note": "secrets were never copied" if migrated
                        else "nothing to migrate"}

    # -- misc -------------------------------------------------------------------
    def now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
