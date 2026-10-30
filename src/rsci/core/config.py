"""Local, non-secret configuration store.

Layout under ``~/.rsci`` (override with ``RSCI_HOME``):

  settings.json     vlab endpoint, identity-file path, default server,
                    provider list (id/name/protocol/base_url/model/api_key_env),
                    optional platform-encrypted per-server credentials and
                    WebDAV sync settings
  servers.json      local mirror of the gateway server catalog (metadata only)
  approval.key      HMAC signing key for approval tokens (0600, auto-created)
  approvals.json    issued approvals + consumed token ids
  runs/<plan>.json  immutable workflow run state
  audit/*.jsonl     append-only audit rows

Invariants:
  - API keys, server passwords/TOTP seeds and WebDAV passwords are encrypted
    before persistence (Windows DPAPI; AES-GCM on POSIX); plaintext secrets
    are never serialized
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .errors import ConfigError, ValidationError

PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PROTOCOLS = ("openai-chat-compatible", "openai-responses", "codex-sdk")
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


def default_home() -> Path:
    override = os.environ.get("RSCI_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".rsci"


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
    """Mirror of one gateway catalog entry (metadata only, never secrets).

    ``auth_mode``: 'interactive' (default, password+TOTP in a visible
    terminal) or 'key' (per-server Ed25519 key on the gateway host, enabling
    unattended auto-reconnect). ``auto_connect`` gates that reconnect.
    ``route`` selects Vlab routing (the default) or direct workstation SSH.
    Optional credentials for either route live separately in the local
    platform-encrypted vault.
    """

    __slots__ = ("name", "target", "port", "remote_root", "persist",
                 "scheduler", "auth_mode", "auto_connect", "route")

    def __init__(self, name: str, target: str, port: int = 22,
                 remote_root: str = "", persist: str = "",
                 scheduler: str = "slurm", auth_mode: str = "interactive",
                 auto_connect: bool = False, route: str = "vlab") -> None:
        self.name = name
        self.target = target
        self.port = int(port)
        self.remote_root = remote_root
        self.persist = persist
        self.scheduler = scheduler
        self.auth_mode = auth_mode if auth_mode in ("interactive", "key") \
            else "interactive"
        self.auto_connect = bool(auto_connect)
        self.route = route if route in ("vlab", "direct") else "vlab"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "target": self.target,
            "port": self.port,
            "remote_root": self.remote_root,
            "persist": self.persist,
            "scheduler": self.scheduler,
            "auth_mode": self.auth_mode,
            "auto_connect": self.auto_connect,
            "route": self.route,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ServerEntry":
        # legacy entries without the fields migrate to interactive/no-auto
        return cls(
            name=str(data.get("name", "")),
            target=str(data.get("target", "")),
            port=int(data.get("port", 22) or 22),
            remote_root=str(data.get("remote_root", "") or ""),
            persist=str(data.get("persist", "") or ""),
            scheduler=str(data.get("scheduler", "slurm") or "slurm"),
            auth_mode=str(data.get("auth_mode", "interactive") or "interactive"),
            auto_connect=bool(data.get("auto_connect", False)),
            route=str(data.get("route", "vlab") or "vlab"),
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

    @property
    def projects_dir(self) -> Path:
        return self.home / "projects"

    @property
    def projects_index_path(self) -> Path:
        return self.home / "projects.json"

    @property
    def chat_dir(self) -> Path:
        return self.home / "chat"

    @property
    def skills_dir(self) -> Path:
        return self.home / "skills"

    @property
    def metrics_dir(self) -> Path:
        return self.home / "metrics"

    @property
    def jobs_dir(self) -> Path:
        return self.home / "jobs"

    @property
    def pending_submits_path(self) -> Path:
        return self.home / "pending_submits.json"

    @property
    def webdav_dir(self) -> Path:
        return self.home / "webdav"

    @property
    def webdav_state_path(self) -> Path:
        return self.webdav_dir / "state.json"

    @property
    def webdav_lock_path(self) -> Path:
        return self.webdav_dir / "sync.lock"

    @property
    def webdav_conflicts_dir(self) -> Path:
        return self.webdav_dir / "conflicts"

    @property
    def secret_key_path(self) -> Path:
        """POSIX master key bound to this configuration directory."""
        return self.home / "secret.key"

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
            "gateway_path": str(data.get("gateway_path", "~/bin/rsci-gateway")),
            "workspace_gateway_path": str(data.get(
                "workspace_gateway_path", "~/bin/huwei-workspace-gateway")),
            # Missing core gateways are installed lazily on the first Vlab
            # operation.  Users can turn this off when their Vlab account is
            # managed centrally and deployments must be manual.
            "auto_deploy_gateway": bool(data.get("auto_deploy_gateway", True)),
        }

    def set_vlab(self, **changes) -> dict:
        current = self.vlab
        current.update({k: v for k, v in changes.items() if v is not None})
        self.update_settings(vlab=current)
        return current

    def identity_file(self) -> str:
        override = os.environ.get("RSCI_IDENTITY_FILE", "").strip()
        if override:
            return override
        return self.vlab["identity_file"]

    # -- agent execution policy -------------------------------------------------
    def agent_submit_mode(self) -> str:
        """'confirm' (default): model job submissions pause for a human click;
        'auto': the agent submits directly, audit-only."""
        value = self.load_settings().get("agent_submit_mode")
        return value if value in ("confirm", "auto") else "confirm"

    def set_agent_submit_mode(self, mode: str) -> str:
        if mode not in ("confirm", "auto"):
            raise ValidationError("agent_submit_mode must be 'confirm' or 'auto'")
        self.update_settings(agent_submit_mode=mode)
        return mode

    # -- monitoring ------------------------------------------------------------
    def temperature_alert_c(self) -> float:
        """GPU temperature above which the UI raises one cooled-down toast."""
        value = self.load_settings().get("temperature_alert_c")
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 85.0
        return min(110.0, max(40.0, number))

    def set_temperature_alert_c(self, value) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValidationError("temperature threshold must be a number")
        if not 40.0 <= number <= 110.0:
            raise ValidationError(
                "temperature threshold must be between 40 and 110 °C")
        number = round(number, 1)
        self.update_settings(temperature_alert_c=number)
        return number

    # -- web search configuration -------------------------------------------------
    WEBSEARCH_PROVIDERS = ("tavily", "zhipu", "bocha", "bing")

    def websearch(self) -> dict:
        data = self.load_settings().get("websearch") or {}
        provider = str(data.get("provider", "zhipu"))
        return {
            "provider": provider if provider in self.WEBSEARCH_PROVIDERS else "zhipu",
            "enabled": bool(data.get("enabled", False)),
            "key_saved": self.provider_key_saved("websearch"),
        }

    # -- VASPKIT ---------------------------------------------------------------
    _POTCAR_LIBRARY_RE = re.compile(r"^(/|~/)[A-Za-z0-9._/+-]{0,500}$")

    def potcar_library(self, server: str) -> str:
        """The pseudopotential library the user chose for one server; empty
        means VASPKIT's own ~/.vaspkit setting on that server applies."""
        data = self.load_settings().get("potcar_libraries") or {}
        value = data.get(server) if isinstance(data, dict) else ""
        return value if isinstance(value, str) else ""

    def set_potcar_library(self, server: str, path: str) -> str:
        path = str(path or "").strip()
        path = path.rstrip("/") or path
        if path and (not self._POTCAR_LIBRARY_RE.fullmatch(path)
                     or ".." in path.split("/")):
            raise ValidationError(
                "the pseudopotential library must be an absolute path on the "
                "server (or start with ~/), using letters, digits and . _ - + /")
        data = self.load_settings().get("potcar_libraries") or {}
        data = dict(data) if isinstance(data, dict) else {}
        if path:
            data[server] = path
        else:
            data.pop(server, None)
        self.update_settings(potcar_libraries=data)
        return path

    def set_websearch(self, *, provider: str, enabled: bool) -> dict:
        if provider not in self.WEBSEARCH_PROVIDERS:
            raise ValidationError(
                f"websearch provider must be one of {self.WEBSEARCH_PROVIDERS}")
        current = self.load_settings().get("websearch") or {}
        current.update({"provider": provider, "enabled": bool(enabled)})
        self.update_settings(websearch=current)
        return self.websearch()

    def websearch_key(self) -> str:
        """Env var beats the local encrypted vault; never logged."""
        override = os.environ.get("RSCI_WEBSEARCH_KEY", "").strip()
        return override or self.get_provider_key("websearch")

    # -- WebDAV synchronization ------------------------------------------------
    WEBDAV_DEFAULT_REMOTE_DIR = "RSCI-sync"
    WEBDAV_INTERVAL_DEFAULT = 15
    WEBDAV_INTERVAL_MIN = 1
    WEBDAV_INTERVAL_MAX = 1440

    @classmethod
    def _webdav_interval(cls, value) -> int:
        try:
            minutes = int(value)
        except (TypeError, ValueError):
            minutes = cls.WEBDAV_INTERVAL_DEFAULT
        return max(cls.WEBDAV_INTERVAL_MIN,
                   min(cls.WEBDAV_INTERVAL_MAX, minutes))

    def webdav(self) -> dict:
        """Return non-secret WebDAV state for the settings page.

        The password blob is never returned; only ``password_saved`` and
        whether an environment override is active.
        """
        from . import secretbox
        data = self.load_settings().get("webdav") or {}
        if not isinstance(data, dict):
            data = {}
        username = str(data.get("username", "") or "")
        return {
            "url": str(data.get("url", "") or ""),
            "username": username,
            "remote_dir": str(data.get("remote_dir", "") or
                              self.WEBDAV_DEFAULT_REMOTE_DIR),
            "enabled": bool(data.get("enabled", False)),
            "auto_sync": bool(data.get("auto_sync", False)),
            "interval_minutes": self._webdav_interval(
                data.get("interval_minutes")),
            "password_saved": bool(data.get("password")),
            "password_from_env": bool(
                os.environ.get("RSCI_WEBDAV_PASSWORD", "").strip()),
            "encryption_available": secretbox.available(),
            "last_sync_at": str(data.get("last_sync_at", "") or ""),
            "last_attempt_at": str(data.get("last_attempt_at", "") or ""),
            "last_result": data.get("last_result") if isinstance(
                data.get("last_result"), dict) else {},
            "conflict_count": int(data.get("conflict_count", 0) or 0),
            "device_id": str(data.get("device_id", "") or ""),
        }

    def _webdav_data(self) -> dict:
        data = self.load_settings().get("webdav")
        return data if isinstance(data, dict) else {}

    def _webdav_decrypt_password(self, username: str, blob: str) -> str:
        from . import secretbox
        if not blob:
            return ""
        try:
            return secretbox.unprotect_b64(
                blob, entropy=secretbox.webdav_credential_entropy(username),
                key_file=self.secret_key_path)
        except (OSError, ValueError) as exc:
            raise ConfigError(
                "无法解密本机保存的 WebDAV 密码；请重新输入并保存") from exc

    def webdav_password(self) -> str:
        """Environment override beats the local encrypted vault; never log."""
        override = os.environ.get("RSCI_WEBDAV_PASSWORD", "").strip()
        if override:
            return override
        data = self._webdav_data()
        username = str(data.get("username", "") or "")
        return self._webdav_decrypt_password(username,
                                             str(data.get("password", "") or ""))

    def set_webdav(self, *, url: str | None = None, username: str | None = None,
                   password: str | None = None, enabled: bool | None = None,
                   auto_sync: bool | None = None, remote_dir: str | None = None,
                   interval_minutes: int | None = None,
                   clear_password: bool = False) -> dict:
        """Update WebDAV sync settings; ``password=None`` keeps the vault.

        The URL must be a plain http(s) collection URL.  Credentials are
        encrypted with the platform secret box before ``settings.json`` is
        written; an empty password field from the UI means "keep existing".
        """
        from ..sync.webdav import normalize_url, validate_remote_dir
        from . import secretbox

        current = dict(self._webdav_data())
        old_username = str(current.get("username", "") or "")
        if url is not None:
            current["url"] = normalize_url(url)
        if username is not None:
            current["username"] = str(username or "").strip()
        if remote_dir is not None:
            current["remote_dir"] = validate_remote_dir(remote_dir)
        if enabled is not None:
            current["enabled"] = bool(enabled)
        if auto_sync is not None:
            current["auto_sync"] = bool(auto_sync)
        if interval_minutes is not None:
            current["interval_minutes"] = self._webdav_interval(interval_minutes)

        new_username = str(current.get("username", "") or "")
        if clear_password:
            current.pop("password", None)
        else:
            supplied = str(password or "")
            if supplied.strip():
                if not secretbox.available():
                    raise ConfigError(
                        "当前系统无法加密保存 WebDAV 密码；请安装 "
                        "cryptography 或使用 RSCI_WEBDAV_PASSWORD 环境变量")
                current["password"] = secretbox.protect_b64(
                    supplied, entropy=secretbox.webdav_credential_entropy(
                        new_username), key_file=self.secret_key_path)
            elif current.get("password") and old_username != new_username:
                # Username is part of the derived key: re-encrypt when it
                # changes so a preserved password remains usable.
                plaintext = self._webdav_decrypt_password(
                    old_username, str(current.get("password", "")))
                if plaintext:
                    current["password"] = secretbox.protect_b64(
                        plaintext, entropy=secretbox.webdav_credential_entropy(
                            new_username), key_file=self.secret_key_path)

        if not current.get("device_id"):
            current["device_id"] = secrets.token_hex(8)
        current["remote_dir"] = validate_remote_dir(
            str(current.get("remote_dir") or self.WEBDAV_DEFAULT_REMOTE_DIR))
        current["interval_minutes"] = self._webdav_interval(
            current.get("interval_minutes"))
        settings = self.load_settings()
        settings["webdav"] = current
        self.save_settings(settings)
        return self.webdav()

    def record_webdav_result(self, result: dict) -> dict:
        """Persist the last sync result without leaking credentials."""
        clean = {
            "ok": bool(result.get("ok")),
            "uploaded": list(result.get("uploaded") or [])[:200],
            "downloaded": list(result.get("downloaded") or [])[:200],
            "deleted_local": list(result.get("deleted_local") or [])[:200],
            "deleted_remote": list(result.get("deleted_remote") or [])[:200],
            "conflicts": list(result.get("conflicts") or [])[:50],
            "errors": list(result.get("errors") or [])[:50],
            "skipped": int(result.get("skipped") or 0),
            "bytes_up": int(result.get("bytes_up") or 0),
            "bytes_down": int(result.get("bytes_down") or 0),
            "started_at": str(result.get("started_at") or ""),
            "finished_at": str(result.get("finished_at") or self.now_iso()),
            "busy": bool(result.get("busy")),
            "message": str(result.get("message") or ""),
        }
        settings = self.load_settings()
        data = settings.get("webdav")
        if not isinstance(data, dict):
            data = {}
        data["last_result"] = clean
        data["last_attempt_at"] = self.now_iso()
        data["conflict_count"] = len(clean["conflicts"])
        if clean["ok"]:
            data["last_sync_at"] = clean["finished_at"]
        settings["webdav"] = data
        self.save_settings(settings)
        return self.webdav()

    def clear_webdav_password(self) -> dict:
        settings = self.load_settings()
        data = settings.get("webdav")
        if isinstance(data, dict):
            data.pop("password", None)
            settings["webdav"] = data
            self.save_settings(settings)
        return self.webdav()

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
        self.remove_server_credentials(name)
        if self.default_server() == name:
            data = _load_json(self.servers_path, {})
            data["default"] = ""
            _atomic_write_json(self.servers_path, data)

    # -- server credential vault (encrypted ciphertext, never plaintext) -----
    CREDENTIAL_CONFIRM_DAYS_DEFAULT = 3
    CREDENTIAL_CONFIRM_DAYS_MIN = 1
    CREDENTIAL_CONFIRM_DAYS_MAX = 30

    @classmethod
    def _credential_days(cls, value) -> int:
        try:
            days = int(value)
        except (TypeError, ValueError):
            days = cls.CREDENTIAL_CONFIRM_DAYS_DEFAULT
        return max(cls.CREDENTIAL_CONFIRM_DAYS_MIN,
                   min(cls.CREDENTIAL_CONFIRM_DAYS_MAX, days))

    def server_credentials_status(self, name: str) -> dict:
        """Return credential metadata only; never return a secret."""
        if not SERVER_NAME_RE.fullmatch(str(name or "")):
            return {"saved": False, "confirmation_due": False,
                    "confirm_interval_days": self.CREDENTIAL_CONFIRM_DAYS_DEFAULT}
        data = self.load_settings().get("server_credentials")
        entry = data.get(name) if isinstance(data, dict) else None
        if not isinstance(entry, dict) or not entry.get("password"):
            return {"saved": False, "confirmation_due": False,
                    "confirm_interval_days": self.CREDENTIAL_CONFIRM_DAYS_DEFAULT}
        days = self._credential_days(entry.get("confirm_interval_days"))
        confirmed = str(entry.get("confirmed_at") or entry.get("saved_at") or "")
        due = True
        stamp = None
        try:
            stamp = datetime.fromisoformat(confirmed.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            due = datetime.now(timezone.utc) >= stamp + timedelta(days=days)
        except (TypeError, ValueError):
            pass
        next_at = ""
        if stamp is not None:
            next_at = (stamp + timedelta(days=days)).isoformat(timespec="seconds")
        return {"saved": True, "confirmation_due": due,
                "confirmed_at": confirmed, "next_confirmation_at": next_at,
                "confirm_interval_days": days}

    def _server_credential_blobs(self, settings: dict, name: str) -> dict:
        stored = settings.get("server_credentials")
        if not isinstance(stored, dict):
            stored = {}
            settings["server_credentials"] = stored
        item = stored.get(name)
        return item if isinstance(item, dict) else {}

    def _decrypt_server_credential(self, name: str, field: str) -> str:
        from . import secretbox
        settings = self.load_settings()
        item = self._server_credential_blobs(settings, name)
        blob = item.get(field)
        if not isinstance(blob, str) or not blob:
            return ""
        try:
            return secretbox.unprotect_b64(
                blob, entropy=secretbox.server_credential_entropy(name),
                key_file=self.secret_key_path)
        except (OSError, ValueError) as exc:
            raise ConfigError(
                f"无法解密服务器 {name!r} 的本机凭据；请重新输入并保存") from exc

    def get_server_credentials(self, name: str, *, allow_expired: bool = False) -> dict:
        """Decrypt credentials for one short-lived SSH operation.

        Expired entries are withheld until the local user confirms retention.
        """
        status = self.server_credentials_status(name)
        if not status.get("saved") or (status.get("confirmation_due") and
                                        not allow_expired):
            return {}
        return {"password": self._decrypt_server_credential(name, "password"),
                "totp_seed": self._decrypt_server_credential(name, "totp_seed")}

    def save_server_credentials(self, name: str, *, password: str | None = None,
                                totp_seed: str | None = None,
                                confirm_interval_days: int | None = None) -> dict:
        """Create/update credentials with platform secret-box protection.

        ``None`` preserves an existing field. An empty password is rejected so
        a half-cleared entry can never become the automatic-auth source.
        """
        if not SERVER_NAME_RE.fullmatch(str(name or "")):
            raise ValidationError(f"server name {name!r} is invalid")
        from . import secretbox
        from .credentials import normalize_totp_seed
        settings = self.load_settings()
        stored = settings.get("server_credentials")
        if not isinstance(stored, dict):
            stored = {}
        current = stored.get(name) if isinstance(stored.get(name), dict) else {}
        if password is None:
            password = self._decrypt_server_credential(name, "password") \
                if current.get("password") else ""
        password = str(password or "")
        if not password.strip():
            raise ValidationError("服务器密码不能为空")
        if totp_seed is None:
            totp_seed = self._decrypt_server_credential(name, "totp_seed") \
                if current.get("totp_seed") else ""
        totp_seed = normalize_totp_seed(str(totp_seed or ""))
        days = self._credential_days(
            confirm_interval_days if confirm_interval_days is not None
            else current.get("confirm_interval_days"))
        now = self.now_iso()
        try:
            password_blob = secretbox.protect_b64(
                password, entropy=secretbox.server_credential_entropy(name),
                key_file=self.secret_key_path)
            totp_blob = (secretbox.protect_b64(
                totp_seed, entropy=secretbox.server_credential_entropy(name),
                key_file=self.secret_key_path)
                         if totp_seed else "")
        except OSError as exc:
            raise ConfigError(
                "当前系统没有可用的本机凭据加密后端，无法安全保存服务器"
                "凭据；请安装 cryptography，或改用 SSH 密钥/代理") from exc
        stored[name] = {
            "password": password_blob,
            "totp_seed": totp_blob,
            "saved_at": now,
            "confirmed_at": now,
            "confirm_interval_days": days,
        }
        settings["server_credentials"] = stored
        self.save_settings(settings)
        return self.server_credentials_status(name)

    def confirm_server_credentials(self, name: str) -> dict:
        """Record the user's periodic confirmation without exposing secrets."""
        status = self.server_credentials_status(name)
        if not status.get("saved"):
            raise ValidationError("该服务器没有已保存的加密凭据")
        # Validate the encrypted blob before extending its trust window; a copied
        # or corrupted settings file must never be confirmed blindly.
        if not self._decrypt_server_credential(name, "password"):
            raise ConfigError("服务器密码密文为空，请重新输入并保存")
        settings = self.load_settings()
        stored = settings.get("server_credentials")
        item = stored.get(name) if isinstance(stored, dict) else None
        if not isinstance(item, dict):
            raise ValidationError("该服务器没有已保存的加密凭据")
        item["confirmed_at"] = self.now_iso()
        item["confirm_interval_days"] = self._credential_days(
            item.get("confirm_interval_days"))
        self.save_settings(settings)
        return self.server_credentials_status(name)

    def remove_server_credentials(self, name: str) -> None:
        settings = self.load_settings()
        stored = settings.get("server_credentials")
        if isinstance(stored, dict) and name in stored:
            del stored[name]
            settings["server_credentials"] = stored
            self.save_settings(settings)

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

    # -- provider key vault (encrypted ciphertext, never plaintext) -----------
    def set_provider_key(self, pid: str, plaintext: str) -> None:
        """Encrypt with the platform secret box and store base64."""
        from . import secretbox
        if not plaintext or not plaintext.strip():
            raise ValidationError("provider key must be non-empty")
        if not PROVIDER_ID_RE.fullmatch(pid):
            raise ValidationError(f"provider id {pid!r} is invalid")
        blob = secretbox.protect_b64(plaintext.strip(),
                                     key_file=self.secret_key_path)
        settings = self.load_settings()
        stored = settings.get("provider_keys")
        if not isinstance(stored, dict):
            stored = {}
        stored[pid] = blob
        settings["provider_keys"] = stored
        self.save_settings(settings)

    def get_provider_key(self, pid: str) -> str:
        """Decrypt the stored key; "" when absent/unreadable. The value is
        meant for one outgoing Authorization header only — callers must not
        log or persist it."""
        from . import secretbox
        stored = self.load_settings().get("provider_keys")
        if not isinstance(stored, dict):
            return ""
        blob = stored.get(pid)
        if not isinstance(blob, str) or not blob:
            return ""
        try:
            return secretbox.unprotect_b64(blob, key_file=self.secret_key_path)
        except (OSError, ValueError):
            return ""

    def remove_provider_key(self, pid: str) -> None:
        settings = self.load_settings()
        stored = settings.get("provider_keys")
        if isinstance(stored, dict) and pid in stored:
            del stored[pid]
            settings["provider_keys"] = stored
            self.save_settings(settings)

    def provider_key_saved(self, pid: str) -> bool:
        stored = self.load_settings().get("provider_keys")
        return bool(isinstance(stored, dict) and stored.get(pid))

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
        Secret-bearing fields (provider_keys encrypted blobs, anything
        secret-looking) are dropped, never copied. Idempotent: already-known
        provider ids are skipped.
        """
        if legacy_path:
            path = Path(legacy_path).expanduser()
        else:
            candidates = (
                Path.home() / ".vasploom" / "local.json",
                Path.home() / ".vaspilot" / "local.json",
                Path.home() / ".rsci" / "local.json",
            )
            path = next((candidate for candidate in candidates if candidate.is_file()),
                        candidates[0])
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
                api_key_env=f"RSCI_API_KEY_{pid.upper().replace('-', '_')}")
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
