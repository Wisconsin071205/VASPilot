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
        app, registry = _registry_of(app_with_fake)
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
        app, registry = _registry_of(app_with_fake)
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
        app, registry = _registry_of(app_with_fake)
        with pytest.raises(ToolNotAllowedError):
            registry.dispatch("remote_mkdir",
                              {"path": "/hpc/home/tester/vaspilot-root/x"},
                              provider_mode=ANALYSIS_ONLY)
        # read tools stay available
        outcome = registry.dispatch("remote_pwd", {},
                                    provider_mode=ANALYSIS_ONLY)
        assert outcome["ok"] is True

    def test_unknown_tool_rejected(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        from vaspilot.core.errors import ValidationError
        with pytest.raises(ValidationError):
            registry.dispatch("run_shell", {}, provider_mode=FULL)

    def test_turn_limit(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
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

    def test_turn_cap_nudge_grants_continuation(self, app_with_fake):
        """At the soft cap the runtime injects a continuation nudge and runs
        on, instead of ending mid-sentence like the 13-tool-call Bader run."""
        app, registry = _registry_of(app_with_fake)
        scripted = [
            {"tool_calls": [ToolCall(call_id="1", name="remote_pwd",
                                     arguments={})]},
            {"tool_calls": [ToolCall(call_id="2", name="remote_pwd",
                                     arguments={})]},
            {"text": "任务完成：一切就绪"},
        ]
        runtime = AgentRuntime(provider=ScriptedProvider(scripted),
                               registry=registry, mode=FULL, max_turns=2)
        result = runtime.run("do it")
        # max_turns=2：第 2 回合触顶注入「继续」，第 3 回合给出结论
        assert result["ok"] is True and result["turns"] == 3
        assert "完成" in result["answer"]


def _registry_of(app_with_fake):
    app, _ = app_with_fake
    # session-scoped so persistent-terminal tests can track cwd memory
    return app, app.build_registry(session_id="test-session")


class TestRuntimeHistory:
    def test_run_injects_history(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        provider = ScriptedProvider([{"text": "you said Fe before"}])
        runtime = AgentRuntime(provider=provider, registry=registry,
                               mode=FULL)
        history = [
            {"role": "system", "content": "must be dropped"},
            {"role": "user", "content": "analyse Fe"},
            {"role": "assistant", "content": "Fe is bcc"},
            {"role": "tool", "content": "also dropped"},
        ]
        result = runtime.run("continue", history=history)
        assert result["ok"] is True
        messages = provider.calls[0][0]
        assert messages[0]["role"] == "system"
        assert messages[1] == {"role": "user", "content": "analyse Fe"}
        assert messages[2] == {"role": "assistant", "content": "Fe is bcc"}
        assert messages[3] == {"role": "user", "content": "continue"}
        assert len(messages) == 4

    def test_run_without_history_unchanged(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        provider = ScriptedProvider([{"text": "fresh"}])
        runtime = AgentRuntime(provider=provider, registry=registry,
                               mode=FULL)
        runtime.run("hello")
        messages = provider.calls[0][0]
        assert len(messages) == 2

    def test_system_extra_appended(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        provider = ScriptedProvider([{"text": "ok"}])
        runtime = AgentRuntime(provider=provider, registry=registry,
                               mode=FULL, system_extra="Skills: demo-skill")
        runtime.run("hi")
        system = provider.calls[0][0][0]["content"]
        assert system.rstrip().endswith("Skills: demo-skill")


class TestShellTools:
    def test_shell_run_executes(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        result = registry.dispatch("shell_run",
                                   {"command": "echo agent-shell-ok"})
        assert result["ok"] is True
        assert "agent-shell-ok" in result["stdout"]
        assert result["rc"] == 0

    def test_shell_run_reports_failure(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        result = registry.dispatch("shell_run",
                                   {"command": "exit 3"})
        assert result["ok"] is False
        assert result["rc"] == 3

    def test_shell_run_timeout(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        result = registry.dispatch("shell_run",
                                   {"command": "ping -n 30 127.0.0.1",
                                    "timeout_seconds": 1})
        assert result["ok"] is False
        assert result.get("timeout") is True

    def test_shell_run_analysis_only_blocked(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        with pytest.raises(ToolNotAllowedError):
            registry.dispatch("shell_run", {"command": "echo x"},
                              provider_mode=ANALYSIS_ONLY)

    def test_remote_run_via_gateway(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        result = registry.dispatch("remote_run",
                                   {"command": "echo remote-ok"})
        assert result["ok"] is True
        assert "remote-ok" in result["stdout"]

    def test_remote_run_reports_rc(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        result = registry.dispatch("remote_run", {"command": "boom now"})
        assert result["ok"] is False
        assert result["rc"] == 2

    def test_remote_run_persists_cwd_across_commands(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        first = registry.dispatch("remote_run", {"command": "cd /tmp && ls"})
        assert first["cwd"] == "/fake/cwd-1"
        second = registry.dispatch("remote_run", {"command": "ls"})
        assert second["cwd"] == "/fake/cwd-2"
        # 第二条命令确实先 cd 回了上一条停下的目录
        state_exec = [c for _, c in registry.context.client.transport
                      .state.exec_log]
        assert state_exec[-1].count("cd ") == 1 and \
            "cd /fake/cwd-1 2>/dev/null" in state_exec[-1]

    def test_remote_run_reset_forgets_cwd(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        registry.dispatch("remote_run", {"command": "cd /tmp && ls"})
        reset = registry.dispatch("remote_run",
                                  {"command": "pwd", "reset": True})
        assert reset["cwd"] == "/fake/cwd-2"
        state_exec = [c for _, c in registry.context.client.transport
                      .state.exec_log]
        assert "cd '" not in state_exec[-1]

    def test_shell_audited(self, app_with_fake):
        app, _ = _registry_of(app_with_fake)
        from vaspilot.tools.registry import ToolContext, ToolRegistry
        audit_calls = []
        context = ToolContext(config=app.config, client=app.client(),
                              audit=type("A", (), {"record":
                                        staticmethod(
                                            lambda event, **kw:
                                            audit_calls.append((event, kw)))})())
        registry = ToolRegistry(context)
        registry.dispatch("shell_run", {"command": "echo audited"})
        assert audit_calls and audit_calls[0][0] == "shell.run"
        assert audit_calls[0][1]["command"] == "echo audited"


class TestJobSubmitTiers:
    ARGS = {"server": "cl9",
            "directory": "/hpc/home/tester/vaspilot-root/runs/t",
            "script": "run.job.sh"}

    def test_confirm_mode_queues_pending(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        app.config.set_agent_submit_mode("confirm")
        result = registry.dispatch("job_submit", dict(self.ARGS))
        assert result["ok"] is True
        assert result["status"] == "pending_confirmation"
        assert result["id"].startswith("ps-")
        from vaspilot.workflow.pending import PendingSubmitStore
        pending = PendingSubmitStore(app.config.pending_submits_path).pending()
        assert len(pending) == 1
        assert pending[0]["directory"] == self.ARGS["directory"]
        # nothing reached the scheduler
        assert not any("submit" in " ".join(c[:1]) for c in
                       app.transport().calls)

    def test_auto_mode_submits_directly(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        app.config.set_agent_submit_mode("auto")
        result = registry.dispatch("job_submit", dict(self.ARGS))
        assert result["ok"] is True
        assert result.get("job_id")
        transport = app.transport()
        assert any(call[0] == "submit" for call in transport.calls)

    def test_analysis_only_blocked(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        with pytest.raises(ToolNotAllowedError):
            registry.dispatch("job_submit", dict(self.ARGS),
                              provider_mode=ANALYSIS_ONLY)


class TestProjectAndSkillTools:
    def test_project_tools_roundtrip(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        created = registry.dispatch("project_create", {
            "name": "toolproj", "incar": "NSW=0\n",
            "kpoints": "0\nGamma\n1 1 1\n", "poscar": "struct\n"})
        assert created["ok"] is True
        listing = registry.dispatch("project_list", {})
        assert any(p["name"] == "toolproj" for p in listing["projects"])
        written = registry.dispatch("project_write", {
            "project": "toolproj", "file": "INCAR", "content": "NSW=5\n"})
        assert written["ok"] is True
        doc = registry.dispatch("project_read",
                                {"project": "toolproj", "file": "INCAR"})
        assert doc["content"] == "NSW=5\n"

    def test_project_write_potcar_refused(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        registry.dispatch("project_create", {"name": "np",
                                             "incar": "NSW=0\n"})
        from vaspilot.core.errors import ValidationError
        with pytest.raises(ValidationError):
            registry.dispatch("project_write", {
                "project": "np", "file": "POTCAR", "content": "TITEL=x"})

    def test_project_validate_tool(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        registry.dispatch("project_create", {"name": "v",
                                             "incar": "NSW=0\n"})
        result = registry.dispatch("project_validate", {"project": "v"})
        assert result["ok"] is False  # KPOINTS/POSCAR missing
        assert result["errors"]

    def test_skill_tools_roundtrip(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        written = registry.dispatch("skill_write", {
            "name": "neb-setup", "description": "CI-NEB 建模流程",
            "body": "step1 relax endpoints\nstep2 interpolate"})
        assert written["ok"] is True
        listing = registry.dispatch("skill_list", {})
        assert any(s["name"] == "neb-setup" for s in listing["skills"])
        doc = registry.dispatch("skill_read", {"name": "neb-setup"})
        assert doc["content"].startswith("step1")
        # no skill_delete tool exists for the model
        assert "skill_delete" not in registry.names()

    def test_skill_index_in_prompt(self, app_with_fake):
        app, _ = _registry_of(app_with_fake)
        from vaspilot.agents.skills import SkillStore
        store = SkillStore(app.config.skills_dir)
        store.write("demo", "a demo skill", "do the thing")
        index = store.index_prompt()
        assert "demo" in index and "a demo skill" in index

    def test_registry_contains_new_tools(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        names = registry.names()
        for expected in ("shell_run", "remote_run", "server_metrics",
                         "project_create", "project_list", "project_read",
                         "project_write", "project_validate",
                         "skill_list", "skill_read", "skill_write",
                         "web_search", "web_fetch"):
            assert expected in names, expected

    def test_server_metrics_tool(self, app_with_fake):
        app, registry = _registry_of(app_with_fake)
        result = registry.dispatch("server_metrics", {"server": "cl9"})
        assert result["ok"] is True
        assert result["cpu"]["usage_pct"] == pytest.approx(60.0)
        assert result["gpus"][0]["name"] == "NVIDIA A100"
        assert result["queue"]["kind"] == "slurm"


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
