"""Provider registry: id -> protocol -> concrete class."""

from __future__ import annotations

from ..core.config import Config, ProviderEntry
from ..core.errors import ConfigError
from .base import ANALYSIS_ONLY, FULL, BaseProvider, CapabilityProbe, ProviderReply, ToolCall
from .openai_chat import OpenAIChatCompatibleProvider
from .openai_responses import OpenAIResponsesProvider
from .codex_sdk import CodexSdkProvider

PROTOCOL_CLASSES = {
    "openai-chat-compatible": OpenAIChatCompatibleProvider,
    "openai-responses": OpenAIResponsesProvider,
    "codex-sdk": CodexSdkProvider,
}


def build_provider(entry: ProviderEntry) -> BaseProvider:
    cls = PROTOCOL_CLASSES.get(entry.protocol)
    if cls is None:
        raise ConfigError(f"unknown provider protocol {entry.protocol!r}")
    return cls(entry)


def provider_by_id(config: Config, pid: str) -> tuple[ProviderEntry, BaseProvider]:
    for entry in config.load_providers():
        if entry.id == pid:
            return entry, build_provider(entry)
    raise ConfigError(f"provider {pid!r} is not registered; "
                      "run 'vaspilot agent provider list'")


def default_provider(config: Config) -> tuple[ProviderEntry, BaseProvider]:
    pid = config.default_provider()
    if not pid:
        providers = config.load_providers()
        if not providers:
            raise ConfigError("no providers registered; "
                              "run 'vaspilot agent provider add'")
        pid = providers[0].id
    return provider_by_id(config, pid)


__all__ = [
    "ANALYSIS_ONLY", "FULL", "BaseProvider", "CapabilityProbe",
    "ProviderReply", "ToolCall", "PROTOCOL_CLASSES", "build_provider",
    "provider_by_id", "default_provider",
    "OpenAIChatCompatibleProvider", "OpenAIResponsesProvider", "CodexSdkProvider",
]
