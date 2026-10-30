"""Approval previews use frozen scripts, including after history refresh."""
from pathlib import Path
import shutil
import subprocess

import pytest

from rsci.core.hashing import text_sha256
from rsci.workflow.pending import PendingSubmitStore
from tests.conftest import ROOT
from tests.test_ui import ui, call


def test_pending_and_history_return_frozen_script_without_remote_reads(ui):
    script = '#!/bin/bash\n#PBS -q short\necho "<review this>"\n'
    store = PendingSubmitStore(ui["app"].config.pending_submits_path)
    entry = store.create(server="cl9", directory=ROOT, script="run.sh",
                         script_content=script, script_sha256=text_sha256(script),
                         session_id="script-review")
    ui["state"].files["cl9"][ROOT + "/run.sh"] = b"changed remote script"
    calls_before = len(ui["app"].transport().calls)
    for action in ("submit.pending", "submit.history"):
        rows = call(ui, action, {"session_id": "script-review"})[
            "pending" if action == "submit.pending" else "entries"]
        row = next(row for row in rows if row["id"] == entry["id"])
        assert row["script_content"] == script
        assert text_sha256(row["script_content"]) == row["script_sha256"]
    assert len(ui["app"].transport().calls) == calls_before
    refused = call(ui, "submit.confirm", {"id": entry["id"], "approve": True})
    assert not refused["ok"]  # displaying a snapshot does not bypass hash checks
    assert not any(command[0] == "submit" for command in ui["app"].transport().calls)


def test_script_preview_frontend_updates_existing_cards_and_escapes_html():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for browser tests")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / "tests/frontend/submit_script_preview.cjs"),
                             str(root / "src/rsci/ui/static/index.html")],
                            capture_output=True, text=True, encoding="utf-8", timeout=20,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stdout + result.stderr
