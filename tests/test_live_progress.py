"""What a running VASP job looks like from its files, as real VASP writes them."""

from __future__ import annotations

import pytest

from tests.test_ui import ui  # noqa: F401  (fixture)

from vaspilot.hpc.scheduler import parse_workdir, workdir_command
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


class TestLabelledElectronicRows:
    def test_dav_and_rmm_rows_are_electronic_steps(self):
        parsed = parse_oszicar(OSZICAR, nelm=60)
        assert parsed["ionic_steps"] == 2
        assert [s["electronic_steps"] for s in parsed["steps"]] == [3, 2]

    def test_nelm_is_caught_on_real_rows(self):
        parsed = parse_oszicar(OSZICAR, nelm=3)
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
