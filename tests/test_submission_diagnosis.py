"""Repair advice follows the deployed path, not an old error or a global default."""

import json
import subprocess
from copy import deepcopy
from types import SimpleNamespace

import pytest

from rsci.core.errors import RemoteError
from rsci.gateway.transport import SshTransport
from rsci.hpc.submission import analyze_submission
from rsci.workflow.pending import PendingSubmitStore
from tests.conftest import ROOT
from tests.test_ui import call, ui


ERROR = "qsub: submit error (No default queue specified MSG=requested queue not found)"
QUEUES = ("Queue              Max Tot Ena Str Que Run Hld Wat Trn Ext T Cpt\n"
          "----------------   --- --- --- --- --- --- --- --- --- --- - ---\n"
          "short               20   5 yes yes   2   1   2   0   0   0 E   0\n")


@pytest.fixture
def current():
    return {"available": True, "gateway_version": "1.5.3", "scheduler": "pbs",
            "kind": "current_path_inspection", "submission_performed": False,
            "command": "cd -- /case && qsub ./run.job.sh",
            "scheduler_version": "Version: 6.1.0", "default_queue": "batch",
            "directive_prefix": "#PBS",
            "script_directives": ["#PBS -N NaOH", "#PBS -q short"],
            "script_directives_complete": True, "queue_list": QUEUES,
            "queue_list_truncated": False}


def test_old_error_with_current_fixed_path_does_not_require_admin_changes(current):
    old = {"id": "ps-old1", "error": ERROR, "status": "uncertain"}
    result = analyze_submission(old, current)
    codes = {item["code"] for item in result["findings"]}
    assert {"historical_execution_incomplete", "pbs_path_operand_present",
            "explicit_queue_available", "invalid_default_not_a_prerequisite"} <= codes
    assert result["historical_execution_gaps"] == ["command", "gateway_version", "returncode"]
    assert result["repair_plan"][0]["target"] == "verification"
    assert result["can_auto_resubmit"] is False
    assert all(not item["requires_cluster_admin"] for item in result["repair_plan"])
    assert "execution" not in old  # current evidence must not backfill historical facts


def test_recorded_command_and_current_command_remain_separate(current):
    old = {"error": ERROR, "execution": {"command": "qsub -- run.job.sh",
           "gateway_version": "1.5.1", "returncode": 191}}
    result = analyze_submission(old, current)
    assert result["attempt"]["execution"]["command"] == "qsub -- run.job.sh"
    assert result["current"]["command"].endswith("qsub ./run.job.sh")
    assert not result["historical_execution_gaps"]
    assert all(item["code"] != "pbs_double_dash_operand" for item in result["findings"])


def test_actual_bad_command_targets_application_adapter(current):
    current["command"] = "cd -- /case && qsub -- run.job.sh"
    result = analyze_submission({"error": ERROR}, current)
    finding = next(item for item in result["findings"] if item["code"] == "pbs_double_dash_operand")
    assert finding["scope"] == "current"
    assert result["repair_plan"][0]["target"] == "application_gateway"
    assert "./<script>" in result["repair_plan"][0]["action"]


@pytest.mark.parametrize("directives,queues", [
    (["#PBS -q missing"], QUEUES),
    (["#PBS -q short"], QUEUES.replace("yes yes", "no yes")),
    ([], QUEUES),
])
def test_invalid_or_missing_explicit_queue_targets_script(current, directives, queues):
    current.update(script_directives=directives, queue_list=queues)
    result = analyze_submission(None, current)
    assert result["repair_plan"][0]["target"] == "job_script"
    assert not result["repair_plan"][0]["requires_cluster_admin"]


@pytest.mark.parametrize("missing", ["queue_list", "truncated_queues", "incomplete_header"])
def test_incomplete_observations_do_not_invent_a_required_queue_fix(current, missing):
    if missing == "queue_list":
        current.pop("queue_list")
        current["queue_list_error"] = "permission denied"
    elif missing == "truncated_queues":
        current["queue_list_truncated"] = True
    else:
        current.update(script_directives=[], script_directives_complete=False)
    result = analyze_submission({"error": ERROR}, current)
    assert all(item["target"] != "job_script" for item in result["repair_plan"])


def test_slurm_end_of_options_is_not_reported_as_a_torque_bug(current):
    current.update(scheduler="slurm", command="cd -- /case && sbatch --parsable -- run.job.sh",
                   script_directives=["#SBATCH --partition=cpu"], queue_list="cpu*|up|01:00:00\n")
    result = analyze_submission(None, current)
    assert result["current_explicit_queue_enabled"] is True
    assert all(item["code"] != "pbs_double_dash_operand" for item in result["findings"])


def test_old_gateway_without_inspection_does_not_trigger_cluster_admin_advice():
    result = analyze_submission({"error": ERROR}, {
        "available": False, "reason": "inspection_unsupported", "gateway_version": "1.5.2"})
    assert result["repair_plan"][0]["target"] == "application_gateway"
    assert "inspect again" in result["repair_plan"][0]["action"]
    assert not result["repair_plan"][0]["requires_cluster_admin"]


@pytest.fixture
def gateway(monkeypatch):
    from rsci.gateway import rsci_gateway
    monkeypatch.setattr(rsci_gateway, "submission_target", lambda args: (
        "cl9", "/case", "pbs", "cd -- /case && qsub ./run.job.sh"))
    monkeypatch.setattr(rsci_gateway, "audit", lambda *args: None)
    return rsci_gateway


def test_gateway_inspection_never_submits_and_stops_reading_directives_at_code(gateway, monkeypatch):
    commands, documents = [], []
    monkeypatch.setattr(gateway, "out", lambda doc: documents.append(doc) or 0)

    def probe(server, command, **kwargs):
        commands.append(command)
        if command == "qstat -Q":
            return QUEUES
        if command == "qstat -Bf":
            return "    default_queue = batch\n"
        if command.startswith("printf "):
            return "#PBS"
        if command.startswith("head "):
            return "#!/bin/bash\n#PBS -q short\ncd /case\n#PBS -q ignored\n"
        return "Version: 6.1.0\n"

    monkeypatch.setattr(gateway, "remote", probe)
    gateway.op_submit_inspect(SimpleNamespace(script="run.job.sh"))
    document = documents[0]
    assert document["submission_performed"] is False
    assert document["script_directives"] == ["#PBS -q short"]
    assert document["default_queue"] == "batch"
    assert not any("&& qsub" in command or "qmgr" in command for command in commands)


def test_gateway_failure_preserves_command_version_exit_code_across_protocol(gateway, monkeypatch):
    def rejected(*args, **kwargs):
        raise gateway.GatewayError("remote_command_failed", ERROR,
            detail={"remote_result": {"returncode": 191, "stderr": ERROR, "stdout": ""}})

    monkeypatch.setattr(gateway, "remote", rejected)
    with pytest.raises(gateway.GatewayError) as caught:
        gateway.op_submit(SimpleNamespace(script="run.job.sh", approval_ref=""))
    documents = []
    monkeypatch.setattr(gateway, "out", lambda doc: documents.append(doc) or 0)
    gateway.fail(caught.value.code, caught.value.message, **caught.value.detail)
    with pytest.raises(RemoteError) as parsed:
        SshTransport.parse_gateway_document(json.dumps(documents[0]))
    evidence = parsed.value.detail["submission"]
    assert evidence["command"] == "cd -- /case && qsub ./run.job.sh"
    assert evidence["gateway_version"] == "1.5.4"
    assert evidence["returncode"] == 191 and evidence["stderr"] == ERROR
    assert evidence["started_at"] and evidence["finished_at"]


def test_gateway_timeout_keeps_acceptance_unknown(gateway, monkeypatch):
    def lost(*args, **kwargs):
        raise subprocess.TimeoutExpired("ssh", 30)
    monkeypatch.setattr(gateway, "remote", lost)
    with pytest.raises(gateway.GatewayError) as caught:
        gateway.op_submit(SimpleNamespace(script="run.job.sh", approval_ref=""))
    assert caught.value.code == "timeout"
    assert "returncode" not in caught.value.detail["submission"]


@pytest.mark.parametrize("supported", [False, True])
def test_client_inspection_does_not_deploy_reconnect_or_submit(app_with_fake, monkeypatch, current, supported):
    app, _ = app_with_fake
    operations = []

    class ReadOnlyTransport:
        def run_gateway(self, args, **kwargs):
            operations.append((args, kwargs))
            assert kwargs["prepare"] is False
            assert args[0] in {"version", "submit-inspect"}
            if args[0] == "version":
                return {"ok": True, "gateway_version": "1.5.3" if supported else "1.5.2",
                        "submission_inspection": supported}
            return current

    monkeypatch.setattr(app.client(), "_transport_for", lambda server: ReadOnlyTransport())
    result = app.client().inspect_submission(f"{ROOT}/case", "run.job.sh", server="cl9")
    assert result["available"] is supported
    assert len(operations) == (2 if supported else 1)


def test_transport_readonly_mode_skips_gateway_installation(monkeypatch):
    transport = SshTransport(host="vlab.invalid", user="tester",
        runner=lambda *args, **kwargs: (0, '{"ok":true,"gateway_version":"1.5.2"}', ""))
    monkeypatch.setattr(transport, "_ensure_gateway", lambda **kwargs: pytest.fail("must not install"))
    assert transport.run_gateway(["version"], prepare=False)["gateway_version"] == "1.5.2"


def test_ui_persists_execution_evidence_for_future_diagnosis(ui, monkeypatch, current):
    session = call(ui, "chat.new", {})["session_id"]
    app = ui["app"]
    store = PendingSubmitStore(app.config.pending_submits_path)
    entry = store.create(server="cl9", directory=f"{ROOT}/case", script="run.job.sh", session_id=session)
    execution = {"command": "qsub -- run.job.sh", "gateway_version": "1.5.1",
                 "returncode": 191, "stderr": ERROR, "stage": "scheduler_submit"}

    def failed(*args, **kwargs):
        raise RemoteError(ERROR, detail={"submission": execution})

    monkeypatch.setattr(app.client(), "submit", failed)
    assert not call(ui, "submit.confirm", {"id": entry["id"], "approve": True})["ok"]
    monkeypatch.setattr(app.client(), "inspect_submission", lambda *args, **kwargs: deepcopy(current))
    registry = app.build_registry(session_id=session)
    result = registry.dispatch("job_submission_diagnose", {"id": entry["id"]}, provider_mode="analysis_only")
    assert result["attempt"]["execution"] == execution
    assert result["current"]["gateway_version"] == "1.5.3"
    assert result["repair_plan"][0]["target"] == "verification"
    assert not result["can_auto_resubmit"]
    assert "qsub -- run.job.sh" in registry.submission_context()


def test_custom_directive_prefix_does_not_blame_missing_queue_directive(current):
    current["directive_prefix"] = "#CUSTOM"
    result = analyze_submission({"error": ERROR}, current)
    assert result["current_explicit_queue"] is None
    assert result["repair_plan"][0]["target"] == "submission_environment"
    assert all(item["target"] != "job_script" for item in result["repair_plan"])
