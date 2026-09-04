"""Gateway integration tests: run the REAL gateway script against the fake HPC.

Exercised across the process boundary (subprocess + JSON protocol):
path confinement, trash lifecycle with double-confirm, upload/download
integrity with SHA-256, submit/cancel validation, scheduler-vs-science
separation, server catalog CRUD, and the auth_required signal.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
GATEWAY = SRC / "vaspilot" / "gateway" / "vaspilot_gateway.py"
ROOT = "/hpc/home/tester/vaspilot-root"


@pytest.fixture()
def gateway_env(tmp_path, monkeypatch):
    """Isolated gateway (config/cache) + fake HPC, returning the env factory."""
    config_dir = tmp_path / "gw-config"
    cache_dir = tmp_path / "gw-cache"
    fs = tmp_path / "cl9fs"
    stage = tmp_path / "stage"
    for directory in (config_dir, cache_dir, fs, stage):
        directory.mkdir(parents=True)
    fake_config = tmp_path / "fake-hpc-config.json"
    vlab_home = tmp_path / "vlab-home"
    vlab_home.mkdir()
    fake_config.write_text(json.dumps({
        "servers": {"cl9": {"root": ROOT, "real": str(fs),
                            "scheduler": "slurm"}},
        "stage_dir": str(stage), "next_job_id": 2000, "jobs": {},
    }), encoding="utf-8")
    monkeypatch.setenv("VASPILOT_GATEWAY_CONFIG", str(config_dir))
    monkeypatch.setenv("VASPILOT_GATEWAY_CACHE", str(cache_dir))
    monkeypatch.setenv("VASPILOT_FAKE_HPC", str(Path(__file__).parent / "fake_hpc.py"))
    monkeypatch.setenv("VASPILOT_FAKE_HPC_CONFIG", str(fake_config))
    # gateway-local stage reads resolve into the fixture stage dir
    monkeypatch.setenv("VASPILOT_GATEWAY_STAGE_DIR", str(stage))
    # the gateway generates per-server keys under ~/.ssh/vaspilot —
    # redirect HOME/USERPROFILE so tests never touch the real home
    monkeypatch.setenv("HOME", str(vlab_home))
    monkeypatch.setenv("USERPROFILE", str(vlab_home))

    def run(*args, check=False):
        result = subprocess.run(
            [sys.executable, str(GATEWAY), *args],
            capture_output=True, text=True, encoding="utf-8", timeout=120)
        document = None
        for line in result.stdout.splitlines():
            if line.startswith("{"):
                document = json.loads(line)
                break
        if check and (document is None or not document.get("ok")):
            raise AssertionError(f"gateway op failed: {result.stdout}\n"
                                 f"{result.stderr}")
        return document

    # register the server and connect (fake ControlMaster marker)
    run("server-add", "cl9", "--target", "user@cl9", "--root", ROOT,
        "--persist", "8h", "--scheduler", "slurm", check=True)
    run("connect", "--server", "cl9", check=True)
    return {"run": run, "fs": fs, "stage": stage, "cache": cache_dir,
            "config": config_dir, "fake_config": fake_config}


class TestConfinement:
    def test_outside_root_rejected(self, gateway_env):
        result = gateway_env["run"]("list", "--server", "cl9", "/etc")
        assert result is not None and not result["ok"]
        assert "must remain under" in result["error"]["message"]

    def test_traversal_rejected(self, gateway_env):
        result = gateway_env["run"]("read", "--server", "cl9",
                                    f"{ROOT}/../etc/passwd")
        assert result is not None and not result["ok"]

    def test_root_removal_refused(self, gateway_env):
        result = gateway_env["run"]("remove", "--server", "cl9", ROOT)
        assert result is None or not result.get("ok")

    def test_control_characters_rejected(self, gateway_env):
        # NUL cannot cross Windows argv; newline is also a control character
        result = gateway_env["run"]("stat", "--server", "cl9",
                                    f"{ROOT}/x\ny")
        assert result is None or not result.get("ok")


class TestFileOperations:
    def test_mkdir_write_read_roundtrip(self, gateway_env):
        run, fs = gateway_env["run"], gateway_env["fs"]
        run("mkdir", "--server", "cl9", f"{ROOT}/runs/case1", check=True)
        target = fs / "runs" / "case1" / "INCAR"
        target.write_text("SYSTEM = sim\n", encoding="utf-8")
        result = run("read", "--server", "cl9", f"{ROOT}/runs/case1/INCAR")
        assert result["ok"]
        assert "SYSTEM = sim" in result["content"]
        result = run("list", "--server", "cl9", f"{ROOT}/runs/case1")
        assert any(e["name"] == "INCAR" and e["type"] == "file"
                   for e in result["entries"])

    def test_read_refuses_potcar(self, gateway_env):
        run, fs = gateway_env["run"], gateway_env["fs"]
        (fs / "POTCAR").write_text("  TITEL  = PAW X\n", encoding="utf-8")
        result = run("read", "--server", "cl9", f"{ROOT}/POTCAR")
        assert result is not None and not result["ok"]
        assert "may not be read" in result["error"]["message"]

    def test_list_is_non_recursive_and_reports_truncation(self, gateway_env):
        run, fs = gateway_env["run"], gateway_env["fs"]
        directory = fs / "runs" / "many"
        directory.mkdir(parents=True)
        for index in range(4):
            (directory / f"entry-{index}.txt").write_text("x", encoding="utf-8")
        result = run("list", "--server", "cl9", f"{ROOT}/runs/many",
                     "--limit", "3", check=True)
        assert len(result["entries"]) == 3
        assert result["truncated"] is True
        assert result["limit"] == 3

    def test_upload_verifies_sha(self, gateway_env):
        run, stage, fs = gateway_env["run"], gateway_env["stage"], gateway_env["fs"]
        local = stage / "staged-file"
        local.write_bytes(b"payload-bytes")
        import hashlib
        sha = hashlib.sha256(b"payload-bytes").hexdigest()
        # tampered hash -> mismatch
        result = run("upload", "--server", "cl9", "/tmp/vaspilot-abcdef0123456789",
                     f"{ROOT}/up/INCAR", "0" * 64)
        assert result is None or not result.get("ok")
        # write the stage where the fake expects it, then upload for real
        expected_stage = stage / "vaspilot-abcdef0123456789"
        expected_stage.parent.mkdir(parents=True, exist_ok=True)
        expected_stage.write_bytes(b"payload-bytes")
        result = run("upload", "--server", "cl9",
                     "/tmp/vaspilot-abcdef0123456789", f"{ROOT}/up/INCAR", sha)
        assert result["ok"]
        assert (fs / "up" / "INCAR").read_bytes() == b"payload-bytes"
        # a second identical upload is idempotent; the CLI re-stages each time
        expected_stage.write_bytes(b"payload-bytes")
        result = run("upload", "--server", "cl9",
                     "/tmp/vaspilot-abcdef0123456789", f"{ROOT}/up/INCAR", sha)
        assert result["ok"] and result["status"] == "identical"

    def test_download_reports_sha(self, gateway_env):
        run, fs, stage = gateway_env["run"], gateway_env["fs"], gateway_env["stage"]
        (fs / "dl").mkdir(exist_ok=True)
        (fs / "dl" / "OSZICAR").write_bytes(b"   1 F= -.1E+02\n")
        result = run("download", "--server", "cl9", f"{ROOT}/dl/OSZICAR",
                     "/tmp/vaspilot-0011223344556677")
        assert result["ok"]
        import hashlib
        assert result["sha256"] == hashlib.sha256(b"   1 F= -.1E+02\n").hexdigest()


class TestTrashLifecycle:
    def test_trash_restore_and_purge(self, gateway_env):
        run, fs = gateway_env["run"], gateway_env["fs"]
        (fs / "victim.txt").write_text("important\n", encoding="utf-8")
        result = run("remove", "--server", "cl9", f"{ROOT}/victim.txt")
        assert result["ok"]
        trash_id = result["trash_id"]
        assert not (fs / "victim.txt").exists()
        listing = run("trash-list", "--server", "cl9")
        assert any(t["trash_id"] == trash_id for t in listing["trash"])
        # restore
        result = run("restore", "--server", "cl9", trash_id)
        assert result["ok"]
        assert (fs / "victim.txt").read_text(encoding="utf-8") == "important\n"
        # purge requires a double match
        result = run("purge", "--server", "cl9", trash_id, "--confirm-trash-id",
                     "wrong")
        assert result is not None and not result["ok"]
        # trash it again, then purge for real
        result = run("remove", "--server", "cl9", f"{ROOT}/victim.txt")
        trash_id = result["trash_id"]
        result = run("purge", "--server", "cl9", trash_id,
                     "--confirm-trash-id", trash_id)
        assert result["ok"]
        listing = run("trash-list", "--server", "cl9")
        assert not any(t["trash_id"] == trash_id for t in listing["trash"])


class TestJobsAndScience:
    def test_submit_cancel_and_states(self, gateway_env):
        run, fs = gateway_env["run"], gateway_env["fs"]
        (fs / "job").mkdir()
        (fs / "job" / "run.job.sh").write_text("#!/bin/bash\n", encoding="utf-8")
        result = run("submit", "--server", "cl9", f"{ROOT}/job", "run.job.sh")
        assert result["ok"]
        job_id = result["job_id"]
        state = run("job-state", "--server", "cl9", job_id)
        assert state["state"] == "RUNNING"
        cancel = run("cancel", "--server", "cl9", job_id,
                     "--confirm-job-id", job_id)
        assert cancel["ok"]
        state = run("job-state", "--server", "cl9", job_id)
        assert state["state"] == "CANCELLED"

    def test_submit_rejects_script_injection(self, gateway_env):
        run, fs = gateway_env["run"], gateway_env["fs"]
        (fs / "job").mkdir(exist_ok=True)
        result = run("submit", "--server", "cl9", f"{ROOT}/job",
                     "run.sh; rm -rf /")
        assert result is None or not result.get("ok")

    def test_cancel_requires_double_match(self, gateway_env):
        result = gateway_env["run"]("cancel", "--server", "cl9", "2001",
                                    "--confirm-job-id", "2002")
        assert result is not None and not result["ok"]
        assert "exactly match" in result["error"]["message"]

    def test_vasp_progress_science_vs_scheduler(self, gateway_env):
        run, fs = gateway_env["run"], gateway_env["fs"]
        good = fs / "case-good"
        good.mkdir()
        (good / "INCAR").write_text("NSW=99\nNELM=60\n", encoding="utf-8")
        (good / "OSZICAR").write_text(
            "      1     -0.1E+02\n"
            "   1 F= -.10000000E+02  E0= -.10000000E+02\n", encoding="utf-8")
        (good / "OUTCAR").write_text("reached required accuracy\n",
                                     encoding="utf-8")
        progress = run("vasp-progress", "--server", "cl9",
                       f"{ROOT}/case-good")
        assert progress["ok"]
        assert progress["scientific_converged"] is True
        # an unconverged case reports the distinction
        bad = fs / "case-bad"
        bad.mkdir()
        (bad / "INCAR").write_text("NSW=99\nNELM=2\n", encoding="utf-8")
        (bad / "OSZICAR").write_text(
            "      1     -0.1E+02\n      2     -0.1E+02\n"
            "   1 F= -.10000000E+02  E0= -.10000000E+02\n", encoding="utf-8")
        (bad / "OUTCAR").write_text("nothing converged\n", encoding="utf-8")
        progress = run("vasp-progress", "--server", "cl9", f"{ROOT}/case-bad")
        assert progress["ok"]
        assert progress["scientific_converged"] is False
        assert progress["electronic_reached_nelm"] is True

    def test_vasp_validate_reports_missing_inputs(self, gateway_env):
        run, fs = gateway_env["run"], gateway_env["fs"]
        (fs / "empty-case").mkdir()
        result = run("vasp-validate", "--server", "cl9", f"{ROOT}/empty-case")
        assert result["ok"] is True or result["ok"] is False
        assert any("missing" in e for e in result.get("errors", []))


class TestCatalogAndSessions:
    def test_servers_catalog_and_default(self, gateway_env):
        result = gateway_env["run"]("servers")
        assert result["ok"]
        entry = next(s for s in result["servers"] if s["name"] == "cl9")
        assert entry["remote_root"] == ROOT
        assert entry["connected"] is True
        assert result["default"] == "cl9"

    def test_server_edit_updates_root(self, gateway_env):
        result = gateway_env["run"]("server-edit", "cl9", "--root", ROOT)
        assert result["ok"]
        # an invalid root is refused
        bad = gateway_env["run"]("server-edit", "cl9", "--root", "not/absolute")
        assert bad is None or not bad.get("ok")

    def test_disconnect_then_auth_required(self, gateway_env):
        run = gateway_env["run"]
        run("disconnect", "--server", "cl9", check=True)
        result = run("list", "--server", "cl9", f"{ROOT}")
        assert result is not None and not result["ok"]
        assert result["error"]["code"] == "disconnected"
        run("connect", "--server", "cl9", check=True)

    def test_unknown_server_fails(self, gateway_env):
        result = gateway_env["run"]("status", "--server", "ghost")
        assert result is not None and not result["ok"]

    def test_invalid_server_name_rejected(self, gateway_env):
        result = gateway_env["run"]("server-add", "bad name",
                                    "--target", "user@host", "--root", ROOT)
        assert result is None or not result.get("ok")

    def test_version_protocol(self, gateway_env):
        result = gateway_env["run"]("version")
        assert result["protocol"] == "2"
        assert result["gateway_version"] == "1.3.3"

    def test_exec_passthrough(self, gateway_env):
        result = gateway_env["run"]("exec", "--server", "cl9",
                                    "--timeout", "10", "--",
                                    "echo gateway-exec-ok")
        assert result["ok"] is True
        assert result["rc"] == 0
        assert "gateway-exec-ok" in result["stdout"]

    def test_exec_failure_reported(self, gateway_env):
        result = gateway_env["run"]("exec", "--server", "cl9", "--",
                                    "sh", "-c", "exit 3")
        assert result["ok"] is True          # the op ran; the command failed
        assert result["rc"] == 3

    def test_exec_empty_command_rejected(self, gateway_env):
        result = gateway_env["run"]("exec", "--server", "cl9", "--timeout", "5")
        assert result is None or not result.get("ok")

    def test_structured_write_flow(self, gateway_env):
        """v1.3.3 structured text save: new file, stale-sha conflict,
        clean update, denylist refusal — all through the real gateway."""
        import hashlib
        stage = gateway_env["stage"]
        stage_arg = "/tmp/vaspilot-aaaa1111"
        target = f"{ROOT}/runs/written.txt"

        content = b"hello structured write\n"
        (stage / "vaspilot-aaaa1111").write_bytes(content)
        doc = gateway_env["run"]("write", "--server", "cl9",
                                 "--expected-sha", "",
                                 stage_arg, target, check=True)
        assert doc["sha256"] == hashlib.sha256(content).hexdigest()
        baseline = doc["sha256"]
        real = gateway_env["fs"] / "runs" / "written.txt"
        assert real.read_bytes() == content

        # stale baseline -> remote_changed, original file untouched
        (stage / "vaspilot-aaaa1111").write_bytes(b"second attempt\n")
        doc = gateway_env["run"]("write", "--server", "cl9",
                                 "--expected-sha", "0" * 64,
                                 stage_arg, target)
        assert doc["ok"] is False and doc["error"]["code"] == "remote_changed"
        assert real.read_bytes() == content

        # clean update with the correct baseline (current remote sha)
        (stage / "vaspilot-aaaa1111").write_bytes(b"second attempt\n")
        doc = gateway_env["run"]("write", "--server", "cl9",
                                 "--expected-sha", baseline,
                                 stage_arg, target, check=True)
        assert doc["sha256"] == hashlib.sha256(b"second attempt\n").hexdigest()

        # text denylist refuses structured writes too
        doc = gateway_env["run"]("write", "--server", "cl9",
                                 "--expected-sha", "", stage_arg,
                                 f"{ROOT}/runs/CHGCAR")
        assert doc["ok"] is False and doc["error"]["code"] == "text_denylist"

    def test_recent_survives_sacct_outage(self, gateway_env):
        """slurmdbd down must not fail the whole history op: empty jobs +
        warning instead of remote_command_failed (v1.3.2)."""
        import json
        path = gateway_env["fake_config"]
        data = json.loads(path.read_text(encoding="utf-8"))
        data["sacct_fail"] = True
        path.write_text(json.dumps(data), encoding="utf-8")
        result = gateway_env["run"]("recent", "--server", "cl9")
        assert result["ok"] is True
        assert result["jobs"] == []
        assert "sacct unavailable" in result.get("warning", "")
        data["sacct_fail"] = False
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_metrics_sections(self, gateway_env):
        result = gateway_env["run"]("metrics", "--server", "cl9")
        assert result["ok"] is True
        sections = result["sections"]
        assert "cpu  " in sections["cpu1"]
        assert "MemTotal" in sections["mem"]
        # v1.2.0: gpu rows carry uuid as the second field, proc rows carry
        # the owning user as a fifth field, and a heartbeat section exists
        assert sections["gpu"].startswith("0, GPU-fake-a100-0")
        assert sections["gpu_proc"].strip().endswith(", wuhong")
        assert "hb" in sections
        assert sections["sched"].startswith("slurm")

    def test_metrics_offline_rejected(self, gateway_env):
        gateway_env["run"]("disconnect", "--server", "cl9", check=True)
        result = gateway_env["run"]("metrics", "--server", "cl9")
        assert result is None or not result.get("ok")


def test_pbs_sched_section_columns():
    """qstat -q columns must map to state/run/queued correctly (v1.2.0 fix:
    previously 状态 showed the Run count)."""
    from vaspilot.gateway.client import _parse_metric_sections
    parsed = _parse_metric_sections({
        "sched": "pbs\n"
                 "Queue Memory CPU_Time Time_In_Q Run Que Lm State\n"
                 "normal -- -- -- 5 2 5 E R\n"
                 "short -- -- -- 0 0 30 E R\n"
                 "atk -- -- -- 1 0 2 R 10\n"})
    queue = parsed["queue"]
    assert queue["kind"] == "pbs"
    rows = {p["queue"]: p for p in queue["partitions"]}
    assert rows["normal"]["run"] == 5 and rows["normal"]["queued"] == 2
    assert rows["normal"]["state"].startswith("E")
    assert rows["short"]["queued"] == 0
    assert rows["atk"]["run"] == 1


class TestKeyAuthLifecycle:
    """v1.3.0 per-server key auth + auto reconnect, against the REAL
    gateway script with the fake HPC."""

    def _flip(self, gateway_env, name, **flags):
        import json
        path = gateway_env["fake_config"]
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = data.setdefault("servers", {}).setdefault(name, {})
        entry.update(flags)
        path.write_text(json.dumps(data), encoding="utf-8")

    def _marker(self, gateway_env):
        return gateway_env["cache"] / "ctl-cl9.sock"

    def test_generate_refuses_overwrite(self, gateway_env):
        run = gateway_env["run"]
        first = run("key-generate", "cl9")
        assert first["ok"] is True and first["key_material_present"] is True
        again = run("key-generate", "cl9")
        assert again["ok"] is False and again["error"]["code"] == "key_exists"

    def test_install_requires_live_session(self, gateway_env):
        run = gateway_env["run"]
        run("key-generate", "cl9")
        run("disconnect", "--server", "cl9", check=True)
        result = run("key-install", "cl9")
        assert result["ok"] is False
        assert result["error"]["code"] == "disconnected"

    def test_full_lifecycle_with_auto_reconnect(self, gateway_env):
        run = gateway_env["run"]
        assert run("key-generate", "cl9")["ok"] is True
        run("key-install", "cl9")
        servers = run("servers")["servers"]
        me = next(s for s in servers if s["name"] == "cl9")
        assert me["auth_mode"] == "key" and me["auto_connect"] is True
        # Vlab/HPC restart: kill the ControlMaster socket, then any read op
        # must transparently rebuild the session with the key
        self._marker(gateway_env).unlink()
        result = run("whoami", "--server", "cl9")
        assert result["ok"] is True
        assert self._marker(gateway_env).exists()   # session rebuilt

    def test_key_rejection_surfaces_and_backs_off(self, gateway_env):
        run = gateway_env["run"]
        run("key-generate", "cl9")
        run("key-install", "cl9")
        self._flip(gateway_env, "cl9", reject_key=True)
        self._marker(gateway_env).unlink()
        result = run("whoami", "--server", "cl9")
        assert result["ok"] is False
        assert "key_rejected" in result["error"]["message"]
        import json
        state = json.loads((gateway_env["cache"] /
                            "reconnect-state.json").read_text())
        assert state["cl9"]["failure_count"] == 1
        assert state["cl9"]["error_code"] == "key_rejected"

    def test_host_key_failure_fails_closed(self, gateway_env):
        run = gateway_env["run"]
        run("key-generate", "cl9")
        run("key-install", "cl9")
        self._flip(gateway_env, "cl9", host_key_fail=True)
        self._marker(gateway_env).unlink()
        first = run("whoami", "--server", "cl9")
        assert first["ok"] is False
        assert "host_key_failed" in first["error"]["message"]
        import json, time
        state = json.loads((gateway_env["cache"] /
                            "reconnect-state.json").read_text())
        count = state["cl9"]["failure_count"]
        time.sleep(0.2)
        second = run("whoami", "--server", "cl9")
        assert second["ok"] is False
        state = json.loads((gateway_env["cache"] /
                            "reconnect-state.json").read_text())
        assert state["cl9"]["failure_count"] == count   # never auto-retried

    def test_backoff_ladder_30_60_120_300(self, gateway_env):
        run = gateway_env["run"]
        run("key-generate", "cl9")
        run("key-install", "cl9")
        self._flip(gateway_env, "cl9", reject_key=True)
        import json
        gaps = []
        for _ in range(4):
            self._marker(gateway_env).unlink(missing_ok=True)
            run("connect", "--server", "cl9", "--manual")   # force attempt
            state = json.loads((gateway_env["cache"] /
                                "reconnect-state.json").read_text())
            entry = state["cl9"]
            gaps.append(entry["next_attempt"] - entry["last_attempt"])
        assert gaps == [30, 60, 120, 300]


class TestPbsQstatFParsing:
    """v1.3.1: PBS jobs/recent parse `qstat -f` stanzas — walltime, name,
    and the completion timestamp of FINISHED jobs now survive."""

    def test_pbs_jobs_and_recent(self, gateway_env):
        run = gateway_env["run"]
        # fake HPC 需认识这台服务器（qstat -f 会经它的 execute 执行）
        import json as _json
        fc = Path(gateway_env["fake_config"])
        data = _json.loads(fc.read_text(encoding="utf-8"))
        data["servers"]["pbst"] = {"root": "/h/x", "real": str(gateway_env["fs"]),
                                   "scheduler": "pbs"}
        fc.write_text(_json.dumps(data), encoding="utf-8")
        run("server-add", "pbst", "--target", "u@p", "--root", "/h/x",
            "--scheduler", "pbs", check=True)
        run("connect", "--server", "pbst", check=True)
        jobs = run("jobs", "--server", "pbst")
        if not jobs.get("ok"):
            import sys as _sys
            print("JOBS-FAIL:", _json.dumps(jobs, ensure_ascii=False)[:600],
                  file=_sys.stderr)
        assert jobs["ok"] is True and jobs["scheduler"] == "pbs"
        active = {j["job_id"]: j for j in jobs["jobs"]}
        # 只有活动作业进 job 列表；Finished 被 过滤
        assert set(active) == {"5001"}
        assert active["5001"]["state"] == "RUNNING"
        assert active["5001"]["elapsed"] == "00:12:34"
        # 申请上限不得覆盖实际用时（v1.5.1 回归）
        assert active["5001"]["limit"] == "72:00:00"
        assert active["5001"]["name"] == "relax_case1"

        recent = run("recent", "--server", "pbst")
        rows = {j["job_id"]: j for j in recent["jobs"]}
        assert set(rows) == {"5000", "5001"}
        # 完成作业的耗时是实际用量，不是申请上限
        assert rows["5000"]["elapsed"] == "01:02:03"
        done = rows["5000"]
        assert done["state"] == "COMPLETED"
        assert done["elapsed"] == "01:02:03"
        assert done["name"] == "bader_run"
        assert "2026-08-29" in done["completed_at"]
        assert "11:02:03" in done["completed_at"]
