"""Actual detached processes, disk-backed scheduler, no real SSH or HPC writes."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from rsci.cli.main import App
from rsci.core.config import Config, ServerEntry
from rsci.desktop import _pid_alive
from tests.conftest import ROOT
from tests.task_process_support import DiskHpc, DiskTransport

PROJECT = Path(__file__).resolve().parents[1]


def wait_for(read, predicate=bool, timeout=20):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            last = read()
            if predicate(last):
                return last
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError(f"process condition timed out; last={last!r}")


def kill_worker(pid):
    # Only the PID recorded by a worker launched by this isolated fixture.
    if not _pid_alive(pid):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False,
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        import signal
        os.kill(pid, signal.SIGKILL)
    from rsci.workflow.worker import _CHILDREN
    for child in _CHILDREN:
        if getattr(child, "pid", None) == pid:
            child.wait(timeout=10)
    wait_for(lambda: not _pid_alive(pid))


@pytest.fixture()
def process_task(config_home, vasp_inputs, tmp_path, monkeypatch):
    state_path = tmp_path / "hpc.json"
    hpc = DiskHpc(state_path)
    hpc.initialize()
    monkeypatch.setenv("RSCI_TASK_TEST_STATE", str(state_path))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(map(str, (
        PROJECT / "tests/task_process_bootstrap", PROJECT / "src", PROJECT))))
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    config = Config(config_home)
    config.upsert_server(ServerEntry(name="cl9", target="user@cl9", remote_root=ROOT,
                                     port=22, scheduler="slurm"))
    config.set_default_server("cl9")
    app = App(config)
    app._transport = DiskTransport()
    prepared = app.engine().prepare({"from_dir": str(vasp_inputs), "server": "cl9",
                                    "remote_dir": ROOT + "/runs/process"})
    plan_id = prepared["plan_id"]
    ref = app.engine().approve(plan_id, confirmed=True)["approval_ref"]
    processes, workers = [], set()
    def start_ui():
        discovery = config.home / "ui.json"
        discovery.unlink(missing_ok=True)
        log = (tmp_path / f"ui-{len(processes)}.log").open("w", encoding="utf-8")
        process = subprocess.Popen([sys.executable, "-m", "rsci", "--config-dir", str(config.home),
                                    "ui", "--no-open", "--port", "0"],
                                   stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log.close()
        processes.append(process)
        info = wait_for(lambda: json.loads(discovery.read_text(encoding="utf-8")))
        return process, info
    def call(info, action, body=None):
        req = urllib.request.Request(info["url"] + "/api/" + action,
            data=json.dumps(body or {}).encode(),
            headers={"X-RSCI-Token": info["token"], "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.load(response)
    def snapshot():
        state = app.engine()._load_run(plan_id)
        pid = (state or {}).get("task", {}).get("worker", {}).get("pid")
        if pid:
            workers.add(pid)
        return state
    def complete():
        def change(data):
            for job in data["state"]["jobs"]["cl9"]:
                job["state"] = "COMPLETED"
        hpc.update(change)
    yield {"app": app, "plan_id": plan_id, "ref": ref, "hpc": hpc,
           "start_ui": start_ui, "call": call, "snapshot": snapshot, "complete": complete}
    snapshot()
    for process in processes:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
    for pid in workers:
        kill_worker(pid)


def test_close_ui_worker_collects_without_ui_and_reopen_shows_results(process_task):
    f = process_task
    process, info = f["start_ui"]()
    started = f["call"](info, "workflow.run", {"plan_id": f["plan_id"],
                     "approval_ref": f["ref"], "poll_seconds": 0.1})
    assert started["ok"], started
    state = wait_for(f["snapshot"], lambda s: s and s["task"]["phase"] == "monitoring")
    assert state["task"]["worker"]["pid"] != process.pid
    process.terminate()
    process.wait(10)
    assert _pid_alive(state["task"]["worker"]["pid"])
    f["complete"]()
    result = wait_for(f["snapshot"], lambda s: s["status"] == "completed")
    assert (Path(result["task"]["download_root"]) / "OUTCAR").is_file()
    _, reopened = f["start_ui"]()
    shown = f["call"](reopened, "workflow.status", {"plan_id": f["plan_id"]})
    assert shown["status"] == "completed" and shown["scientific_converged"]
    assert len([c for c in f["hpc"].load()["calls"] if c[0] == "submit"]) == 1


@pytest.mark.parametrize("stage", ["preparing", "monitoring", "downloading", "accepted_submit"])
def test_killed_worker_recovers_on_ui_start_without_duplicate_submit(process_task, stage):
    f = process_task
    gate_op = {"preparing": "mkdir", "downloading": "scp-download", "accepted_submit": "submit"}.get(stage)
    when = "after" if stage == "accepted_submit" else "before"
    gate = f["hpc"].root / f"gate-{when}-{gate_op}" if gate_op else None
    if gate:
        gate.touch()
    f["app"].engine().background(f["plan_id"], f["ref"], poll_seconds=0.1)
    state = wait_for(f["snapshot"], lambda s: s and s["task"].get("worker", {}).get("pid"))
    if stage == "downloading":
        wait_for(f["snapshot"], lambda s: bool(s.get("job_id")))
        f["complete"]()
    if gate:
        wait_for(lambda: (f["hpc"].root / f"entered-{when}-{gate_op}").exists())
    else:
        wait_for(f["snapshot"], lambda s: s["task"]["phase"] == "monitoring")
    kill_worker(state["task"]["worker"]["pid"])
    if gate:
        gate.unlink()
    _, info = f["start_ui"]()
    if stage == "accepted_submit":
        blocked = wait_for(f["snapshot"], lambda s: s["task"]["phase"] == "blocked")
        assert blocked["task"]["blocked_reason"] == "submit_uncertain"
        known = f["hpc"].load()["state"]["jobs"]["cl9"][0]["job_id"]
        bound = f["call"](info, "workflow.bind_job", {"plan_id": f["plan_id"], "job_id": known,
                         "phrase": f"bind {f['plan_id']} {known}"})
        assert bound["ok"], bound
    wait_for(f["snapshot"], lambda s: bool(s.get("job_id")))
    f["complete"]()
    result = wait_for(f["snapshot"], lambda s: s["status"] == "completed")
    assert result["scientific_converged"]
    assert len([c for c in f["hpc"].load()["calls"] if c[0] == "submit"]) == 1


def test_two_real_worker_processes_contend_for_one_task(process_task):
    from rsci.workflow.worker import command
    f = process_task
    engine = f["app"].engine()
    engine.tasks.enqueue(f["plan_id"], f["ref"], poll_seconds=0.1)
    children = [subprocess.Popen(command(engine, f["plan_id"]), stdin=subprocess.DEVNULL,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) for _ in range(2)]
    try:
        wait_for(f["snapshot"], lambda s: s and s.get("job_id"))
        f["complete"]()
        wait_for(f["snapshot"], lambda s: s["status"] == "completed")
        for child in children:
            child.wait(timeout=10)
        assert len([c for c in f["hpc"].load()["calls"] if c[0] == "submit"]) == 1
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)
