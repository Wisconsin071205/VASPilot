"""Scheduler parsing and the scheduler-vs-science separation."""

from __future__ import annotations

import pytest

from vaspilot.hpc.scheduler import (build_cancel, build_submit, parse_qstat,
                                    parse_sacct, parse_squeue)
from vaspilot.hpc.vasp import (parse_incar, parse_oszicar,
                               parse_outcar_tail, potcar_metadata,
                               scientific_status, validate_inputs)

OSZICAR_CONVERGED = """       N       E                     dE             d eps       ncg     rms          rms(c)
DAV:   1     -0.25247E+03
DAV:   2     -0.25255E+03
   1 F= -.25254434914E+03  E0= -.25254168968E+03  d E =-.252544E+03
DAV:   1     -0.25266E+03
DAV:   2     -0.25267E+03
   2 F= -.25267293475E+03  E0= -.25267177823E+03  d E =-.128586E-01
"""


class TestSchedulerParsing:
    def test_squeue(self):
        rows = parse_squeue("12345|RUNNING|0:12|8:00:00|cpu|relax01|node1\n"
                            "12346|PD|0:00|24:00:00|gpu|static1|\n")
        assert rows[0]["job_id"] == "12345"
        assert rows[0]["state"] == "RUNNING"
        assert rows[1]["state"] == "PENDING"

    def test_sacct(self):
        rows = parse_sacct("12345|COMPLETED|01:02:03|64|0:0\n"
                           "12346|TIMEOUT|24:00:01|32|1:0\n")
        assert rows[0]["state"] == "COMPLETED"
        assert rows[1]["state"] == "TIMEOUT"

    def test_qstat_full_form(self):
        raw = ("Job id: 7777.server\n    Job_Name = run01\n"
               "    job_state = R\n    exec_host = n001/0-7\n")
        rows = parse_qstat(raw)
        assert rows[0]["job_id"] == "7777"
        assert rows[0]["state"] == "RUNNING"

    def test_builders_validate_inputs(self):
        from vaspilot.core.errors import ValidationError
        assert build_submit("/root/dir", "run.sh", scheduler="slurm") == \
            ["sbatch", "--parsable", "/root/dir/run.sh"]
        with pytest.raises(ValidationError):
            build_submit("/root/dir", "run.sh; rm -rf /", scheduler="slurm")
        with pytest.raises(ValidationError):
            build_cancel("1;2", scheduler="slurm")


class TestScientificStatus:
    def test_oszicar_parsing(self):
        parsed = parse_oszicar(OSZICAR_CONVERGED, nelm=60)
        assert parsed["ionic_steps"] == 2
        assert parsed["last_ionic"]["energy_zero_ev"] == pytest.approx(
            -252.67177823, rel=1e-6)
        assert parsed["electronic_reached_nelm"] is False

    def test_nelm_exceeded_detection(self):
        incar = parse_incar("NELM = 2\nNSW = 99\n")
        oszicar = ("      1     -0.10E+02\n      2     -0.10E+02\n"
                   "   1 F= -.10000000E+02  E0= -.10000000E+02\n")
        parsed = parse_oszicar(oszicar, nelm=incar.get_int("NELM", 60))
        assert parsed["electronic_reached_nelm"] is True

    def test_outcar_convergence_and_signatures(self):
        out = parse_outcar_tail("   reached required accuracy\n")
        assert out["ionic_converged"] is True
        out = parse_outcar_tail("ZBRENT: fatal error\n")
        assert "zbrent_fatal" in out["error_signatures"]
        assert out["ionic_converged"] is False

    def test_scheduler_completed_is_not_convergence(self):
        """The central invariant: COMPLETED + unconverged != converged."""
        status = scientific_status(
            scheduler_state="COMPLETED",
            files={"INCAR": "NSW=99\nNELM=60\n",
                   "OSZICAR": OSZICAR_CONVERGED,
                   "OUTCAR": "no accuracy statement\n"})
        assert status["scheduler_done"] is True
        assert status["ionic_converged"] is False
        assert status["scientific_converged"] is False
        assert status["completed"] is False

    def test_static_run_converges_electronically(self):
        status = scientific_status(
            scheduler_state="COMPLETED",
            files={"INCAR": "NSW=0\nNELM=60\n", "OSZICAR": OSZICAR_CONVERGED,
                   "OUTCAR": "irrelevant tail\n"})
        assert status["scientific_converged"] is True
        assert status["completed"] is True

    def test_validate_inputs(self):
        result = validate_inputs({
            "INCAR": "ENCUT=520\nNSW=0\n",
            "KPOINTS": "k\n0\nGamma\n2 2 2\n0 0 0\n",
            "POSCAR": "s\n1.0\n3 0 0\n0 3 0\n0 0 3\nNa\n1\nd\n0 0 0\n",
        }, require_potcar=False)
        assert result["ok"] is True
        result = validate_inputs({"INCAR": ""}, require_potcar=False)
        assert result["ok"] is False
        assert any("missing" in e for e in result["errors"])

    def test_potcar_metadata_only(self):
        meta = potcar_metadata(
            "  TITEL  = PAW Na_sv 08Apr2002\n   ENMAX =  302.000000\n")
        assert meta["titel"].startswith("PAW Na_sv")
        assert meta["enmax_ev"] == 302.0
        # no pseudopotential body ever appears in metadata
        assert "VRHFIN" not in meta
