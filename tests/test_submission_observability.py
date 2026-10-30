"""Submission evidence reaches agents without guessing from an empty queue."""

import json
import shlex
from datetime import timedelta
from types import SimpleNamespace

import pytest

from rsci.agents.runtime import AgentRuntime
from rsci.core.errors import RemoteError, ValidationError
from rsci.providers.base import ANALYSIS_ONLY, FULL, ToolCall
from rsci.workflow import pending
from rsci.workflow.pending import PendingSubmitStore
from tests.conftest import ROOT
from tests.test_agent_audit import ScriptedProvider
from tests.test_ui import call, sse_chat, ui


ERROR = ("qsub: submit error (No default queue specified "
         "MSG=requested queue not found)")


def attempted(store, *, session_id="s-current", directory="/case"):
    entry = store.create(server="cl9", directory=directory, script="run.job.sh",
                         script_content="private script body", session_id=session_id)
    store.claim(entry["id"])
    store.finish_submission(entry["id"], error=ERROR)
    return entry


def test_inspection_keeps_approval_evidence_after_card_expiry(tmp_path, monkeypatch):
    store = PendingSubmitStore(tmp_path / "pending.json")
    entry = attempted(store)
    original = store.path.read_bytes()
    monkeypatch.setattr(pending, "_utcnow", lambda: (
        pending.datetime.now(pending.timezone.utc) + timedelta(days=2)))

    row = store.inspect(entry_id=entry["id"])[0]

    assert row["status"] == "uncertain"
    assert row["approval_received"] and row["submission_attempted"]
    assert not row["awaiting_approval"]
    assert row["scheduler_accepted"] is None
    assert row["requires_remote_reconciliation"]
    assert row["submission_started_at"] and row["error"] == ERROR
    assert "script_content" not in row
    assert store.path.read_bytes() == original  # a diagnostic cannot reset state


def test_inspection_includes_success_and_rejection_and_expires_only_in_view(tmp_path):
    store = PendingSubmitStore(tmp_path / "pending.json")
    approved = store.create(server="cl9", directory="/ok", script="run.sh")
    store.claim(approved["id"])
    store.finish_submission(approved["id"], result={"job_id": "123", "raw": "private"})
    rejected = store.create(server="cl9", directory="/no", script="run.sh")
    store.settle(rejected["id"], "rejected")
    expired = store.create(server="cl9", directory="/old", script="run.sh", ttl_seconds=-1)
    original = store.path.read_bytes()

    rows = {row["id"]: row for row in store.inspect()}

    assert rows[approved["id"]]["scheduler_accepted"] is True
    assert rows[approved["id"]]["job_id"] == "123"
    assert "result" not in rows[approved["id"]]
    assert rows[rejected["id"]]["status"] == "rejected"
    assert not rows[rejected["id"]]["submission_attempted"]
    assert rows[expired["id"]]["status"] == "expired"
    assert not rows[expired["id"]]["approval_received"]
    assert store.path.read_bytes() == original


@pytest.mark.parametrize("content", ["{truncated", "[]", '{"ps-bad": 1}'])
def test_unreadable_records_are_unknown_not_empty_history(tmp_path, content):
    path = tmp_path / "pending.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValidationError, match="state is unknown"):
        PendingSubmitStore(path).inspect()


def test_status_tool_is_read_only_session_scoped_and_available_in_analysis_mode(app_with_fake):
    app, _ = app_with_fake
    store = PendingSubmitStore(app.config.pending_submits_path)
    own = attempted(store)
    other = attempted(store, session_id="s-other", directory="/other")
    registry = app.build_registry(session_id="s-current")
    before = list(app.transport().calls)

    result = registry.dispatch("job_submission_status", {}, provider_mode=ANALYSIS_ONLY)

    assert result["source"] == "local_submission_records"
    assert result["history_complete"] is False
    assert [row["id"] for row in result["submissions"]] == [own["id"]]
    assert result["submissions"][0]["error"] == ERROR
    assert app.transport().calls == before
    with pytest.raises(ValidationError, match="requested scope"):
        registry.dispatch("job_submission_status", {"id": other["id"]})
    outside = app.build_registry()
    with pytest.raises(ValidationError, match="outside a chat"):
        outside.dispatch("job_submission_status", {})
    assert outside.dispatch("job_submission_status", {"id": own["id"]})["submissions"]


def test_old_failure_records_reach_model_even_without_a_session_notification(app_with_fake):
    app, _ = app_with_fake
    store = PendingSubmitStore(app.config.pending_submits_path)
    entry = attempted(store)
    provider = ScriptedProvider([{"text": "已读取提交错误。"}])
    runtime = AgentRuntime(provider=provider, registry=app.build_registry(session_id="s-current"), mode=FULL)

    runtime.run("为什么没提交成功？", history=[
        {"role": "assistant", "content": "还在等你点击审批卡片。"}])

    system = provider.calls[0][0][0]["content"]
    assert entry["id"] in system and ERROR in system
    assert '"approval_received": true' in system
    assert '"awaiting_approval": false' in system
    assert "private script body" not in system


def test_context_refreshes_when_approval_finishes_between_model_calls(app_with_fake, monkeypatch):
    app, _ = app_with_fake
    store = PendingSubmitStore(app.config.pending_submits_path)
    entry = store.create(server="cl9", directory="/case", script="run.sh", session_id="s-current")
    original = app.client().pwd

    def approval_finishes(server=None):
        store.claim(entry["id"])
        store.finish_submission(entry["id"], error=ERROR)
        return original(server)

    monkeypatch.setattr(app.client(), "pwd", approval_finishes)
    provider = ScriptedProvider([
        {"tool_calls": [ToolCall(call_id="read", name="remote_pwd", arguments={})]},
        {"text": "已确认用户批准过，提交返回错误。"},
    ])
    runtime = AgentRuntime(provider=provider, registry=app.build_registry(session_id="s-current"), mode=FULL)
    runtime.run("核对状态")

    first = provider.calls[0][0][0]["content"]
    second = provider.calls[1][0][0]["content"]
    assert '"awaiting_approval": true' in first
    assert '"submission_attempted": true' in second and ERROR in second
    assert '"awaiting_approval": true' not in second
    assert second.count("Current persisted job submission evidence") == 1


def test_unavailable_snapshot_is_reported_to_model(app_with_fake):
    app, _ = app_with_fake
    app.config.pending_submits_path.write_text("{broken", encoding="utf-8")
    provider = ScriptedProvider([{"text": "提交记录无法读取，尚不能判断原因。"}])
    runtime = AgentRuntime(provider=provider, registry=app.build_registry(session_id="s-current"), mode=FULL)
    assert runtime.chat("核对状态")["ok"]
    assert "Submission records are unavailable" in provider.calls[0][0][0]["content"]


@pytest.mark.parametrize("error", [ERROR, "connection closed after submission"])
def test_ui_failure_reaches_notification_status_tool_and_next_chat(ui, monkeypatch, error):
    session_id = call(ui, "chat.new", {})["session_id"]
    app = ui["app"]
    store = PendingSubmitStore(app.config.pending_submits_path)
    entry = store.create(server="cl9", directory=f"{ROOT}/case", script="run.sh",
                         session_id=session_id)

    def failed_submit(*args, **kwargs):
        raise RemoteError(error)

    monkeypatch.setattr(app.client(), "submit", failed_submit)
    result = call(ui, "submit.confirm", {"id": entry["id"], "approve": True})

    assert not result["ok"]
    assert error in result["error"]["message"]
    assert result["error"]["submission_id"] == entry["id"]
    loaded = call(ui, "chat.load", {"session_id": session_id})
    assert any(error in row["content"] and "已获人工确认" in row["content"]
               for row in loaded["messages"])
    status = app.build_registry(session_id=session_id).dispatch("job_submission_status", {})
    assert status["submissions"][0]["requires_remote_reconciliation"]
    assert not call(ui, "submit.confirm", {"id": entry["id"], "approve": True})["ok"]

    ui["scripted"].script[:] = [{"text": "已读取真实提交记录。"}]
    sse_chat(ui, {"provider": "mock", "session_id": session_id, "message": "为什么失败？"})
    model_system = ui["scripted"].calls[-1][0]["content"]
    assert error in model_system and '"approval_received": true' in model_system


@pytest.mark.parametrize("script", ["run.job.sh", "NaOH+opt.sh"])
def test_pbs_command_passes_a_script_filename_without_torque_double_dash_bug(monkeypatch, script):
    from rsci.gateway import rsci_gateway as gateway

    monkeypatch.setattr(gateway, "resolve_server", lambda server: (server, {}))
    monkeypatch.setattr(gateway, "validated_remote_path", lambda directory, entry, name: directory)
    monkeypatch.setattr(gateway, "require_resolved_within_root", lambda name, directory, entry: directory)
    monkeypatch.setattr(gateway, "scheduler_for", lambda name, entry: "pbs")
    monkeypatch.setattr(gateway, "audit", lambda *args: None)
    emitted = []
    monkeypatch.setattr(gateway, "out", lambda doc: emitted.append(doc) or 0)

    def torque_submit(server, command):
        tokens = shlex.split(command)
        qsub = tokens[tokens.index("qsub"):]
        # Torque 6.1's pre-getopt scanner consumes the token following `--`
        # as an option argument and falls back to empty stdin, losing #PBS.
        script_operand = None if qsub[1] == "--" else qsub[1]
        assert script_operand == "./" + script
        return "123.admin\n"

    monkeypatch.setattr(gateway, "remote", torque_submit)
    gateway.op_submit(SimpleNamespace(server="cl9", directory="/case", script=script, approval_ref=""))
    assert emitted[0]["job_id"] == "123"


@pytest.mark.parametrize("version, compatible", [("1.5.1", False), ("1.5.2", False), ("1.5.3", False), ("1.5.4", True)])
def test_gateway_probe_requires_the_pbs_submission_fix(version, compatible):
    from rsci.gateway.transport import SshTransport

    def runner(cmd, **kwargs):
        return 0, json.dumps({"ok": True, "gateway_version": version, "protocol": "2"}), ""

    transport = SshTransport(host="vlab.invalid", user="tester", runner=runner)
    assert transport._probe_gateway() is compatible
