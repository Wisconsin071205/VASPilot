"""Short accepted jobs stay visible even when PBS forgets before the first poll."""
from pathlib import Path
import shutil
import subprocess

import pytest

from rsci.core.config import ServerEntry
from rsci.core.errors import ValidationError
from rsci.hpc.center import RemoteCenter
from rsci.workflow.pending import PendingSubmitStore
from tests.conftest import ROOT
from tests.test_ui import ui, call


def accepted(app, result):
    store = PendingSubmitStore(app.config.pending_submits_path)
    card = store.create(server="cl9", directory=ROOT, script="run.sh")
    store.claim(card["id"])
    store.finish_submission(card["id"], result=result)
    return store


def test_short_job_survives_before_first_poll_and_restart(app_with_fake):
    app, transport = app_with_fake
    result = app.client().submit(ROOT, "run.sh", server="cl9")
    center = RemoteCenter(app.config, app.client())
    row = center.overview("cl9", refresh=False)["jobs"][0]
    assert row["job_id"] == result["job_id"]
    assert row["directory"] == ROOT
    transport.state.jobs["cl9"] = []
    row = center.refresh("cl9")["jobs"][0]
    assert row["state"] == "UNKNOWN"  # disappearance cannot prove completion
    assert row["last_known_state"] == "SUBMITTED"
    assert RemoteCenter(app.config, app.client()).known("cl9", result["job_id"])


def test_failed_local_save_recovers_from_approval_receipt(app_with_fake, monkeypatch):
    app, transport = app_with_fake
    with monkeypatch.context() as patch:
        patch.setattr(RemoteCenter, "record_submission", lambda *args: (_ for _ in ()).throw(OSError("disk unavailable")))
        result = app.client().submit(ROOT, "run.sh", server="cl9")
    assert result["ok"] and result["job_id"] and result["record_warnings"]
    accepted(app, result)
    transport.state.jobs["cl9"] = []
    center = RemoteCenter(app.config, app.client())
    assert center.overview("cl9", refresh=False)["jobs"][0]["job_id"] == result["job_id"]
    assert center.refresh("cl9")["jobs"][0]["state"] == "UNKNOWN"
    assert transport.state.job_seq == 1001  # recovery never resubmits


def test_receipts_are_identity_bound_and_do_not_regress_completion(app_with_fake):
    app, transport = app_with_fake
    result = app.client().submit(ROOT, "run.sh", server="cl9")
    accepted(app, result)
    center = RemoteCenter(app.config, app.client())
    transport.state.jobs["cl9"] = [{"job_id": result["job_id"], "state": "COMPLETED"}]
    center.refresh("cl9")
    center.record_submission("cl9", result)
    assert center.known("cl9", result["job_id"])["state"] == "COMPLETED"
    app.config.upsert_server(ServerEntry("cl9", "different@host", remote_root=ROOT))
    assert center.overview("cl9", refresh=False)["jobs"] == []
    with pytest.raises(ValidationError, match="身份发生变化"):
        center.record_submission("cl9", result)


def test_legacy_receipt_is_not_attached_to_current_account_without_evidence(app_with_fake):
    app, _ = app_with_fake
    accepted(app, {"ok": True, "job_id": "129371", "directory": ROOT})
    assert RemoteCenter(app.config, app.client()).overview("cl9", refresh=False)["jobs"] == []


def test_approval_flow_registers_and_returns_disappeared_job(ui):
    app, fake = ui["app"], ui["state"]
    # Exercise the real submit client used by confirmation and both UI views.
    result = app.client().submit(ROOT, "run.sh", server="cl9")
    fake.jobs["cl9"] = []
    assert call(ui, "job.center", {"server": "cl9"})["jobs"][0]["job_id"] == result["job_id"]
    recent = call(ui, "job.recent", {"server": "cl9"})
    assert recent["jobs"][0]["state"] == "UNKNOWN"
    assert not recent["active_job_ids"]


def test_history_frontend_retains_unknown_without_claiming_completed():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required")
    root = Path(__file__).resolve().parents[1]
    run = subprocess.run([node, str(root / "tests/frontend/submission_history.cjs"),
                          str(root / "src/rsci/ui/static/index.html")],
                         capture_output=True, text=True, encoding="utf-8", timeout=20,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert run.returncode == 0, run.stdout + run.stderr
