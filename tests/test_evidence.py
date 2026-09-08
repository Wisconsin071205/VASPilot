from types import SimpleNamespace

import pytest

from vasploom.hpc.evidence import organize_evidence, evidence_markdown
from vasploom.gateway import vasploom_gateway as gateway


def snapshot():
    return {"scheduler": "slurm", "sources": {
        "sacct": {"status": "available", "raw":
            "42|case|teaching|cpu|COMPLETED|2026-09-01T10:00:00|2026-09-01T10:05:00|2026-09-01T10:15:00|00:10:00|8|2Gc|||0:0|n1\n"
            "42.batch|batch|||COMPLETED||||00:10:00|||512M|1G|0:0|n1\n"
            "420|other|other|cpu|FAILED||||||||||\n"},
        "squeue": {"status": "empty", "raw": ""},
        "sstat": {"status": "empty", "raw": ""}}}


def organize(data):
    return organize_evidence(data, job_id="42", server="cl9", directory="/runs/case",
                             logs=[{"path": "/runs/case/slurm-42.out", "content": "done\n```"}], warnings=[])


def test_completed_evidence_keeps_step_memory_and_wait():
    result = organize(snapshot())
    assert result["summary"]["queue_wait_seconds"] == 300
    assert result["summary"]["elapsed"] == "00:10:00"
    assert result["summary"]["requested_memory"] == "2Gc"
    assert result["records"]["sacct"][1]["max_rss"] == "512M"
    assert len(result["records"]["sacct"]) == 2
    assert not result["warnings"]
    assert "````text" in evidence_markdown(result)


def test_pending_start_prediction_is_not_actual_wait():
    data = snapshot()
    data["sources"]["sacct"]["raw"] = ""
    data["sources"]["squeue"]["raw"] = "42|PENDING|0:00|1:00:00|cpu|Resources|2026-09-01T10:00:00|2026-09-01T11:00:00"
    result = organize(data)
    assert result["summary"]["state"] == "PENDING"
    assert result["summary"]["queue_wait_seconds"] is None
    assert result["summary"]["queue_reason_or_nodes"] == "Resources"


def test_unavailable_is_not_zero():
    result = organize({"warnings": ["sacct unavailable"]})
    assert result["summary"]["elapsed"] is None
    assert result["summary"]["max_rss"] is None
    assert "sacct unavailable" in result["warnings"]
    assert any("不足以证明" in warning for warning in result["warnings"])


def test_gateway_independent_failures_and_step_query(monkeypatch):
    commands, output = [], []
    monkeypatch.setattr(gateway, "resolve_server", lambda _: ("cl9", {}))
    monkeypatch.setattr(gateway, "scheduler_for", lambda *_: "slurm")
    monkeypatch.setattr(gateway, "audit", lambda *_: None)
    monkeypatch.setattr(gateway, "out", output.append)
    def remote(server, command, **kwargs):
        commands.append(command)
        if command.startswith("sacct"):
            raise RuntimeError("accounting offline")
        return "42|RUNNING|0:10|1:00:00|cpu|n1|Unknown|Unknown"
    monkeypatch.setattr(gateway, "remote", remote)
    assert gateway.op_job_evidence(SimpleNamespace(server="cl9", job_id="42")) == 0
    assert len(commands) == 3
    assert " -X" not in commands[1]
    assert output[0]["sources"]["sacct"]["status"] == "unavailable"
    assert output[0]["sources"]["squeue"]["status"] == "available"


@pytest.mark.parametrize("job_id", ["42;whoami", "$(id)", "42,43"])
def test_gateway_rejects_injection(monkeypatch, job_id):
    monkeypatch.setattr(gateway, "resolve_server", lambda _: ("cl9", {}))
    monkeypatch.setattr(gateway, "fail", lambda *_: 2)
    monkeypatch.setattr(gateway, "remote", lambda *_: pytest.fail("must not query"))
    assert gateway.op_job_evidence(SimpleNamespace(server="cl9", job_id=job_id)) == 2
