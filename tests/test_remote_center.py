"""Fleet isolation, conservative scheduler states and resumable collection."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rsci.core.config import ServerEntry
from rsci.core.errors import AuthRequiredError, ValidationError
from rsci.hpc.center import RemoteCenter, project_store, read_record
from rsci.hpc.collections import Collections
from tests.conftest import ROOT
from tests.test_ui import ui, call

RUN = ROOT + "/external"


def seed(app, fake, server="cl9", job_id="713", state="COMPLETED"):
    fake.dirs[server].add(RUN)
    fake.jobs[server] = [{"job_id": job_id, "state": state, "name": "external-python", "directory": RUN}]
    fake.workdirs = {job_id: RUN}
    fake.files[server][RUN + "/results.csv"] = b"x,y\n1,2\n"
    fake.files[server][RUN + "/job.err"] = b"ModuleNotFoundError: No module named scipy\n"
    store = project_store(app.config)
    store.create("remote-study", kind="general")
    center = RemoteCenter(app.config, app.client())
    center.refresh(server)
    return center, store


@pytest.fixture
def remote_job(app_with_fake):
    app, transport = app_with_fake
    fake = transport.state
    center, store = seed(app, fake)
    return app, fake, center, store


def test_fleet_includes_external_and_same_id_on_distinct_servers(remote_job):
    app, fake, center, _ = remote_job
    fake.add_server("pbs1", scheduler="pbs")
    app.config.upsert_server(ServerEntry("pbs1", "user@pbs1", remote_root=ROOT, scheduler="pbs"))
    fake.jobs["pbs1"] = [{"job_id": "713", "state": "RUNNING", "name": "other", "directory": ROOT}]
    result = center.overview()
    assert {(j["server"], j["job_id"]) for j in result["jobs"]} == {("cl9", "713"), ("pbs1", "713")}


def test_disappearance_unknown_and_offline_preserves_last_state(remote_job, monkeypatch):
    app, fake, center, _ = remote_job
    fake.jobs["cl9"][0]["state"] = "RUNNING"
    center.refresh("cl9")
    fake.jobs["cl9"] = []
    row = center.refresh("cl9")["jobs"][0]
    assert row["state"] == "UNKNOWN" and row["last_known_state"] == "RUNNING"
    monkeypatch.setattr(app.client(), "jobs", lambda **kw: (_ for _ in ()).throw(AuthRequiredError("login expired")))
    offline = center.overview()
    assert offline["jobs"][0]["stale"]
    assert offline["jobs"][0]["state"] == "UNKNOWN"
    assert "login expired" in offline["servers"][0]["error"]


def test_project_binding_survives_rename_and_rejects_other_project(remote_job):
    app, _, center, store = remote_job
    center.associate("cl9", "713", "remote-study")
    store.update("remote-study", new_name="renamed")
    assert center.overview(refresh=False)["jobs"][0]["project"] == "renamed"
    store.create("different", kind="general")
    with pytest.raises(ValidationError, match="其他项目"):
        center.associate("cl9", "713", "different")


def test_configuration_change_never_reuses_old_jobs(remote_job):
    app, _, center, _ = remote_job
    app.config.upsert_server(ServerEntry("cl9", "another@host", remote_root=ROOT))
    assert center.overview(refresh=False)["jobs"] == []
    with pytest.raises(ValidationError, match="当前用户"):
        center.associate("cl9", "9999", "remote-study")


def test_workdir_mismatch_and_arbitrary_log_rejected(remote_job):
    _, fake, center, _ = remote_job
    with pytest.raises(ValidationError):
        center.text("cl9", "713", ROOT + "/other/private.txt")
    fake.workdirs["713"] = ROOT + "/different"
    with pytest.raises(ValidationError, match="不一致"):
        center.associate("cl9", "713", "remote-study")


def test_generic_diagnosis_contains_actual_log_evidence(remote_job):
    _, _, center, _ = remote_job
    report = center.diagnose("cl9", "713")
    assert report["domain"] == "general"
    assert "依赖或可执行程序缺失" in report["diagnosis"]["findings"]
    assert "ModuleNotFoundError" in report["diagnosis"]["matches"][0]["excerpt"]
    assert report["diagnosis"]["scientific_converged"] is None


def admit(remote_job):
    app, fake, center, store = remote_job
    center.associate("cl9", "713", "remote-study")
    queue = Collections(app.config, app.client())
    task = queue.enqueue("cl9", "713", [RUN + "/results.csv", RUN + "/job.err"])
    return queue, task


def test_duplicate_collection_atomic_receipts_and_project_guard(remote_job):
    queue, task = admit(remote_job)
    app, fake, center, store = remote_job
    with ThreadPoolExecutor(max_workers=3) as pool:
        duplicates = list(pool.map(lambda _: queue.enqueue("cl9", "713", [RUN + "/results.csv", RUN + "/job.err"]), range(3)))
    assert {t["task_id"] for t in duplicates} == {task["task_id"]}
    with pytest.raises(ValidationError, match="回收任务"):
        store.delete("remote-study")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(queue.execute, [task["task_id"]] * 2))
    result = queue.load(task["task_id"])
    assert result["status"] == "completed"
    assert all(item["sha256"] and item["status"] == "completed" for item in result["files"])
    assert (Path(result["result_directory"]) / "results.csv").read_bytes() == b"x,y\n1,2\n"
    assert fake.job_seq == 1000  # no submit operation


def test_collection_pauses_on_auth_then_skips_completed_files(remote_job, monkeypatch):
    queue, task = admit(remote_job)
    original = queue.client.download
    calls = []
    def download(path, local, **kwargs):
        calls.append(path)
        if path.endswith("results.csv"):
            raise AuthRequiredError("login expired")
        return original(path, local, **kwargs)
    monkeypatch.setattr(queue.client, "download", download)
    queue.execute(task["task_id"])
    state = queue.load(task["task_id"])
    assert state["status"] == "blocked"
    assert state["files"][0]["status"] == "completed"
    monkeypatch.setattr(queue.client, "download", lambda path, local, **kw: (calls.append(path), original(path, local, **kw))[1])
    monkeypatch.setattr("rsci.hpc.collections.launch", lambda q, task_id: q.execute(task_id))
    queue.resume(task["task_id"])
    assert queue.load(task["task_id"])["status"] == "completed"
    assert calls.count(RUN + "/job.err") == 1


@pytest.mark.parametrize("change", ["server", "source", "target"])
def test_collection_rejects_changes_without_overwrite(remote_job, change):
    queue, task = admit(remote_job)
    app, fake, _, _ = remote_job
    if change == "server":
        app.config.upsert_server(ServerEntry("cl9", "other@host", remote_root=ROOT))
    elif change == "source":
        fake.files["cl9"][RUN + "/job.err"] = b"changed"
    else:
        root = Path(task["result_directory"])
        root.mkdir(parents=True)
        (root / "job.err").write_text("user file")
    queue.execute(task["task_id"])
    assert queue.load(task["task_id"])["status"] == "blocked"
    if change == "target":
        assert (Path(task["result_directory"]) / "job.err").read_text() == "user file"


def test_collection_rejects_running_jobs_and_path_traversal(remote_job):
    app, fake, center, _ = remote_job
    center.associate("cl9", "713", "remote-study")
    queue = Collections(app.config, app.client())
    with pytest.raises(ValidationError):
        queue.enqueue("cl9", "713", [RUN + "/../elsewhere.csv"])
    fake.jobs["cl9"][0]["state"] = "PENDING"
    with pytest.raises(ValidationError, match="已结束"):
        queue.enqueue("cl9", "713", [RUN + "/results.csv"])


def test_cancel_and_corrupt_record_preserved(remote_job):
    queue, task = admit(remote_job)
    queue.cancel(task["task_id"])
    queue.execute(task["task_id"])
    assert queue.load(task["task_id"])["status"] == "cancelled"
    queue.path(task["task_id"]).write_text("broken", encoding="utf-8")
    result = queue.list()
    assert result["attention"]
    assert queue.path(task["task_id"]).read_text() == "broken"


def test_crash_after_atomic_publish_adopts_only_its_receipt(remote_job, monkeypatch):
    queue, task = admit(remote_job)
    save = queue.save
    def crash(state):
        if any(f.get("status") == "completed" for f in state["files"]):
            raise SystemExit("simulated power loss before checkpoint")
        save(state)
    monkeypatch.setattr(queue, "save", crash)
    with pytest.raises(SystemExit):
        queue.execute(task["task_id"])
    durable = queue.load(task["task_id"])
    assert durable["files"][0]["status"] == "verified"
    assert (Path(task["result_directory"]) / "job.err").exists()
    monkeypatch.setattr(queue, "save", save)
    downloaded = []
    original = queue.client.download
    monkeypatch.setattr(queue.client, "download", lambda path, local, **kw: (downloaded.append(path), original(path, local, **kw))[1])
    queue.execute(task["task_id"])
    assert queue.load(task["task_id"])["status"] == "completed"
    assert downloaded == [RUN + "/results.csv"]


def test_launch_failure_leaves_recoverable_record(remote_job, monkeypatch):
    from rsci.hpc.collections import launch
    queue, task = admit(remote_job)
    def fail(*args, **kwargs):
        raise OSError("test cannot start process")
    monkeypatch.setattr("rsci.hpc.collections.subprocess.Popen", fail)
    with pytest.raises(ValidationError, match="启动失败"):
        launch(queue, task["task_id"])
    assert queue.load(task["task_id"])["status"] == "blocked"


def test_unsafe_record_cannot_write_outside_results(remote_job):
    queue, task = admit(remote_job)
    task["result_relative"] = "../outside"
    queue.save(task)
    with pytest.raises(ValidationError, match="不安全"):
        queue.execute(task["task_id"])
    assert queue.list()["attention"]


def test_incomplete_record_does_not_hide_healthy_queue(remote_job):
    queue, task = admit(remote_job)
    task.pop("project_id")
    queue.save(task)
    result = queue.list()
    assert result["ok"] and result["attention"]


def test_legacy_file_timestamp_is_not_a_scheduler_verdict(tmp_path):
    from rsci.core.jobhistory import JobLedger
    ledger = JobLedger(tmp_path)
    ledger._save("cl9", {"7": {"job_id": "7", "state": "COMPLETED", "completion_source": "files"}})
    assert ledger.merged("cl9")[0]["state"] == "UNKNOWN"
    ledger.observe("cl9", [{"job_id": "7", "state": "COMPLETED"}])
    assert ledger.merged("cl9")[0]["state"] == "COMPLETED"


def test_completed_result_path_tracks_project_rename(remote_job):
    queue, task = admit(remote_job)
    queue.execute(task["task_id"])
    remote_job[3].update("remote-study", new_name="renamed")
    view = queue.list()["tasks"][0]
    assert view["project"] == "renamed"
    assert Path(view["result_directory"]).is_dir()


def test_api_routes_and_token_protected_asset(ui, monkeypatch):
    app, fake = ui["app"], ui["state"]
    seed(app, fake)
    assert call(ui, "job.center")["jobs"][0]["job_id"] == "713"
    args = {"server": "cl9", "job_id": "713"}
    assert call(ui, "job.associate", {**args, "project": "remote-study"})["ok"]
    assert call(ui, "job.details", args)["files"]
    assert call(ui, "job.diagnosis", args)["diagnosis"]["matches"]
    monkeypatch.setattr("rsci.hpc.collections.launch", lambda q, tid: {"ok": True, "task_id": tid})
    result = call(ui, "collection.enqueue", {**args, "paths": [RUN + "/results.csv"]})
    assert result["ok"] and result["task_id"]
    assert call(ui, "collection.list")["tasks"][0]["task_id"] == result["task_id"]
    assert call(ui, "collection.cancel", {"task_id": result["task_id"]})["ok"]
    import urllib.request
    with urllib.request.urlopen(ui["base"] + "/t/" + ui["token"] + "/remote-center.js") as response:
        assert b"loadRemoteJobs" in response.read()
