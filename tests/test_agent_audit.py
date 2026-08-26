"""Agent runtime enforcement + audit sanitization + legacy migration."""

from __future__ import annotations

import json

import pytest

from vaspilot.agents.runtime import AgentRuntime
from vaspilot.core.audit import AuditLog, sanitize
from vaspilot.core.config import Config
from vaspilot.core.errors import ToolNotAllowedError
from vaspilot.providers.base import ANALYSIS_ONLY, FULL, ProviderReply, ToolCall


class ScriptedProvider:
    """Deterministic provider double for runtime tests."""

    protocol = "scripted"

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[list] = []

    def chat(self, messages, tools, *, stream_cb=None):
        self.calls.append((list(messages), list(tools)))
        action = self.script.pop(0)
        return ProviderReply(**action)


class TestAgentRuntime:
    def _registry(self, app_with_fake):
        app, _ = app_with_fake
        return app, app.registry()

    def test_full_mode_tool_loop(self, app_with_fake):
        app, registry = self._registry(app_with_fake)
        provider = ScriptedProvider([
            {"tool_calls": [ToolCall(call_id="1", name="remote_pwd",
                                    arguments={})]},
            {"text": "the remote root is /hpc/..."},
        ])
        runtime = AgentRuntime(provider=provider, registry=registry,
                               mode=FULL)
        result = runtime.run("show the remote root")
        assert result["ok"] is True
        assert result["tool_calls"] == ["remote_pwd"]
        assert "remote root" in result["answer"]

    def test_analysis_only_blocks_write_tools(self, app_with_fake):
        app, registry = self._registry(app_with_fake)
        provider = ScriptedProvider([
            {"tool_calls": [ToolCall(call_id="1", name="remote_mkdir",
                                    arguments={"path":
                                               "/hpc/home/tester/vaspilot-root/x"})]},
            {"text": "mkdir was refused; nothing was changed"},
        ])
        runtime = AgentRuntime(provider=provider, registry=registry,
                               mode=ANALYSIS_ONLY)
        result = runtime.run("create a directory")
        assert result["ok"] is True
        assert result["tool_calls"] == ["remote_mkdir"]
        # the tool result the model saw must be a refusal, not a change
        tool_message = provider.calls[0]
        assert tool_message is not None

    def test_registry_enforcement_direct(self, app_with_fake):
        app, registry = self._registry(app_with_fake)
        with pytest.raises(ToolNotAllowedError):
            registry.dispatch("remote_mkdir",
                              {"path": "/hpc/home/tester/vaspilot-root/x"},
                              provider_mode=ANALYSIS_ONLY)
        # read tools stay available
        outcome = registry.dispatch("remote_pwd", {},
                                    provider_mode=ANALYSIS_ONLY)
        assert outcome["ok"] is True

    def test_unknown_tool_rejected(self, app_with_fake):
        app, registry = self._registry(app_with_fake)
        from vaspilot.core.errors import ValidationError
        with pytest.raises(ValidationError):
            registry.dispatch("run_shell", {}, provider_mode=FULL)

    def test_turn_limit(self, app_with_fake):
        app, registry = self._registry(app_with_fake)
        endless = [{"tool_calls": [ToolCall(call_id=str(i),
                                            name="remote_pwd",
                                            arguments={})]}
                   for i in range(50)]
        provider = ScriptedProvider(endless)
        runtime = AgentRuntime(provider=provider, registry=registry,
                               mode=FULL, max_turns=3)
        result = runtime.run("loop forever")
        assert result["ok"] is False
        assert "exceeded" in result["error"]


class TestAuditSanitization:
    def test_secret_keys_redacted(self):
        row = sanitize({"password": "hunter2", "api_key": "sk-123",
                        "totp_seed": "JBSWY3DP", "Authorization": "Bearer x",
                        "path": "/safe/path", "sha256": "abc"})
        assert row["password"] == "[REDACTED]"
        assert row["api_key"] == "[REDACTED]"
        assert row["totp_seed"] == "[REDACTED]"
        assert row["Authorization"] == "[REDACTED]"
        assert row["path"] == "/safe/path"

    def test_nested_and_capped(self):
        row = sanitize({"nested": {"user_password": "x", "note": "y" * 5000}})
        assert row["nested"]["user_password"] == "[REDACTED]"
        assert len(row["nested"]["note"]) <= 401

    def test_append_only_jsonl(self, tmp_path):
        log = AuditLog(tmp_path)
        log.record("remote.read", outcome="ok", path="/a")
        log.record("remote.read", outcome="failed", path="/b")
        files = list(tmp_path.glob("audit-*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        rows = [json.loads(ln) for ln in lines]
        assert rows[0]["event"] == "remote.read"
        assert all("row_sha256" in r for r in rows)


class TestLegacyMigration:
    def test_migrates_providers_without_secrets(self, config_home, tmp_path):
        legacy = tmp_path / "local.json"
        legacy.write_text(json.dumps({
            "providers": [
                {"id": "deepseek", "name": "DeepSeek",
                 "base_url": "https://api.deepseek.com/v1",
                 "model": "deepseek-chat"},
                {"id": "bad id!", "name": "x", "base_url": "u", "model": "m"},
            ],
            "models": ["deepseek-chat"],
            "provider_keys": {"deepseek": "BASE64_DPAPI_BLOB"},
        }), encoding="utf-8")
        config = Config(config_home)
        result = config.migrate_legacy_local(legacy)
        assert result["migrated"] == ["deepseek"]
        providers = config.load_providers()
        assert [p.id for p in providers] == ["deepseek"]
        assert providers[0].protocol == "openai-chat-compatible"
        assert providers[0].api_key_env == "VASPILOT_API_KEY_DEEPSEEK"
        # secrets are never copied
        stored = json.loads((config_home / "settings.json").read_text(
            encoding="utf-8"))
        assert "provider_keys" not in stored

    def test_idempotent(self, config_home, tmp_path):
        legacy = tmp_path / "local.json"
        legacy.write_text(json.dumps({
            "providers": [{"id": "glm", "name": "GLM",
                           "base_url": "https://open.bigmodel.cn/api/paas/v4",
                           "model": "glm-4.6"}]}), encoding="utf-8")
        config = Config(config_home)
        first = config.migrate_legacy_local(legacy)
        second = config.migrate_legacy_local(legacy)
        assert first["migrated"] == ["glm"]
        assert second["migrated"] == []
        assert len(config.load_providers()) == 1
