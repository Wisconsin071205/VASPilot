"""Plan determinism, hash invalidation, and the full run/resume lifecycle."""

from __future__ import annotations

import json

import pytest

from vaspilot.core.errors import ApprovalError, ValidationError
from vaspilot.core.hashing import file_sha256, text_sha256
from vaspilot.workflow.plan import build_plan, plan_files_hash, verify_plan_integrity

REMOTE_DIR = "/hpc/home/tester/vaspilot-root/runs/case-1"


def spec(from_dir, server="cl9", remote_dir=REMOTE_DIR):
    return {"from_dir": str(from_dir), "server": server,
            "remote_dir": remote_dir, "ntasks": 16, "walltime": "08:00:00"}


class TestPlanDeterminism:
    def test_same_inputs_same_hash(self, vasp_inputs):
        one = build_plan(from_dir=vasp_inputs, server="cl9",
                         remote_dir=REMOTE_DIR)
        two = build_plan(from_dir=vasp_inputs, server="cl9",
                         remote_dir=REMOTE_DIR)
        assert one["plan_hash"] == two["plan_hash"]
        assert one["plan_id"] == two["plan_id"] == one["plan_hash"][:16]

    def test_changed_file_changes_hash(self, vasp_inputs):
        before = build_plan(from_dir=vasp_inputs, server="cl9",
                            remote_dir=REMOTE_DIR)["plan_hash"]
        (vasp_inputs / "INCAR").write_text("SYSTEM = changed\n", encoding="utf-8")
        after = build_plan(from_dir=vasp_inputs, server="cl9",
                           remote_dir=REMOTE_DIR)["plan_hash"]
        assert before != after

    def test_changed_server_changes_hash(self, vasp_inputs):
        one = build_plan(from_dir=vasp_inputs, server="cl9",
                         remote_dir=REMOTE_DIR)["plan_hash"]
        two = build_plan(from_dir=vasp_inputs, server="other",
                         remote_dir=REMOTE_DIR)["plan_hash"]
        assert one != two

    def test_tampered_plan_rejected(self, vasp_inputs):
        plan = build_plan(from_dir=vasp_inputs, server="cl9",
                          remote_dir=REMOTE_DIR)
        plan["steps"].append({"id": "evil", "type": "mkdir", "path": "/etc"})
        with pytest.raises(ValidationError, match="modified"):
            verify_plan_integrity(plan)

    def test_files_listed_with_sha256(self, vasp_inputs):
        plan = build_plan(from_dir=vasp_inputs, server="cl9",
                         remote_dir=REMOTE_DIR)
        names = {entry["name"] for entry in plan["files"]}
        assert {"INCAR", "KPOINTS", "POSCAR", "POTCAR", "run.job.sh"} <= names
        incar = next(e for e in plan["files"] if e["name"] == "INCAR")
        assert incar["sha256"] == file_sha256(vasp_inputs / "INCAR")
        assert plan["job_script_content"].startswith("#!/bin/bash")
        assert "reached required accuracy" not in json.dumps(plan["risk_summary"])
        assert plan["risk_summary"]["destructive"].startswith("nothing")

    def test_missing_required_input_rejected(self, tmp_path):
        (tmp_path / "INCAR").write_text("x=1\n", encoding="utf-8")
        with pytest.raises(ValidationError, match="missing required file"):
            build_plan(from_dir=tmp_path, server="cl9", remote_dir=REMOTE_DIR)


class TestWorkflowLifecycle:
    def _prepare_and_approve(self, app, vasp_inputs, **spec_over):
        engine = app.engine()
        s = spec(vasp_inputs, **spec_over)
        prepared = engine.prepare(s)
        approved = engine.approve(prepared["plan_id"],
                                  stdin_lines=[f"approve {prepared['plan_id']}"])
        return prepared, approved

    def test_full_run_converged(self, app_with_fake, vasp_inputs):
        app, transport = app_with_fake
        # the fake server will host the uploaded run; make it converge
        prepared, approved = self._prepare_and_approve(app, vasp_inputs)
        remote = prepared["plan"]["remote_dir"]
        # pre-seed results the scheduler "produces": job completes and files
        # exist only after submit — simulate by planting CONTCAR/OSZICAR/OUTCAR
        # as what the finished run leaves behind (fake server returns them)
        state = transport.state
        state.files["cl9"][f"{remote}/OSZICAR"] = (
            b"      1     -0.1000E+02\n"
            b"   1 F= -.10000000E+02  E0= -.10000000E+02\n")
        state.files["cl9"][f"{remote}/OUTCAR"] = b"reached required accuracy\n"
        state.files["cl9"][f"{remote}/CONTCAR"] = b"final structure\n"
        result = app.engine().run(prepared["plan_id"],
                                  approved["approval_ref"], poll_seconds=0)
        assert result["status"] == "completed"
        assert result["scientific_converged"] is True
        assert result["attempts"][0]["status"] == "completed"
        step_types = [row["type"] for row in result["attempts"][0]["steps"]]
        assert step_types == ["mkdir", "upload", "upload", "upload", "upload",
                              "upload", "validate", "submit", "monitor",
                              "progress", "download", "parse"]

    def test_scheduler_done_but_unconverged_needs_review(self, app_with_fake,
                                                         vasp_inputs):
        app, transport = app_with_fake
        prepared, approved = self._prepare_and_approve(app, vasp_inputs)
        remote = prepared["plan"]["remote_dir"]
        state = transport.state
        state.files["cl9"][f"{remote}/OSZICAR"] = (
            b"      1     -0.1E+02\n      2     -0.1E+02\n"
            b"   1 F= -.10000000E+02  E0= -.10000000E+02\n")
        state.files["cl9"][f"{remote}/OUTCAR"] = b"no accuracy line\n"
        result = app.engine().run(prepared["plan_id"],
                                  approved["approval_ref"], poll_seconds=0)
        # scheduler COMPLETED is NOT scientific convergence
        assert result["scheduler_state"] == "COMPLETED"
        assert result["scientific_converged"] is False
        assert result["status"] == "needs_review"

    def test_approval_phrase_required(self, app_with_fake, vasp_inputs):
        app, _ = app_with_fake
        prepared = app.engine().prepare(spec(vasp_inputs))
        with pytest.raises(ApprovalError, match="phrase"):
            app.engine().approve(prepared["plan_id"],
                                 stdin_lines=["approve wrong-id-here"])

    def test_run_without_approval_rejected(self, app_with_fake, vasp_inputs):
        app, _ = app_with_fake
        prepared = app.engine().prepare(spec(vasp_inputs))
        with pytest.raises(ApprovalError):
            app.engine().run(prepared["plan_id"], "garbage-token")

    def test_approval_replay_for_second_run(self, app_with_fake, vasp_inputs):
        app, transport = app_with_fake
        prepared, approved = self._prepare_and_approve(app, vasp_inputs)
        remote = prepared["plan"]["remote_dir"]
        transport.state.files["cl9"][f"{remote}/OSZICAR"] = (
            b"   1 F= -.10000000E+02  E0= -.10000000E+02\n")
        transport.state.files["cl9"][f"{remote}/OUTCAR"] = \
            b"reached required accuracy\n"
        engine = app.engine()
        first = engine.run(prepared["plan_id"], approved["approval_ref"],
                           poll_seconds=0)
        assert first["status"] == "completed"
        # wipe run state to force a "new run" with the same token
        engine.run_path(prepared["plan_id"]).unlink()
        with pytest.raises(ApprovalError, match="already used"):
            engine.run(prepared["plan_id"], approved["approval_ref"],
                       poll_seconds=0)

    def test_plan_change_requires_new_approval(self, app_with_fake,
                                               vasp_inputs):
        app, _ = app_with_fake
        engine = app.engine()
        prepared, approved = self._prepare_and_approve(app, vasp_inputs)
        # mutate the input file and re-prepare -> different plan entirely
        (vasp_inputs / "INCAR").write_text("SYSTEM = changed\n", encoding="utf-8")
        other = engine.prepare(spec(vasp_inputs))
        assert other["plan_id"] != prepared["plan_id"]
        # the old approval is bound to the old plan hash
        with pytest.raises((ApprovalError, ValidationError)):
            engine.run(other["plan_id"], approved["approval_ref"])

    def test_resume_after_failure_new_attempt(self, app_with_fake, vasp_inputs):
        app, transport = app_with_fake
        engine = app.engine()
        prepared, approved = self._prepare_and_approve(app, vasp_inputs)
        remote = prepared["plan"]["remote_dir"]
        # sabotage: validation fails because the remote validate returns an
        # error when INCAR is missing (nothing uploaded yet is fine — but make
        # upload fail by pre-creating a conflicting remote file)
        transport.state.files["cl9"][f"{remote}/KPOINTS"] = b"stale bytes\n"
        result = engine.run(prepared["plan_id"], approved["approval_ref"],
                            poll_seconds=0)
        assert result["status"] == "failed"
        assert result["failure"]["step"].startswith("upload:")
        assert len(result["attempts"]) == 1
        # clear the collision, then resume: a NEW attempt completes the run
        transport.state.files["cl9"].pop(f"{remote}/KPOINTS")
        transport.state.files["cl9"][f"{remote}/OSZICAR"] = (
            b"   1 F= -.10000000E+02  E0= -.10000000E+02\n")
        transport.state.files["cl9"][f"{remote}/OUTCAR"] = \
            b"reached required accuracy\n"
        resumed = engine.resume(prepared["plan_id"], approved["approval_ref"],
                                poll_seconds=0)
        assert resumed["status"] == "completed"
        assert len(resumed["attempts"]) == 2
        assert resumed["attempts"][0]["status"] == "failed"
        assert resumed["attempts"][1]["status"] == "completed"

    def test_uploads_verify_local_file_hashes(self, app_with_fake, vasp_inputs):
        app, transport = app_with_fake
        engine = app.engine()
        prepared, approved = self._prepare_and_approve(app, vasp_inputs)
        # mutate a local input AFTER approval: the engine must refuse
        (vasp_inputs / "INCAR").write_text("SYSTEM = mutated post-approval\n",
                                           encoding="utf-8")
        result = engine.run(prepared["plan_id"], approved["approval_ref"],
                            poll_seconds=0)
        assert result["status"] == "failed"
        assert "changed since approval" in result["failure"]["detail"]
