"""What a running VASP job looks like from its files, as real VASP writes them."""

from __future__ import annotations

import pytest

from tests.test_ui import ui  # noqa: F401  (fixture)

from vaspilot.hpc.scheduler import (parse_workdir, parse_workdirs,
                                    workdir_command, workdirs_command)
from vaspilot.hpc.vasp import live_progress, parse_oszicar

# Real OSZICAR rows carry the algorithm label (DAV:/RMM:) before the step.
OSZICAR = """       N       E                     dE             d eps       ncg     rms          rms(c)
DAV:   1     0.432915E+03    0.43292E+03   -0.27826E+04   960   0.119E+03
DAV:   2    -0.101235E+02   -0.44304E+03   -0.41424E+03  1200   0.253E+02
RMM:   3    -0.252540E+03   -0.24242E+03   -0.10000E+01   900   0.410E+01    0.120E+01
   1 F= -.25254434914E+03 E0= -.25254168968E+03  d E =-.252544E+03
RMM:   1    -0.252660E+03   -0.12000E+00   -0.5E-01   900   0.3E+00
RMM:   2    -0.252673E+03   -0.13000E-01   -0.3E-02   900   0.1E+00
   2 F= -.25267293475E+03 E0= -.25267177823E+03  d E =-.128586E-01
RMM:   1    -0.252675D+03   -0.21000D-02   -0.1E-02   900   0.2E-01
RMM:   2    -0.252676E+03   -0.35000E-04   -0.2E-04   900   0.8E-02
"""
INCAR = "SYSTEM = C60\nNSW = 200\nNELM = 60\nEDIFF = 1E-5\nEDIFFG = -0.02\nIBRION = 2\n"
OUTCAR_TAIL = """
 POSITION                                       TOTAL-FORCE (eV/Angst)
 -----------------------------------------------------------------------------------
      0.00000      0.00000      0.00000         0.030000     -0.040000      0.000000
      1.00000      1.00000      1.00000        -0.100000      0.000000      0.000000
 -----------------------------------------------------------------------------------
    total drift:                                0.000000      0.000000      0.000000

 POSITION                                       TOTAL-FORCE (eV/Angst)
 -----------------------------------------------------------------------------------
      0.00000      0.00000      0.00000         0.003000      0.004000      0.000000
      1.00000      1.00000      1.00000        -0.010000      0.000000      0.000000
 -----------------------------------------------------------------------------------
"""

TIMING = """
 General timing and accounting informations for this job:
 ========================================================

                  Total CPU time used (sec):     5201.113
                            User time (sec):     5150.002
                          System time (sec):       51.111
                         Elapsed time (sec):     5234.817
"""


class TestLabelledElectronicRows:
    def test_dav_and_rmm_rows_are_electronic_steps(self):
        parsed = parse_oszicar(OSZICAR, nelm=60)
        assert parsed["ionic_steps"] == 2
        assert [s["electronic_steps"] for s in parsed["steps"]] == [3, 2]

    def test_an_early_nelm_hit_is_counted_not_fatal(self):
        parsed = parse_oszicar(OSZICAR, nelm=3)
        assert parsed["nelm_hits"] == 1
        assert parsed["electronic_reached_nelm"] is False

    def test_nelm_on_the_last_step_is_caught_on_real_rows(self):
        parsed = parse_oszicar(OSZICAR, nelm=2)
        assert parsed["nelm_hits"] == 2
        assert parsed["electronic_reached_nelm"] is True


class TestLiveProgress:
    def progress(self, **over):
        files = {"INCAR": INCAR, "OSZICAR": OSZICAR, "OUTCAR": OUTCAR_TAIL}
        files.update(over)
        return live_progress(files)

    def test_where_the_run_is(self):
        p = self.progress()
        assert p["ionic_step"] == 2
        assert p["nsw"] == 200 and p["nelm"] == 60
        # the third ionic step is under way: two electronic rows so far
        assert p["current_electronic"] == 2
        assert p["current_de"] == pytest.approx(-3.5e-5)
        assert p["last_electronic_steps"] == 2

    def test_energy_trend(self):
        p = self.progress()
        assert p["last_e0"] == pytest.approx(-252.67177823)
        assert p["delta_e"] == pytest.approx(-252.67177823 + 252.54168968)
        assert [row["step"] for row in p["energies"]] == [1, 2]

    def test_criteria_come_from_the_incar(self):
        p = self.progress()
        assert p["ediff"] == pytest.approx(1e-5)
        assert p["ediffg"] == pytest.approx(-0.02)

    def test_vasp_defaults_when_the_incar_is_silent(self):
        p = self.progress(INCAR="NSW = 10\n")
        assert p["nelm"] == 60
        assert p["ediff"] == pytest.approx(1e-4)
        assert p["ediffg"] == pytest.approx(1e-3)  # EDIFF x 10

    def test_max_force_is_from_the_last_block(self):
        assert self.progress()["max_force"] == pytest.approx(0.01)

    def test_no_force_block_yet(self):
        assert self.progress(OUTCAR="")["max_force"] is None

    def test_a_truncated_tail_keeps_the_real_step_number(self):
        tail = "\n".join(OSZICAR.splitlines()[5:])  # starts mid-file
        p = self.progress(OSZICAR=tail.replace("   2 F=", "  57 F="))
        assert p["ionic_step"] == 57

    def test_nothing_written_yet(self):
        p = self.progress(OSZICAR="", OUTCAR="")
        assert p["ionic_step"] == 0 and p["current_electronic"] == 0
        assert p["last_e0"] is None and p["delta_e"] is None
        assert p["scientific_converged"] is False

    def test_convergence_flags_are_included(self):
        p = self.progress(OUTCAR=OUTCAR_TAIL + " reached required accuracy\n")
        assert p["ionic_converged"] is True

    def test_electronic_steps_per_ionic_step(self):
        p = self.progress()
        assert [(r["step"], r["electronic"]) for r in p["energies"]] == [(1, 3), (2, 2)]
        # 3 + 2 finished, 2 more in the step under way
        assert p["total_electronic"] == 7

    def test_a_run_that_is_still_going_has_not_finished(self):
        p = self.progress()
        assert p["finished_normally"] is False and p["elapsed_seconds"] is None

    def test_a_run_that_ended_properly(self):
        p = self.progress(OUTCAR=OUTCAR_TAIL + TIMING)
        assert p["finished_normally"] is True
        assert p["elapsed_seconds"] == pytest.approx(5234.817)

    def test_nsw_used_up_without_convergence(self):
        p = self.progress(INCAR=INCAR.replace("NSW = 200", "NSW = 2"))
        assert p["ionic_exhausted"] is True
        done = self.progress(INCAR=INCAR.replace("NSW = 200", "NSW = 2"),
                             OUTCAR=OUTCAR_TAIL + " reached required accuracy\n")
        assert done["ionic_exhausted"] is False
        assert self.progress()["ionic_exhausted"] is False


class TestWorkdir:
    def test_command_asks_both_schedulers(self):
        command = workdir_command("128870")
        assert "scontrol show job -o 128870" in command
        assert "qstat -f 128870" in command

    def test_bad_job_ids_are_refused(self):
        with pytest.raises(Exception):
            workdir_command("1; rm -rf ~")

    def test_slurm(self):
        out = ("JobId=5501 JobName=relax UserId=u(1) WorkDir=/share/home/u/C60 "
               "StdOut=/share/home/u/C60/slurm-5501.out\n__VP_PBS__\n")
        assert parse_workdir(out) == "/share/home/u/C60"

    def test_pbs_variable_list_wrapped_across_lines(self):
        out = ("__VP_PBS__\nJob Id: 128870.admin\n    Job_Name = C60_opt\n"
               "    Variable_List = PBS_O_HOME=/public/home/wuhong,PBS_O_LANG=en_US,\n"
               "\tPBS_O_LOGNAME=wuhong,PBS_O_WORKDIR=/public/home/wuhong/C60_\n"
               "\topt,PBS_O_SYSTEM=Linux\n")
        assert parse_workdir(out) == "/public/home/wuhong/C60_opt"

    def test_torque_init_work_dir_wins(self):
        out = ("__VP_PBS__\nJob Id: 7.head\n    init_work_dir = /home/u/run1\n"
               "    Variable_List = PBS_O_WORKDIR=/home/u\n")
        assert parse_workdir(out) == "/home/u/run1"

    def test_unknown_job(self):
        assert parse_workdir("__VP_PBS__\n") == ""

    def test_finished_slurm_job_from_sacct(self):
        out = "__VP_SACCT__\n/share/home/u/done-run\n__VP_PBS__\n"
        assert parse_workdir(out) == "/share/home/u/done-run"

    def test_finished_jobs_are_asked_for_too(self):
        command = workdir_command("42")
        assert "sacct -j 42" in command and "qstat -xf 42" in command

    def test_several_jobs_in_one_round_trip(self):
        command = workdirs_command(["11", "12"])
        assert command.count("scontrol show job -o") == 2
        out = ("__VP_JOB__11\nJobId=11 WorkDir=/a/one\n__VP_SACCT__\n__VP_PBS__\n"
               "__VP_JOB__12\n__VP_SACCT__\n__VP_PBS__\n")
        assert parse_workdirs(out) == {"11": "/a/one", "12": ""}

    def test_several_refuses_a_bad_id(self):
        with pytest.raises(Exception):
            workdirs_command(["11", "12;id"])


class TestGatewayPayload:
    def test_the_gateway_counts_labelled_rows_too(self):
        from vaspilot.gateway.vaspilot_gateway import vasp_progress_payload
        payload = vasp_progress_payload({"INCAR": "NELM = 3\nNSW = 200\n",
                                         "OSZICAR": OSZICAR, "OUTCAR": ""})
        assert payload["electronic_reached_nelm"] is True


RUN = "/hpc/home/tester/vaspilot-root/runs/C60_opt"


class TestThroughTheClient:
    @pytest.fixture()
    def client(self, app_with_fake):
        app, transport = app_with_fake
        files = transport.state.files["cl9"]
        files[f"{RUN}/INCAR"] = INCAR.encode()
        files[f"{RUN}/OSZICAR"] = OSZICAR.encode()
        files[f"{RUN}/OUTCAR"] = OUTCAR_TAIL.encode()
        return app.client(), transport.state

    def test_live_progress_of_a_directory(self, client):
        c, _ = client
        p = c.vasp_live(RUN, server="cl9")
        assert p["ok"] is True
        assert p["ionic_step"] == 2 and p["nsw"] == 200
        assert p["current_electronic"] == 2
        assert p["max_force"] == pytest.approx(0.01)
        assert p["files_present"] == ["INCAR", "OSZICAR", "OUTCAR"]

    def test_the_oszicar_tail_is_what_is_read(self, client):
        c, state = client
        long = "".join(f"RMM:   1  -0.1E+03  -0.1E-01\n{n} F= -.1E+03 E0= -.1E+03  d E =-.1E-02\n"
                       for n in range(1, 3001))
        state.files["cl9"][f"{RUN}/OSZICAR"] = long.encode()
        assert c.vasp_live(RUN, server="cl9")["ionic_step"] == 3000

    def test_a_directory_with_nothing_yet(self, client):
        c, _ = client
        p = c.vasp_live(RUN + "-empty", server="cl9")
        assert p["ok"] is True and p["files_present"] == []
        assert p["ionic_step"] == 0

    def test_workdir_of_a_job(self, client):
        c, state = client
        state.workdirs = {"5501": RUN}
        assert c.job_workdir("5501", server="cl9")["workdir"] == RUN
        unknown = c.job_workdir("9999", server="cl9")
        assert unknown["ok"] is False and unknown["workdir"] == ""


class TestConsoleEndpoints:
    def test_live_and_workdir(self, ui):
        from tests.test_ui import call
        files = ui["state"].files["cl9"]
        files[f"{RUN}/INCAR"] = INCAR.encode()
        files[f"{RUN}/OSZICAR"] = OSZICAR.encode()
        ui["state"].workdirs = {"5501": RUN}
        where = call(ui, "job.workdir", {"server": "cl9", "job_id": "5501"})
        assert where["workdir"] == RUN
        live = call(ui, "vasp.live", {"server": "cl9", "directory": where["workdir"]})
        assert live["ionic_step"] == 2 and live["current_electronic"] == 2

    def test_bad_job_id_is_an_error(self, ui):
        from tests.test_ui import call
        assert call(ui, "job.workdir", {"server": "cl9", "job_id": "1;id"})["ok"] is False


class TestPbsOwnJobsOnly:
    QSTAT = ("__VP_USER__wuhong\n"
             "Job Id: 128870.admin\n    Job_Name = C60_opt\n    Job_Owner = wuhong@admin\n"
             "    job_state = R\n    queue = short\n"
             "Job Id: 128842.admin\n    Job_Name = 1\n    Job_Owner = gyz@admin\n"
             "    job_state = R\n    queue = long\n")

    def test_other_users_jobs_are_dropped(self):
        from vaspilot.gateway.vaspilot_gateway import _pbs_own_jobs
        assert list(_pbs_own_jobs(self.QSTAT)) == ["128870"]

    def test_without_a_user_line_nothing_is_dropped(self):
        from vaspilot.gateway.vaspilot_gateway import _pbs_own_jobs
        raw = self.QSTAT.split("\n", 1)[1]
        assert sorted(_pbs_own_jobs(raw)) == ["128842", "128870"]


class TestLedgerRemembersWhereJobsRan:
    def test_remember_and_retry_limit(self, tmp_path):
        from vaspilot.core.jobhistory import JobLedger
        ledger = JobLedger(tmp_path)
        ledger.observe("cl9", [{"job_id": "7", "state": "RUNNING"},
                               {"job_id": "8", "state": "RUNNING"}])
        assert ledger.needs_workdir("cl9", ["7", "8", "99"]) == ["7", "8"]
        ledger.remember_workdirs("cl9", {"7": RUN, "8": ""})
        assert ledger.workdir("cl9", "7") == RUN
        assert ledger.needs_workdir("cl9", ["7", "8"]) == ["8"]
        for _ in range(JobLedger.WORKDIR_TRIES):
            ledger.remember_workdirs("cl9", {"8": ""})
        assert ledger.needs_workdir("cl9", ["8"]) == []
        # the record survives the job finishing and the state changing
        ledger.observe("cl9", [], infer_missing=True)
        row = next(r for r in ledger.merged("cl9") if r["job_id"] == "7")
        assert row["state"] == "COMPLETED" and row["workdir"] == RUN

    def test_relative_paths_are_not_recorded(self, tmp_path):
        from vaspilot.core.jobhistory import JobLedger
        ledger = JobLedger(tmp_path)
        ledger.seed_submitted("cl9", "9", workdir="runs/x")
        assert ledger.workdir("cl9", "9") == ""
        ledger.seed_submitted("cl9", "10", workdir=RUN)
        assert ledger.workdir("cl9", "10") == RUN


class TestFinishedJobsInTheConsole:
    def test_a_job_seen_running_can_be_opened_after_it_is_gone(self, ui):
        from tests.test_ui import call
        state = ui["state"]
        files = state.files["cl9"]
        files[f"{RUN}/INCAR"] = INCAR.encode()
        files[f"{RUN}/OSZICAR"] = OSZICAR.encode()
        files[f"{RUN}/OUTCAR"] = (OUTCAR_TAIL + " reached required accuracy\n"
                                  + TIMING).encode()
        state.jobs["cl9"].append({"job_id": "5601", "state": "RUNNING",
                                  "name": "C60_opt"})
        state.workdirs = {"5601": RUN}
        call(ui, "job.list", {"server": "cl9"})
        asked = len(state.workdir_asks)
        call(ui, "job.list", {"server": "cl9"})
        assert len(state.workdir_asks) == asked  # learned once, not every poll

        # the job finishes and the scheduler forgets it entirely
        state.jobs["cl9"].clear()
        state.workdirs = {}
        recent = call(ui, "job.recent", {"server": "cl9"})
        row = next(j for j in recent["jobs"] if j["job_id"] == "5601")
        assert row["state"] == "COMPLETED" and row["workdir"] == RUN

        where = call(ui, "job.workdir", {"server": "cl9", "job_id": "5601"})
        assert where["workdir"] == RUN and where["source"] == "record"
        live = call(ui, "vasp.live", {"server": "cl9", "directory": RUN})
        assert live["scientific_converged"] is True
        assert live["finished_normally"] is True
        assert live["last_e0"] == pytest.approx(-252.67177823)

    def test_an_answer_from_the_scheduler_is_remembered(self, ui):
        from tests.test_ui import call
        state = ui["state"]
        state.jobs["cl9"].append({"job_id": "5602", "state": "COMPLETED"})
        call(ui, "job.recent", {"server": "cl9"})
        state.workdirs = {"5602": RUN}
        assert call(ui, "job.workdir", {"server": "cl9",
                                        "job_id": "5602"})["source"] == "scheduler"
        state.workdirs = {}
        again = call(ui, "job.workdir", {"server": "cl9", "job_id": "5602"})
        assert again["workdir"] == RUN and again["source"] == "record"


class TestRealStartAndEnd:
    """cl9 runs Torque: a finished job is simply gone, so the ledger only
    knows a window for its end until the job's own files are read."""

    OUT = ("__VP_JOB__128870\n__VP_TZ__+0800\n"
           " executed on             LinuxIFC date 2026.09.23  16:02:55\n"
           "__VP_LAST__1790151922\n"
           "__VP_JOB__128871\n")

    def test_command_reads_the_run_directory(self):
        from vaspilot.hpc.scheduler import timing_command
        command = timing_command({"128870": "/public/home/wuhong/C60 opt"})
        assert "cd -- '/public/home/wuhong/C60 opt'" in command
        assert "executed on" in command and "*.o128870" in command

    def test_command_refuses_relative_or_odd_paths(self):
        from vaspilot.hpc.scheduler import timing_command
        for bad in ("runs/x", "/a\nrm -rf ~"):
            with pytest.raises(Exception):
                timing_command({"1": bad})
        with pytest.raises(Exception):
            timing_command({"1;id": "/a"})

    def test_parse(self):
        from datetime import datetime, timezone
        from vaspilot.hpc.scheduler import parse_timing
        found = parse_timing(self.OUT)
        start = datetime.fromtimestamp(found["128870"]["vasp_started"], timezone.utc)
        assert start.isoformat() == "2026-09-23T08:02:55+00:00"
        assert found["128870"]["last_write"] == 1790151922
        assert found["128871"] == {"vasp_started": None, "last_write": None}

    @staticmethod
    def vanished(tmp_path, last_seen="2026-09-23T08:10:00+00:00",
                 noticed="2026-09-23T08:47:00+00:00"):
        import json
        from vaspilot.core.jobhistory import JobLedger
        (tmp_path / "cl9.json").write_text(json.dumps({"128870": {
            "job_id": "128870", "name": "C60_opt", "state": "COMPLETED",
            "elapsed": "00:06:40", "assumed_end": True, "workdir": RUN,
            "first_seen": last_seen, "last_seen": last_seen,
            "completed_at": noticed}}), encoding="utf-8")
        return JobLedger(tmp_path)

    def test_the_files_settle_start_end_and_duration(self, tmp_path):
        from datetime import datetime
        ledger = self.vanished(tmp_path)
        assert ledger.needs_timing("cl9") == {"128870": RUN}
        end = datetime.fromisoformat("2026-09-23T08:25:22+00:00").timestamp()
        start = datetime.fromisoformat("2026-09-23T08:02:55+00:00").timestamp()
        ledger.remember_timing("cl9", {"128870": {"vasp_started": start,
                                                  "last_write": end}})
        row = ledger.merged("cl9")[0]
        assert row["assumed_end"] is False
        assert row["completed_at"] == "2026-09-23T08:25:22+00:00"
        assert row["completion_source"] == "files"
        assert row["noticed_at"] == "2026-09-23T08:47:00+00:00"
        assert row["started_at"] == "2026-09-23T08:02:55+00:00"
        assert row["start_source"] == "outcar"
        assert row["duration_seconds"] == 22 * 60 + 27
        assert ledger.needs_timing("cl9") == {}

    def test_a_later_run_in_the_same_directory_is_not_believed(self, tmp_path):
        from datetime import datetime
        ledger = self.vanished(tmp_path)
        later = datetime.fromisoformat("2026-09-24T10:00:00+00:00").timestamp()
        ledger.remember_timing("cl9", {"128870": {"vasp_started": later,
                                                  "last_write": later}})
        row = ledger.merged("cl9")[0]
        assert row["assumed_end"] is True and row["duration_seconds"] is None
        assert row["completed_at"] == "2026-09-23T08:47:00+00:00"
        assert ledger.needs_timing("cl9") == {}  # gives up at once

    def test_no_files_spends_a_try(self, tmp_path):
        from vaspilot.core.jobhistory import JobLedger
        ledger = self.vanished(tmp_path)
        for _ in range(JobLedger.TIMING_TRIES):
            assert ledger.needs_timing("cl9")
            ledger.remember_timing("cl9", {"128870": {}})
        assert ledger.needs_timing("cl9") == {}
        assert ledger.merged("cl9")[0]["assumed_end"] is True

    def test_an_assumed_end_has_no_duration(self, tmp_path):
        row = self.vanished(tmp_path).merged("cl9")[0]
        assert row["duration_seconds"] is None
        assert row["completion_source"] == "noticed"

    def test_a_queued_job_has_used_no_time(self, tmp_path):
        from vaspilot.core.jobhistory import JobLedger
        ledger = JobLedger(tmp_path)
        # older gateways reported the requested walltime for queued jobs
        ledger.observe("cl9", [{"job_id": "5", "state": "PENDING",
                                "elapsed": "32:00:00"}])
        assert ledger.merged("cl9")[0]["elapsed"] == ""
        ledger.observe("cl9", [{"job_id": "5", "state": "RUNNING",
                                "elapsed": "00:01:10"}])
        assert ledger.merged("cl9")[0]["elapsed"] == "00:01:10"

    def test_torque_start_and_completion_fields(self):
        from vaspilot.gateway.vaspilot_gateway import _pbs_parse_qstat_f
        raw = ("Job Id: 128875.admin\n    Job_Name = test64l\n"
               "    job_state = C\n    start_time = Wed Sep 23 19:49:28 2026\n"
               "    comp_time = Wed Sep 23 20:30:01 2026\n"
               "    mtime = Wed Sep 23 20:30:05 2026\n")
        job = _pbs_parse_qstat_f(raw)["128875"]
        assert job["started_at"].startswith("2026-09-23T19:49:28")
        assert job["completed_at"].startswith("2026-09-23T20:30:01")

    def test_the_console_settles_a_vanished_job(self, ui):
        from datetime import datetime
        from tests.test_ui import call
        state = ui["state"]
        state.jobs["cl9"].append({"job_id": "5701", "state": "RUNNING",
                                  "name": "C60_opt"})
        state.workdirs = {"5701": RUN}
        call(ui, "job.list", {"server": "cl9"})
        call(ui, "job.recent", {"server": "cl9"})
        state.jobs["cl9"].clear()
        now = datetime.now().timestamp()
        state.timings = {"5701": (now - 600, now - 1)}
        first = call(ui, "job.recent", {"server": "cl9"})
        row = next(j for j in first["jobs"] if j["job_id"] == "5701")
        assert row["assumed_end"] is False and row["completion_source"] == "files"
        assert 590 <= row["duration_seconds"] <= 600
        asked = len(state.timing_asks)
        call(ui, "job.recent", {"server": "cl9"})
        assert len(state.timing_asks) == asked  # settled once
