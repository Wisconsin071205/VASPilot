"""General projects and approval-bound Python analysis with real subprocesses."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from rsci.cli.main import App
from rsci.core.config import Config
from rsci.core.errors import ApprovalError, ValidationError
from rsci.core.hashing import file_sha256
from rsci.workflow import local_python, worker
from rsci.workflow.research import prepare_csv_template, project_store


@pytest.fixture()
def research(tmp_path):
    app = App(Config(tmp_path / "research-home"))
    store = project_store(app.config, app.audit)
    store.create("experiment")
    store.import_file("experiment", "data/sample.csv", b"group,value,delta\na,2,-2\nb,4,6\nc,,bad\n")
    template = prepare_csv_template(app.config, "experiment", "data/sample.csv")
    return app, store, template


def prepared(research):
    app, _, template = research
    engine = app.engine()
    result = engine.prepare(template["spec"])
    plan_id = result["plan_id"]
    approval = engine.approve(plan_id, confirmed=True)
    return engine, plan_id, approval["approval_ref"]


def wait_until(read, predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = read()
        if predicate(result):
            return result
        time.sleep(.1)
    raise AssertionError(f"condition not reached: {result!r}")


def test_default_general_project_and_explicit_legacy_migration(research):
    app, store, _ = research
    assert store.kind("experiment") == "general"
    assert {p.name for p in (app.config.projects_dir / "experiment").iterdir() if p.is_dir()} == {
        "data", "scripts", "notes", "results", "references"}
    legacy = app.config.projects_dir / "legacy"
    legacy.mkdir()
    (legacy / "INCAR").write_text("ENCUT=520", encoding="utf-8")
    assert store.kind("legacy") == "vasp"
    assert (legacy / "INCAR").read_text() == "ENCUT=520"
    assert json.loads(app.config.projects_index_path.read_text())["legacy"]["migrated_from"] == "legacy-vasp"


@pytest.mark.parametrize("name", ["../escape.csv", "data/../../escape.csv", "data\\escape.csv",
    "C:/temp/x.csv", "data/.secret.txt", "data/con.txt", "data/x.csv:secret", "data/link./x.txt",
    "scripts/x.exe", "POTCAR", "data/POTCAR.txt"])
def test_managed_paths_reject_escape_and_unsafe_names(research, name):
    _, store, _ = research
    with pytest.raises(ValidationError):
        store.write_file("experiment", name, "never write")


def test_hashes_prevent_stale_editor_and_import_overwrite(research):
    _, store, _ = research
    row = store.read_file("experiment", "data/sample.csv")
    store.write_file("experiment", "data/sample.csv", "x\n1\n", expected_sha256=row["sha256"])
    with pytest.raises(ValidationError, match="changed"):
        store.write_file("experiment", "data/sample.csv", "stale", expected_sha256=row["sha256"])
    with pytest.raises(ValidationError, match="already exists"):
        store.import_file("experiment", "data/sample.csv", b"stale")


def test_corrupt_project_index_preserved(research):
    app, store, _ = research
    app.config.projects_index_path.write_text("broken", encoding="utf-8")
    with pytest.raises(ValidationError, match="damaged"):
        store.list()
    assert app.config.projects_index_path.read_text() == "broken"


def test_analysis_plan_binds_code_input_interpreter_and_outputs(research):
    app, store, template = research
    engine = app.engine()
    first = engine.prepare(template["spec"])["plan"]
    assert first["domain"] == "general"
    assert first["execution"]["python_sha256"] == file_sha256(Path(sys.executable))
    assert "安全沙箱" in first["risk_summary"]["notes"][0]
    store.write_file("experiment", "data/sample.csv", "x\n2\n")
    assert engine.prepare(template["spec"])["plan_id"] != first["plan_id"]
    assert engine.prepare({**template["spec"], "outputs": ["results/other.json"]})["plan_id"] != first["plan_id"]


def test_real_python_pipeline_no_hpc_and_no_reexecution(research):
    engine, plan_id, token = prepared(research)
    with pytest.raises(ApprovalError):
        engine.run(plan_id, "not-an-approval")
    result = engine.run(plan_id, token)
    assert result["status"] == "completed", result
    assert result["execution_state"] == "COMPLETED"
    assert result["scheduler_state"] == "NOT_APPLICABLE"
    assert result["scientific_converged"] is None
    root = Path(result["task"]["download_root"])
    summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    assert summary["rows"] == 3
    assert summary["columns"]["value"]["mean"] == 3
    assert summary["columns"]["value"]["missing_count"] == 1
    assert summary["columns"]["delta"]["non_numeric_count"] == 1
    assert "\n## Limitations\n" in (root / "report.md").read_text(encoding="utf-8")
    assert (root / "means.svg").read_text(encoding="utf-8").startswith("<svg")
    assert len(result["task"]["artifacts"]) == 3
    assert engine.resume(plan_id)["run_instance"] == result["run_instance"]
    assert len(engine.status(plan_id)["attempts"]) == 1
    assert [p["plan_id"] for p in engine.list_plans()] == [plan_id]
    assert worker.recover(engine)["attention"] == []


def test_changed_inputs_block_before_python(research):
    engine, plan_id, token = prepared(research)
    research[1].write_file("experiment", "data/sample.csv", "x\n3\n")
    with pytest.raises(ValidationError, match="changed"):
        engine.run(plan_id, token)
    assert not local_python.paths(engine, engine.load_plan(plan_id))[1].exists()


def test_monitor_crash_survives_and_only_collects_once(research):
    app, store, template = research
    script = store.read_file("experiment", "scripts/analyze.py")["content"]
    store.write_file("experiment", "scripts/analyze.py", "import time\ntime.sleep(2)\n" + script)
    engine, plan_id, token = prepared(research)
    engine.tasks.enqueue(plan_id, token)
    with subprocess.Popen(worker.command(engine, plan_id), stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as monitor:
        try:
            wait_until(lambda: engine._load_run(plan_id), lambda s: bool(s.get("local_execution")))
            evidence = local_python.paths(engine, engine.load_plan(plan_id))[1]
            wait_until(lambda: evidence.exists(), bool)
        finally:
            monitor.terminate()
            monitor.wait(timeout=10)
    report = wait_until(lambda: json.loads(evidence.read_text(encoding="utf-8")),
                        lambda r: r["payload"]["state"] == "COMPLETED")
    result = App(Config(app.config.home)).engine().resume(plan_id)
    assert result["status"] == "completed", result
    assert len([e for e in result["events"] if e["event"] == "python.execution_intent"]) == 1
    assert json.loads(evidence.read_text(encoding="utf-8")) == report


def test_local_job_cannot_be_bound_to_remote_or_rerun_after_uncertain_launch(research, monkeypatch):
    engine, plan_id, token = prepared(research)
    monkeypatch.setattr(local_python, "_launch", lambda *_: (_ for _ in ()).throw(OSError("failed launch")))
    result = engine.run(plan_id, token)
    assert result["task"]["blocked_reason"] == "execution_uncertain"
    with pytest.raises(ValidationError, match="local|本地"):
        engine.tasks.bind_job(plan_id, "123", f"bind {plan_id} 123")
    assert engine.tasks.reconcile(plan_id)["execution"] == "local_python"
    assert worker.recover(engine)["attention"][0]["reason"] == "execution_uncertain"


@pytest.mark.parametrize("source,expected", [
    ("raise SystemExit(3)\n", "FAILED"),
    ("import time\ntime.sleep(20)\n", "TIMEOUT"),
    ("pass\n", "COMPLETED"),
])
def test_failure_timeout_and_missing_results_not_success(research, source, expected):
    app, store, template = research
    store.write_file("experiment", "scripts/analyze.py", source)
    template["spec"]["timeout_seconds"] = 1
    engine, plan_id, token = prepared(research)
    result = engine.run(plan_id, token)
    assert result["execution_state"] == expected
    assert result["status"] != "completed"
    assert result["collection_status"] == "incomplete"
    assert result["scientific_converged"] is None
    assert result["missing_outputs"]
    assert engine.resume(plan_id)["run_instance"] == result["run_instance"]
    assert len(engine.status(plan_id)["attempts"]) == 1


def test_collection_conflict_preserved_and_retry_does_not_execute(research):
    engine, plan_id, token = prepared(research)
    plan = engine.load_plan(plan_id)
    root = local_python.result_directory(engine, plan)
    root.mkdir(parents=True)
    target = root / "summary.json"
    target.write_text("user data", encoding="utf-8")
    result = engine.run(plan_id, token)
    assert result["task"]["blocked_reason"] == "result_conflict"
    assert target.read_text() == "user data"
    report = local_python.paths(engine, plan)[1].read_bytes()
    target.rename(root / "summary-original.json")
    resumed = engine.resume(plan_id)
    assert resumed["status"] == "completed"
    assert local_python.paths(engine, plan)[1].read_bytes() == report
    assert len([e for e in resumed["events"] if e["event"] == "python.execution_intent"]) == 1
    assert (root / "summary-original.json").read_text() == "user data"


def test_damaged_plan_does_not_hide_last_known_status(research):
    engine, plan_id, token = prepared(research)
    result = engine.run(plan_id, token)
    run_bytes = engine.run_path(plan_id).read_bytes()
    engine.plan_path(plan_id).write_text("damaged", encoding="utf-8")
    view = engine.status(plan_id)
    assert view["status"] == result["status"] == "completed"
    assert view["task"]["blocked_reason"] == "invalid_plan"
    assert view["task"]["artifacts"] == result["task"]["artifacts"]
    assert "execution_receipt" not in view
    assert engine.run_path(plan_id).read_bytes() == run_bytes
    assert engine.plan_path(plan_id).read_text() == "damaged"
    with pytest.raises(ValidationError):
        engine.resume(plan_id)
