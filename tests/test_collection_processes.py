"""A real UI process and detached collection workers share a disk HPC."""
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from rsci.cli.main import App
from rsci.core.config import Config, ServerEntry
from rsci.hpc.center import RemoteCenter, project_store
from rsci.hpc.collections import Collections, launch
from tests.conftest import ROOT
from tests.task_process_support import DiskHpc, DiskTransport
from tests.test_task_processes import wait_for, kill_worker


@pytest.fixture
def disk_collection(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[1]
    state_path = tmp_path / "hpc.json"
    hpc = DiskHpc(state_path)
    hpc.initialize()
    monkeypatch.setenv("RSCI_TASK_TEST_STATE", str(state_path))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(map(str, (
        repo / "tests/task_process_bootstrap", repo / "src", repo))))
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    config = Config(tmp_path / "config")
    config.upsert_server(ServerEntry("cl9", "user@cl9", remote_root=ROOT))
    app = App(config)
    app._transport = DiskTransport()
    directory = ROOT + "/external"
    def seed(data):
        fake = data["state"]
        fake["dirs"]["cl9"].add(directory)
        fake["jobs"]["cl9"] = [{"job_id": "701", "state": "COMPLETED", "name": "external", "directory": directory}]
        fake["workdirs"] = {"701": directory}
        fake["files"]["cl9"][directory + "/a.csv"] = b"x,y\n1,2\n"
        fake["files"]["cl9"][directory + "/b.csv"] = b"x,y\n3,4\n"
    hpc.update(seed)
    project_store(config).create("remote", kind="general")
    center = RemoteCenter(config, app.client())
    center.refresh("cl9")
    center.associate("cl9", "701", "remote")
    queue = Collections(config, app.client())
    task = queue.enqueue("cl9", "701", [directory + "/a.csv", directory + "/b.csv"])
    processes = []
    def start_ui():
        discovery = config.home / "ui.json"
        discovery.unlink(missing_ok=True)
        with (tmp_path / f"ui-{len(processes)}.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "-m", "rsci", "--config-dir", str(config.home),
                                        "ui", "--no-open", "--port", "0"],
                stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        processes.append(process)
        info = wait_for(lambda: json.loads(discovery.read_text(encoding="utf-8")))
        return process, info
    yield queue, task, hpc, start_ui
    for gate in tmp_path.glob("gate-*"):
        gate.unlink(missing_ok=True)
    for process in processes:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
    from rsci.hpc.collections import _CHILDREN
    for process in list(_CHILDREN):
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


def test_close_ui_worker_finishes_then_reopen(disk_collection):
    queue, task, hpc, start_ui = disk_collection
    gate = hpc.root / "gate-before-download-b.csv"
    gate.touch()
    process, _ = start_ui()  # startup discovers and launches the queued task
    wait_for(lambda: (hpc.root / "entered-before-download-b.csv").exists())
    state = queue.load(task["task_id"])
    assert state["files"][0]["status"] == "completed"
    assert state["worker"]["pid"] != process.pid
    process.terminate()
    process.wait(timeout=10)
    gate.unlink()
    completed = wait_for(lambda: queue.load(task["task_id"]), lambda s: s["status"] == "completed")
    assert (Path(completed["result_directory"]) / "b.csv").read_bytes() == b"x,y\n3,4\n"
    _, info = start_ui()
    req = Request(info["url"] + "/api/collection.list", data=b"{}",
                  headers={"X-RSCI-Token": info["token"], "Content-Type": "application/json"})
    with urlopen(req, timeout=10) as response:
        assert json.load(response)["tasks"][0]["status"] == "completed"
    calls = hpc.load()["calls"]
    assert sum(c[0] == "download" for c in calls) == 2
    assert not any(c[0] in {"submit", "cancel"} for c in calls)


def test_killed_worker_and_two_recovery_launches_do_not_recopy(disk_collection):
    queue, task, hpc, _ = disk_collection
    gate = hpc.root / "gate-before-download-b.csv"
    gate.touch()
    launch(queue, task["task_id"])
    wait_for(lambda: (hpc.root / "entered-before-download-b.csv").exists())
    from rsci.hpc.collections import _CHILDREN
    # Windows venv's redirector can be the Popen PID while the worker records
    # the child interpreter PID. Kill the actual lock owner, then reap its launcher.
    owner = _CHILDREN[-1]
    kill_worker(queue.load(task["task_id"])["worker"]["pid"])
    owner.wait(timeout=10)
    gate.unlink()
    launch(queue, task["task_id"])
    launch(queue, task["task_id"])
    wait_for(lambda: queue.load(task["task_id"]), lambda s: s["status"] == "completed")
    calls = hpc.load()["calls"]
    assert sum(c[0] == "download" for c in calls) == 2
    assert not any(c[0] in {"submit", "cancel"} for c in calls)
