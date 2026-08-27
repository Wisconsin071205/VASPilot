"""Provider abstraction shared by all model protocols.

Contracts:
  - a provider is constructed from a :class:`ProviderEntry` (no key material;
    the API key is read from the named environment variable at call time)
  - :meth:`probe` returns a :class:`CapabilityProbe`; a provider that fails
    ANY capability is degraded to ``analysis_only`` and may never call a
    write or scheduler tool
  - :meth:`chat` returns one :class:`ProviderReply` with either final text or
    a list of tool calls whose arguments are already-parsed JSON objects
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from ..core.config import ProviderEntry
from ..core.errors import ProviderError

FULL = "full"
ANALYSIS_ONLY = "analysis_only"


@dataclass
class CapabilityProbe:
    reachable: bool = False
    streaming: bool = False
    tool_calling: bool = False
    structured_json: bool = False
    detail: str = ""
    provider_id: str = ""
    checked_at: str = ""

    @property
    def mode(self) -> str:
        return FULL if self.full else ANALYSIS_ONLY

    @property
    def full(self) -> bool:
        # structured_json is deliberately NOT required: nothing in the
        # runtime consumes response_format=json mode (the agent loop rides
        # on tool calling), so an endpoint lacking it stays fully usable.
        return (self.reachable and self.streaming
                and self.tool_calling)

    def to_dict(self) -> dict:
        return {
            "reachable": self.reachable,
            "streaming": self.streaming,
            "tool_calling": self.tool_calling,
            "structured_json": self.structured_json,
            "mode": self.mode,
            "detail": self.detail,
            "provider_id": self.provider_id,
            "checked_at": self.checked_at,
        }


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish: str = "stop"  # stop | tools | error
    usage: dict[str, Any] = field(default_factory=dict)
    raw_events: int = 0


def resolve_api_key(entry: ProviderEntry) -> str:
    """Read the API key from the env var named by ``api_key_env``."""
    if not entry.api_key_env:
        return ""
    value = os.environ.get(entry.api_key_env, "")
    if value and value.strip():
        return value.strip()
    return ""


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def require_api_key_if_remote(entry: ProviderEntry) -> str:
    """Fail fast with an actionable message instead of a remote 401.

    Cloud endpoints must carry a key (env var or the DPAPI vault); localhost
    servers (Ollama, LM Studio) and the codex-sdk bridge are exempt.
    """
    api_key = resolve_api_key(entry)
    if api_key:
        return api_key
    host = urlparse(entry.base_url or "").hostname or ""
    if entry.base_url and (host in LOCAL_HOSTS or host.endswith(".local")):
        return ""
    var = entry.api_key_env or "(no api_key_env configured)"
    hint = (f"paste the key once in the web console '配置 → 模型服务', or set "
            f"environment variable {var!r}")
    raise ProviderError(
        f"provider {entry.id!r} has no API key configured: {hint}. "
        "Keys are encrypted with Windows DPAPI or read from env vars — "
        "never stored in plaintext.")


def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """Tool arguments arrive as JSON strings or objects; both must become dicts."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip() or "{}"
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"tool arguments were not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ProviderError("tool arguments must decode to a JSON object")
        return parsed
    raise ProviderError("tool arguments must be a JSON string or object")


class BaseProvider:
    """Common plumbing; concrete providers implement ``_chat`` and ``probe``."""

    protocol = ""

    def __init__(self, entry: ProviderEntry, config=None) -> None:
        self.entry = entry
        # key precedence: environment variable > DPAPI vault (web console)
        self.api_key = resolve_api_key(entry)
        if not self.api_key and config is not None:
            try:
                self.api_key = config.get_provider_key(entry.id)
            except Exception:  # vault problems must not break construction
                self.api_key = ""
        if not self.api_key and entry.protocol != "codex-sdk":
            require_api_key_if_remote(entry)

    # -- subclass API ---------------------------------------------------------
    def probe(self) -> CapabilityProbe:  # pragma: no cover - abstract
        raise NotImplementedError

    def chat(self, messages: list[dict], tools: list[dict],
             *, stream_cb: Callable[[str], None] | None = None) -> ProviderReply:
        return self._chat(messages, tools, stream_cb=stream_cb)

    def _chat(self, messages: list[dict], tools: list[dict],
              *, stream_cb: Callable[[str], None] | None) -> ProviderReply:
        raise NotImplementedError  # pragma: no cover - abstract
