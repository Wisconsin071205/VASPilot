"""Safety and recovery tests for durable, ledger-bound single-stage tasks."""
from __future__ import annotations

import json
import threading
from datetime import timedelta

import pytest

from rsci.core.errors import ApprovalError, AuthRequiredError, RemoteError, ValidationError
from rsci.core.locking import try_file_lock
from rsci.workflow import tasks, worker
from tests.conftest import ROOT


@pytest.fixture()
def task(app_with_fake, vasp_inputs):
    app, transport = app_with_fake
    engine = app.engine()
    prepared = engine.prepare({"from_dir": str(vasp_inputs), "server": "cl9",
                               "remote_dir": f"{ROOT}/runs/durable"})
    plan_id = prepared["plan_id"]
    approved = engine.approve(plan_id, confirmed=True)
    remote = prepared["plan"]["remote_dir"]
    transport.state.files["cl9"][f"{remote}/OSZICAR"] = b"   1 F= -.10000000E+02  E0= -.10000000E+02\n"
    transport.state.files["cl9"][f"{remote}/OUTCAR"] = b"reached required accuracy\n"
    return engine, plan_id, approved["approval_ref"], transport


def submit_count(transport):
    return sum(c[0] == "submit" for c in transport.calls)


def enqueue(task):
    engine, plan_id, ref, _ = task
    return engine.tasks.enqueue(plan_id, ref, poll_seconds=0)


def interrupt_monitor(task, monkeypatch):
    engine, plan_id, _, _ = task
    enqueue(task)
    def stop(*a, **kw):
        raise KeyboardInterrupt()
    with monkeypatch.context() as patch:
        patch.setattr(engine.client, "job_state", stop)
        with pytest.raises(KeyboardInterrupt):
            engine.tasks.execute(plan_id)
    return engine._load_run(plan_id)


def uncertain_submit(task, monkeypatch):
    engine, plan_id, _, transport = task
    enqueue(task)
    original = engine.client.submit
    def lost(*a, **kw):
        original(*a, **kw)
        raise TimeoutError("response lost after scheduler acceptance")
    with monkeypatch.context() as patch:
        patch.setattr(engine.client, "submit", lost)
        state = engine.tasks.execute(plan_id)
    assert state["task"]["blocked_reason"] == "submit_uncertain"
    known = transport.state.jobs["cl9"][-1]["job_id"]
    transport.state.workdirs = {known: engine.load_plan(plan_id)["remote_dir"]}
    return known


def test_no_reusable_approval_or_receipt_in_public_state(task):
    engine, plan_id, ref, _ = task
    enqueue(task)
    text = engine.run_path(plan_id).read_text(encoding="utf-8")
    assert ref not in text
    assert "execution_receipt" in text
    assert "execution_receipt" not in engine.status(plan_id)
    assert ref not in json.dumps(engine.list_plans())
    assert all(ref not in a for a in worker.command(engine, plan_id))


@pytest.mark.parametrize("after_ledger", [False, True])
def test_admission_crash_recovers_without_original_token(task, monkeypatch, after_ledger):
    engine, plan_id, ref, transport = task
    if after_ledger:
        original = engine._save_run
        def save(state):
            if state["task"]["phase"] == "queued":
                raise KeyboardInterrupt()
            original(state)
        target, name, replacement = engine, "_save_run", save
    else:
        def consume(*args):
            raise KeyboardInterrupt()
        target, name, replacement = engine, "_mark_consumed", consume
    with monkeypatch.context() as patch:
        patch.setattr(target, name, replacement)
        with pytest.raises(KeyboardInterrupt):
            engine.tasks.enqueue(plan_id, ref, poll_seconds=0)
    assert engine._load_run(plan_id)["task"]["phase"] == "admitting"
    assert engine.tasks.execute(plan_id)["status"] == "completed"
    assert submit_count(transport) == 1


def test_duplicate_admission_is_idempotent_but_deleted_record_cannot_replay(task):
    engine, plan_id, ref, transport = task
    first = enqueue(task)
    assert enqueue(task)["run_instance"] == first["run_instance"]
    engine.tasks.execute(plan_id)
    engine.run_path(plan_id).unlink()
    with pytest.raises(ApprovalError, match="already used"):
        engine.tasks.enqueue(plan_id, ref)
    assert not engine.run_path(plan_id).exists()
    assert submit_count(transport) == 1


def test_lock_not_stale_heartbeat_controls_ownership(task, monkeypatch):
    engine, plan_id, ref, transport = task
    enqueue(task)
    entered, release = threading.Event(), threading.Event()
    original = engine.client.job_state
    def held(*a, **kw):
        entered.set()
        assert release.wait(5)
        return original(*a, **kw)
    monkeypatch.setattr(engine.client, "job_state", held)
    thread = threading.Thread(target=engine.tasks.execute, args=(plan_id,))
    thread.start()
    try:
        assert entered.wait(5)
        assert engine.status(plan_id)["task"]["worker_alive"]
        with pytest.raises(ValidationError, match="already executing"):
            engine.tasks.enqueue(plan_id, ref, force=True)
        with pytest.raises(ValidationError, match="already executing"):
            engine.tasks.execute(plan_id)
    finally:
        release.set()
        thread.join(5)
    assert submit_count(transport) == 1


def test_known_job_resumes_context_and_collects_after_expiry(task, monkeypatch):
    engine, plan_id, _, transport = task
    state = interrupt_monitor(task, monkeypatch)
    original_now = tasks.now()
    monkeypatch.setattr(tasks, "now", lambda: original_now + timedelta(hours=25))
    result = engine.resume(plan_id)
    assert result["status"] == "completed"
    assert result["scheduler_state"] == "COMPLETED"
    assert result["scientific_converged"] is True
    assert result["collection_status"] == "completed"
    assert result["job_id"] == state["job_id"]
    assert submit_count(transport) == 1


def test_expired_presubmit_waits_for_new_approval(task, monkeypatch):
    engine, plan_id, _, transport = task
    enqueue(task)
    original_now = tasks.now()
    monkeypatch.setattr(tasks, "now", lambda: original_now + timedelta(hours=25))
    with pytest.raises(ApprovalError):
        engine.resume(plan_id)
    assert engine.status(plan_id)["task"]["blocked_reason"] == "approval_required"
    assert submit_count(transport) == 0
    ref = engine.approve(plan_id, confirmed=True, validity_hours=72)["approval_ref"]
    assert engine.resume(plan_id, ref)["status"] == "completed"
    assert submit_count(transport) == 1


@pytest.mark.parametrize("force", [False, True])
def test_lost_submit_response_never_retries(task, monkeypatch, force):
    engine, plan_id, ref, transport = task
    uncertain_submit(task, monkeypatch)
    with pytest.raises(ValidationError, match="提交结果待核对"):
        engine.tasks.enqueue(plan_id, ref, resume=not force, force=force)
    assert engine.tasks.execute(plan_id)["status"] == "needs_review"
    assert submit_count(transport) == 1


def test_reconcile_read_only_and_confirmed_bind_resumes(task, monkeypatch):
    engine, plan_id, _, transport = task
    known = uncertain_submit(task, monkeypatch)
    before = engine.run_path(plan_id).read_bytes()
    report = engine.tasks.reconcile(plan_id)
    assert report["read_only"] and report["candidates"][0]["job_id"] == known
    assert engine.run_path(plan_id).read_bytes() == before
    with pytest.raises(ValidationError, match="确认短语"):
        engine.tasks.bind_job(plan_id, known, "wrong")
    engine.tasks.bind_job(plan_id, known, f"bind {plan_id} {known}")
    assert engine.resume(plan_id)["status"] == "completed"
    with pytest.raises(ValidationError, match="关联一次"):
        engine.tasks.bind_job(plan_id, known, f"bind {plan_id} {known}")
    assert submit_count(transport) == 1


@pytest.mark.parametrize("unknown", [False, True])
def test_binding_requires_matching_directory_and_scheduler_evidence(task, monkeypatch, unknown):
    engine, plan_id, _, _ = task
    known = uncertain_submit(task, monkeypatch)
    if unknown:
        monkeypatch.setattr(engine.client, "job_state", lambda *a, **kw: {"state": "UNKNOWN"})
    else:
        monkeypatch.setattr(engine.client, "job_workdir", lambda *a, **kw: {"workdir": ROOT + "/wrong"})
    with pytest.raises(ValidationError, match="证据不足"):
        engine.tasks.bind_job(plan_id, known, f"bind {plan_id} {known}")
    assert engine.status(plan_id).get("job_id") is None


def test_unknown_never_completes_and_deadline_survives_restart(task, monkeypatch):
    engine, plan_id, _, transport = task
    state = interrupt_monitor(task, monkeypatch)
    deadline = state["task"]["monitor_deadline_at"]
    monkeypatch.setattr(tasks, "now", lambda: tasks.parse_time(deadline) + timedelta(seconds=1))
    monkeypatch.setattr(engine.client, "job_state", lambda *a, **kw: {"state": "UNKNOWN"})
    result = engine.resume(plan_id)
    assert result["status"] == "paused"
    assert result["task"]["blocked_reason"] == "monitor_timeout"
    assert result["task"]["monitor_deadline_at"] == deadline
    assert submit_count(transport) == 1


def test_manual_extension_only_monitors_same_job(task, monkeypatch):
    engine, plan_id, _, transport = task
    state = interrupt_monitor(task, monkeypatch)
    state["task"]["monitor_deadline_at"] = (tasks.now() - timedelta(seconds=1)).isoformat()
    engine._save_run(state)
    old_deadline = state["task"]["monitor_deadline_at"]
    result = engine.resume(plan_id, extend_monitor=True)
    assert result["status"] == "completed"
    assert result["task"]["monitor_deadline_at"] != old_deadline
    assert submit_count(transport) == 1


def test_auth_failure_pauses_without_opening_login(task, monkeypatch):
    engine, plan_id, _, _ = task
    enqueue(task)
    def denied(*a, **kw):
        raise AuthRequiredError("login needed")
    monkeypatch.setattr(engine.client, "mkdir", denied)
    monkeypatch.setattr(engine.client, "open_login_terminal", lambda *a, **kw: pytest.fail("no login in worker"))
    result = engine.tasks.execute(plan_id)
    assert result["task"]["blocked_reason"] == "auth_required"


def test_network_retries_persist_before_waiting(task, monkeypatch):
    engine, plan_id, _, transport = task
    enqueue(task)
    original, calls, delays = engine.client.job_state, [0], []
    def intermittent(*a, **kw):
        calls[0] += 1
        if calls[0] < 7:
            raise RemoteError("temporary connection reset")
        return original(*a, **kw)
    def sleep(delay):
        if delay:
            saved = engine._load_run(plan_id)
            assert saved["task"]["phase"] == "retrying"
            assert saved["task"]["next_retry_at"]
            delays.append(delay)
    monkeypatch.setattr(engine.client, "job_state", intermittent)
    monkeypatch.setattr(tasks.time, "sleep", sleep)
    assert engine.tasks.execute(plan_id)["status"] == "completed"
    assert delays == [5, 10, 20, 40, 60, 60]
    assert submit_count(transport) == 1


def test_download_retry_does_not_resubmit_or_reupload(task, monkeypatch):
    engine, plan_id, _, transport = task
    enqueue(task)
    original = engine.client.download
    def broken(*a, **kw):
        raise ValidationError("local output is stale")
    with monkeypatch.context() as patch:
        patch.setattr(engine.client, "download", broken)
        assert engine.tasks.execute(plan_id)["task"]["phase"] == "blocked"
    uploads = sum(c[0] == "upload" for c in transport.calls)
    assert engine.resume(plan_id)["status"] == "completed"
    assert sum(c[0] == "upload" for c in transport.calls) == uploads
    assert submit_count(transport) == 1


@pytest.mark.parametrize("tamper", ["receipt", "target", "input", "root"])
def test_recovery_refuses_drift_before_remote_effects(task, monkeypatch, tamper):
    engine, plan_id, _, transport = task
    state = enqueue(task)
    if tamper == "receipt":
        state["execution_receipt"]["signature"] = "0" * 64
        engine._save_run(state)
    elif tamper == "root":
        state["task"]["download_root"] += "-other"
        engine._save_run(state)
    elif tamper == "target":
        entry = engine.client.server_entry("cl9")
        entry.target = "someone@elsewhere"
        engine.config.upsert_server(entry)
    else:
        plan = engine.load_plan(plan_id)
        from pathlib import Path
        Path(plan["files"][0]["local_path"]).write_text("changed", encoding="utf-8")
    result = engine.tasks.execute(plan_id)
    assert result["task"]["phase"] == "blocked"
    assert submit_count(transport) == 0
    assert not any(c[0] == "mkdir" for c in transport.calls)


def test_launch_failure_keeps_admission_recoverable(task, monkeypatch):
    engine, plan_id, _, transport = task
    enqueue(task)
    def unavailable(*a, **kw):
        raise OSError("unable to create child")
    monkeypatch.setattr(worker.subprocess, "Popen", unavailable)
    with pytest.raises(ValidationError, match="已入队"):
        worker.launch(engine, plan_id)
    assert engine.status(plan_id)["task"]["blocked_reason"] == "launch_failed"
    assert engine.resume(plan_id)["status"] == "completed"
    assert submit_count(transport) == 1


def test_launcher_uses_hidden_independent_process_and_persisted_home(task, monkeypatch):
    engine, plan_id, ref, _ = task
    enqueue(task)
    seen = {}
    class Child:
        def poll(self):
            return None
    def spawn(argv, **kw):
        seen.update(kw, argv=argv)
        return Child()
    monkeypatch.setattr(worker.subprocess, "Popen", spawn)
    worker.launch(engine, plan_id)
    assert seen["stdin"] == worker.subprocess.DEVNULL
    assert seen["stdout"] is seen["stderr"]
    assert seen["close_fds"]
    assert str(engine.config.home.resolve()) in seen["argv"]
    assert all(ref not in value for value in seen["argv"])
    if worker.os.name == "nt":
        assert seen["creationflags"] & worker.subprocess.CREATE_NO_WINDOW
        assert seen["creationflags"] & worker.subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert seen["start_new_session"]
    monkeypatch.setattr(worker.sys, "frozen", True, raising=False)
    assert worker.command(engine, plan_id)[1] == "--config-dir"


def test_unknown_schema_and_broken_records_remain_unchanged(task, monkeypatch):
    engine, plan_id, _, _ = task
    state = enqueue(task)
    state["schema"] = "rsci.run/999"
    engine._save_run(state)
    before = engine.run_path(plan_id).read_bytes()
    monkeypatch.setattr(worker, "launch", lambda *a: pytest.fail("unknown record cannot launch"))
    report = worker.recover(engine)
    assert report["attention"][0]["reason"] == "unsupported_schema"
    assert before == engine.run_path(plan_id).read_bytes()
    engine.run_path(plan_id).write_text("{broken", encoding="utf-8")
    assert worker.recover(engine)["attention"]
    assert engine.list_plans()[0]["status"] == "invalid"
    assert engine.run_path(plan_id).read_text(encoding="utf-8") == "{broken"


def test_unprovable_legacy_is_not_overwritten(task):
    engine, plan_id, _, _ = task
    state = enqueue(task)
    state.pop("schema")
    state.pop("task")
    state.pop("execution_receipt")
    engine._save_run(state)
    before = engine.run_path(plan_id).read_bytes()
    assert "recovery_error" in engine.tasks.execute(plan_id)
    assert before == engine.run_path(plan_id).read_bytes()


def test_try_lock_returns_immediately_to_another_thread(tmp_path):
    lock = tmp_path / "task.lock"
    answer = []
    with try_file_lock(lock) as acquired:
        assert acquired
        def check():
            with try_file_lock(lock) as other:
                answer.append(other)
        thread = threading.Thread(target=check)
        thread.start()
        thread.join(1)
        assert answer == [False]


def test_pbs_without_queue_or_history_evidence_is_unknown(monkeypatch, capsys):
    from types import SimpleNamespace
    from rsci.gateway import rsci_gateway as gateway
    monkeypatch.setattr(gateway, "resolve_server", lambda name: (name, {}))
    monkeypatch.setattr(gateway, "scheduler_for", lambda *a: "pbs")
    monkeypatch.setattr(gateway, "remote", lambda *a, **kw: "")
    monkeypatch.setattr(gateway, "audit", lambda *a, **kw: None)
    gateway.op_job_state(SimpleNamespace(server="cl9", job_id="123"))
    assert json.loads(capsys.readouterr().out)["state"] == "UNKNOWN"


def test_completed_download_checkpoint_detects_local_tampering(task, monkeypatch):
    engine, plan_id, _, transport = task
    enqueue(task)
    original = engine._run_step
    def stop_before_parse(plan, state, step, context, **kwargs):
        if step["type"] == "parse":
            raise KeyboardInterrupt()
        return original(plan, state, step, context, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(engine, "_run_step", stop_before_parse)
        with pytest.raises(KeyboardInterrupt):
            engine.tasks.execute(plan_id)
    from pathlib import Path
    state = engine._load_run(plan_id)
    (Path(state["task"]["download_root"]) / "OUTCAR").write_text("changed", encoding="utf-8")
    result = engine.resume(plan_id)
    assert result["task"]["blocked_reason"] == "step_failed"
    assert "哈希" in result["failure"]["detail"]
    assert submit_count(transport) == 1


def test_verifiable_legacy_run_migrates_to_observation_only(task, monkeypatch):
    engine, plan_id, _, transport = task
    state = interrupt_monitor(task, monkeypatch)
    transport.state.workdirs = {state["job_id"]: engine.load_plan(plan_id)["remote_dir"]}
    state.pop("task")
    state.pop("schema")
    state.pop("execution_receipt")
    state.pop("job_id")
    engine._save_run(state)
    assert engine.resume(plan_id)["status"] == "completed"
    assert engine._load_run(plan_id)["execution_receipt"]["payload"]["mode"] == "observe"
    assert submit_count(transport) == 1


def test_startup_does_not_restart_safety_block(task, monkeypatch):
    engine, plan_id, _, _ = task
    uncertain_submit(task, monkeypatch)
    monkeypatch.setattr(worker, "launch", lambda *a: pytest.fail("uncertain submit cannot relaunch automatically"))
    assert worker.recover(engine)["attention"][0]["reason"] == "submit_uncertain"


def test_resumed_uploads_are_hash_checked_remotely_before_submit(task, monkeypatch):
    engine, plan_id, _, transport = task
    enqueue(task)
    def stop(*a, **kw):
        raise KeyboardInterrupt()
    with monkeypatch.context() as patch:
        patch.setattr(engine.client, "vasp_validate", stop)
        with pytest.raises(KeyboardInterrupt):
            engine.tasks.execute(plan_id)
    directory = engine.load_plan(plan_id)["remote_dir"]
    transport.state.files["cl9"][directory + "/run.job.sh"] = b"echo changed by someone else\n"
    state = engine.resume(plan_id)
    assert state["task"]["blocked_reason"] == "inputs_changed"
    assert submit_count(transport) == 0
    assert not tasks.uncertain(state)  # the failing preflight never entered submit


def test_presubmit_hash_query_retries_without_recording_submit_intent(task, monkeypatch):
    engine, plan_id, _, transport = task
    enqueue(task)
    original, calls, delays = engine.client.run_command, [0], []
    def flaky(*a, **kw):
        calls[0] += 1
        if calls[0] == 1:
            raise RemoteError("connection reset")
        return original(*a, **kw)
    def sleep(seconds):
        if seconds:
            assert not tasks.uncertain(engine._load_run(plan_id))
            delays.append(seconds)
    monkeypatch.setattr(engine.client, "run_command", flaky)
    monkeypatch.setattr(tasks.time, "sleep", sleep)
    assert engine.tasks.execute(plan_id)["status"] == "completed"
    assert delays == [5] and submit_count(transport) == 1
