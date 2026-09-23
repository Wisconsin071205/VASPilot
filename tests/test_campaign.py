"""A campaign end to end: plan, approve, and tick through the fake HPC."""

from __future__ import annotations

import json

import pytest

from tests.conftest import FAKE_INCAR_TEMPLATES, ROOT
from vaspilot.core.config import ServerEntry
from vaspilot.core.errors import AuthRequiredError, ValidationError
from vaspilot.core.hashing import text_sha256
from vaspilot.hpc.vasp import parse_incar
from vaspilot.vaspkit.verify import check_incar
from vaspilot.workflow.campaign import (CampaignRunner, CampaignStore,
                                        apply_settings, campaign_dir,
                                        plan_campaign, same_cell,
                                        stage_settings, view)

SI = ("Si2\n1.0\n0 2.715 2.715\n2.715 0 2.715\n2.715 2.715 0\nSi\n2\nDirect\n"
      "0 0 0\n0.25 0.25 0.25\n")
FEO = ("FeO\n1.0\n4.3 0 0\n0 4.3 0\n0 0 4.3\nFe O\n1 1\nDirect\n"
       "0 0 0\n0.5 0.5 0.5\n")
VASP_FILE = SI + "\n帮我算一下能带和态密度，PBE\n"
READY = {"command": "/opt/vaspkit/bin/vaspkit", "mode": "stdin",
         "version": "VASPKIT 1.4.1", "ready": True}
ENTRY = ServerEntry(name="cl9", target="user@cl9", remote_root=ROOT,
                    scheduler="slurm")


def recipe(**over):
    raw = {"functional": "PBE", "stages": ["band", "dos"],
           "resources": {"ntasks": 16, "walltime": "12:00:00"}}
    raw.update(over)
    return raw


def planned(**over):
    return plan_campaign(vasp_text=VASP_FILE, file_name="Si.vasp",
                         recipe=recipe(**over), server_entry=ENTRY,
                         profile=READY)


def stage(result, name):
    return next(s for s in result["campaign"]["stages"] if s["name"] == name)


# ----------------------------------------------------------------- settings
class TestSettings:
    def test_static_writes_its_defining_values(self):
        settings = stage(planned(), "static")["settings"]
        assert settings["NSW"] == "0"
        assert settings["ICHARG"] == "2"
        assert settings["LCHARG"] == ".TRUE."

    def test_relax_gets_a_step_budget_that_an_override_can_change(self):
        assert stage(planned(), "relax")["settings"]["NSW"] == "200"
        tuned = planned(overrides={"relax": {"NSW": "80"}})
        assert stage(tuned, "relax")["settings"]["NSW"] == "80"

    def test_band_and_dos_are_built_on_the_static_template(self):
        band = stage(planned(), "band")["settings"]
        dos = stage(planned(), "dos")["settings"]
        assert band["ICHARG"] == dos["ICHARG"] == "11"
        assert band["LORBIT"] == dos["LORBIT"] == "11"
        assert dos["NEDOS"] == "2001"

    def test_pbesol_switches_the_gga(self):
        assert stage(planned(functional="PBEsol"), "static")["settings"]["GGA"] == "PS"

    def test_unsupported_functional_is_refused(self):
        with pytest.raises(ValidationError, match="PBE or PBEsol"):
            planned(functional="HSE06")

    def test_spin_sets_ispin_everywhere(self):
        result = planned(spin=True)
        assert all(s["settings"]["ISPIN"] == "2"
                   for s in result["campaign"]["stages"])

    def test_hubbard_u_follows_the_poscar_species_order(self):
        result = plan_campaign(
            vasp_text=FEO, file_name="FeO.vasp",
            recipe=recipe(hubbard_u={"Fe": {"L": 2, "U": 4.0, "J": 0.0}}),
            server_entry=ENTRY, profile=READY)
        settings = stage(result, "static")["settings"]
        assert settings["LDAU"] == ".TRUE."
        assert settings["LDAUL"] == "2 -1"
        assert settings["LDAUU"] == "4 0"
        assert settings["LDAUJ"] == "0 0"
        assert settings["LMAXMIX"] == "4"

    def test_a_value_that_could_inject_a_line_is_refused(self):
        graph_stage = {"name": "static", "assertions": [], "overrides":
                       {"ENCUT": "520\nLCHARG = .FALSE."}}
        with pytest.raises(ValidationError, match="may only hold"):
            stage_settings(graph_stage, {"functional": "PBE", "hubbard_u": {}},
                           ("Si",))


class TestApplySettings:
    def test_settings_replace_the_template_values(self):
        text = apply_settings(FAKE_INCAR_TEMPLATES["ST"],
                              {"LCHARG": ".TRUE.", "ICHARG": "11"})
        values = parse_incar(text).values
        assert values["LCHARG"] == ".TRUE."
        assert values["ICHARG"] == "11"
        assert values["ISTART"] == "0"  # its neighbour on the same line stays

    def test_no_key_appears_twice(self):
        text = apply_settings(FAKE_INCAR_TEMPLATES["ST"], {"ICHARG": "11"})
        assert text.upper().count("ICHARG") == 1

    def test_lowercase_template_keys_are_caught(self):
        text = apply_settings("nsw = 5\nencut = 400\n", {"NSW": "0"})
        assert "nsw = 5" not in text
        assert "encut = 400" in text

    def test_comment_only_lines_survive(self):
        text = apply_settings("# made by VASPKIT\nNSW = 5\n", {"NSW": "0"})
        assert text.startswith("# made by VASPKIT\n")

    def test_result_passes_its_own_assertions(self):
        settings = {"NSW": "0", "ICHARG": "2", "LCHARG": ".TRUE."}
        text = apply_settings(FAKE_INCAR_TEMPLATES["ST"], settings)
        report = check_incar(text, [{"key": k, "op": "eq", "value": v}
                                    for k, v in settings.items()])
        assert report["ok"], report


# --------------------------------------------------------------------- plan
class TestPlan:
    def test_id_is_the_hash_prefix(self):
        result = planned()
        assert result["campaign_id"] == result["campaign_hash"][:16]

    def test_structure_block_is_computed_not_trusted(self):
        result = planned(structure={"file": "x", "formula": "Au",
                                    "sha256": "0" * 64})
        structure = result["campaign"]["structure"]
        assert structure["formula"] == "Si2"
        assert structure["sha256"] == text_sha256(SI)
        assert structure["poscar"] == SI

    def test_annotation_is_kept_for_the_record(self):
        assert "能带" in planned()["campaign"]["annotation"]

    def test_every_stage_has_a_frozen_job_script(self):
        for item in planned()["campaign"]["stages"]:
            assert item["job_script"].startswith("#!/bin/bash")
            assert item["job_script_sha256"] == text_sha256(item["job_script"])
            assert f"--job-name=Si2-{item['name']}" in item["job_script"]

    def test_server_comes_from_the_entry(self):
        result = planned(resources={"server": "evil", "ntasks": 4})
        assert result["campaign"]["server"] == "cl9"

    def test_profile_must_be_ready(self):
        with pytest.raises(ValidationError, match="vaspkit_doctor"):
            plan_campaign(vasp_text=VASP_FILE, file_name="Si.vasp",
                          recipe=recipe(), server_entry=ENTRY,
                          profile={"ready": False})

    def test_server_needs_a_remote_root(self):
        bare = ServerEntry(name="cl9", target="user@cl9", remote_root="")
        with pytest.raises(ValidationError, match="remote_root"):
            plan_campaign(vasp_text=VASP_FILE, file_name="Si.vasp",
                          recipe=recipe(), server_entry=bare, profile=READY)


class TestSameCell:
    def test_permuted_axes_are_the_same_cell(self):
        permuted = SI.replace("0 2.715 2.715\n2.715 0 2.715\n",
                              "2.715 0 2.715\n0 2.715 2.715\n")
        assert same_cell(SI, permuted)

    def test_a_conventional_cell_is_not_the_primitive_one(self):
        conventional = ("Si8\n1.0\n5.43 0 0\n0 5.43 0\n0 0 5.43\nSi\n8\nDirect\n"
                        + "0 0 0\n" * 8)
        assert not same_cell(conventional, SI)


# -------------------------------------------------------------------- store
class TestStore:
    def test_create_then_load(self, tmp_path):
        store = CampaignStore(tmp_path)
        record = store.create(planned())
        assert record["state"]["status"] == "awaiting_approval"
        assert set(record["state"]["stages"]) == {"relax", "static", "band", "dos"}
        assert store.load(record["campaign_id"])["campaign_hash"] == \
            record["campaign_hash"]

    def test_a_modified_campaign_does_not_load(self, tmp_path):
        store = CampaignStore(tmp_path)
        record = store.create(planned())
        path = store.path(record["campaign_id"])
        data = json.loads(path.read_text(encoding="utf-8"))
        data["campaign"]["stages"][0]["settings"]["NSW"] = "0"
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValidationError, match="modified"):
            store.load(record["campaign_id"])

    def test_approve_once(self, tmp_path):
        store = CampaignStore(tmp_path)
        campaign_id = store.create(planned())["campaign_id"]
        record = store.approve(campaign_id, via="ui")
        assert record["state"]["status"] == "running"
        assert record["state"]["approval"]["campaign_hash"] == record["campaign_hash"]
        with pytest.raises(ValidationError, match="not awaiting"):
            store.approve(campaign_id, via="ui")

    def test_reject(self, tmp_path):
        store = CampaignStore(tmp_path)
        campaign_id = store.create(planned())["campaign_id"]
        assert store.reject(campaign_id)["state"]["status"] == "rejected"


# ------------------------------------------------------------------- runner
@pytest.fixture()
def chain(app_with_fake, tmp_path):
    app, transport = app_with_fake
    store = CampaignStore(tmp_path / "campaigns")
    runner = CampaignRunner(store=store, client=app.client())

    def start(**over):
        record = store.create(planned(**over))
        store.approve(record["campaign_id"], via="test")
        return record["campaign_id"]

    return store, runner, transport.state, start


def files_of(state):
    return state.files["cl9"]


def stage_dir(store, campaign_id, name):
    record = store.load(campaign_id)
    item = next(s for s in record["campaign"]["stages"] if s["name"] == name)
    return f"{campaign_dir(record)}/{item['dir']}"


def finish(state, where, *, converged=True, relax=False):
    """Leave behind what a finished VASP run writes."""
    files = files_of(state)
    files[f"{where}/OSZICAR"] = (
        b"       N       E                     dE             d eps\n"
        b"DAV:   1    -0.1E+02\n"
        b"   1 F= -.10000000E+02  E0= -.10000000E+02  d E =-.1E-09\n")
    accuracy = b"reached required accuracy\n" if converged else b"still going\n"
    files[f"{where}/OUTCAR"] = accuracy if relax else b"done\n"
    if relax:
        files[f"{where}/CONTCAR"] = files[f"{where}/POSCAR"]
    files[f"{where}/CHGCAR"] = b"charge density\n"


def run_until(runner, campaign_id, statuses, limit=40):
    for _ in range(limit):
        record = runner.tick(campaign_id)
        if record["state"]["status"] in statuses:
            return record
    raise AssertionError(f"campaign never reached {statuses}: "
                         f"{record['state']['status']}")


def submits(state):
    return [job for job in state.jobs["cl9"]]


class TestRunner:
    def test_nothing_happens_before_approval(self, chain):
        store, runner, state, _ = chain
        campaign_id = store.create(planned())["campaign_id"]
        runner.tick(campaign_id)
        assert submits(state) == []

    def test_the_whole_chain_runs_in_dependency_order(self, chain):
        store, runner, state, start = chain
        campaign_id = start()
        order = []
        for _ in range(40):
            record = runner.tick(campaign_id)
            rows = record["state"]["stages"]
            for name, row in rows.items():
                if row["status"] == "running" and name not in order:
                    order.append(name)
                    finish(state, row["remote_dir"], relax=name == "relax")
            if record["state"]["status"] != "running":
                break
        assert record["state"]["status"] == "completed", view(record)
        assert order[:2] == ["relax", "static"]
        assert set(order[2:]) == {"band", "dos"}
        assert len(submits(state)) == 4

    def test_band_and_dos_start_in_the_same_tick(self, chain):
        store, runner, state, start = chain
        campaign_id = start()
        for _ in range(40):
            record = runner.tick(campaign_id)
            rows = record["state"]["stages"]
            if rows["band"]["status"] == "running":
                assert rows["dos"]["status"] == "running"
                return
            for name in ("relax", "static"):
                if rows[name]["status"] == "running":
                    finish(state, rows[name]["remote_dir"], relax=name == "relax")
        raise AssertionError("band never started")

    def test_stage_one_runs_exactly_the_approved_structure(self, chain):
        store, runner, state, start = chain
        campaign_id = start()
        record = runner.tick(campaign_id)
        row = record["state"]["stages"]["relax"]
        assert row["status"] == "running"
        assert row["hashes"]["POSCAR"] == record["campaign"]["structure"]["sha256"]
        assert set(row["hashes"]) == {"POSCAR", "INCAR", "KPOINTS", "POTCAR"}

    def test_the_approved_settings_beat_the_vaspkit_template(self, chain):
        store, runner, state, start = chain
        campaign_id = start()
        runner.tick(campaign_id)
        where = stage_dir(store, campaign_id, "relax")
        values = parse_incar(files_of(state)[f"{where}/INCAR"].decode()).values
        assert values["ISIF"] == "3"      # template said 2
        assert values["NSW"] == "200"     # template said 300
        assert values["ENCUT"] == "520"   # untouched template value

    def test_later_stages_take_the_previous_structure(self, chain):
        store, runner, state, start = chain
        campaign_id = start()
        runner.tick(campaign_id)
        relax = stage_dir(store, campaign_id, "relax")
        finish(state, relax, relax=True)
        files_of(state)[f"{relax}/CONTCAR"] = SI.replace("Si2", "relaxed").encode()
        for _ in range(3):
            record = runner.tick(campaign_id)
        assert record["state"]["stages"]["static"]["status"] == "running"
        static = stage_dir(store, campaign_id, "static")
        assert files_of(state)[f"{static}/POSCAR"].startswith(b"relaxed")

    def test_band_reads_the_high_symmetry_path_and_the_charge(self, chain):
        store, runner, state, start = chain
        campaign_id = start()
        for _ in range(40):
            record = runner.tick(campaign_id)
            rows = record["state"]["stages"]
            if rows["band"]["status"] == "running":
                break
            for name in ("relax", "static"):
                if rows[name]["status"] == "running":
                    finish(state, rows[name]["remote_dir"], relax=name == "relax")
        band = stage_dir(store, campaign_id, "band")
        assert files_of(state)[f"{band}/KPOINTS"].startswith(b"K-Path")
        assert files_of(state)[f"{band}/CHGCAR"] == b"charge density\n"
        assert "CHGCAR" in rows["band"]["hashes"]

    def test_unconverged_relax_holds_everything_after_it(self, chain):
        store, runner, state, start = chain
        campaign_id = start()
        runner.tick(campaign_id)
        finish(state, stage_dir(store, campaign_id, "relax"),
               relax=True, converged=False)
        record = run_until(runner, campaign_id, ("needs_review",))
        assert record["state"]["stages"]["relax"]["status"] == "needs_review"
        assert record["state"]["stages"]["static"]["status"] == "waiting"
        assert len(submits(state)) == 1

    def test_an_incar_that_does_not_match_blocks_before_submitting(
            self, chain, monkeypatch):
        store, runner, state, start = chain
        campaign_id = start()
        # a server-side write that silently keeps the old file
        monkeypatch.setattr(runner.client, "write_file",
                            lambda *a, **k: {"ok": True})
        record = runner.tick(campaign_id)
        row = record["state"]["stages"]["relax"]
        assert row["status"] == "blocked"
        assert record["state"]["status"] == "blocked"
        assert {f["key"] for f in row["verify"]["failures"]} >= {"ISIF"}
        assert submits(state) == []

    def test_vaspkit_that_writes_nothing_fails_the_stage(self, chain):
        store, runner, state, start = chain
        state.vaspkit_fail = {"103"}
        campaign_id = start()
        record = runner.tick(campaign_id)
        row = record["state"]["stages"]["relax"]
        assert row["status"] == "failed"
        assert "vaspkit-*.log" in row["error"]
        assert submits(state) == []

    def test_a_non_primitive_cell_is_stopped_before_any_compute(self, chain):
        store, runner, state, start = chain
        state.vaspkit_primcell = FEO.encode()
        campaign_id = start()
        record = runner.tick(campaign_id)
        row = record["state"]["stages"]["relax"]
        assert row["status"] == "blocked"
        assert "PRIMCELL.vasp" in row["error"]
        assert submits(state) == []

    def test_no_primitive_check_without_a_band_stage(self, chain):
        store, runner, state, start = chain
        state.vaspkit_primcell = FEO.encode()
        campaign_id = start(stages=["dos"])
        record = runner.tick(campaign_id)
        assert record["state"]["stages"]["relax"]["status"] == "running"

    def test_a_dropped_connection_is_waited_out(self, chain, monkeypatch):
        store, runner, state, start = chain
        campaign_id = start()
        runner.tick(campaign_id)

        def offline(*args, **kwargs):
            raise AuthRequiredError("cl9 has no reusable SSH session")

        real = runner.client.job_state
        monkeypatch.setattr(runner.client, "job_state", offline)
        record = runner.tick(campaign_id)
        assert record["state"]["status"] == "running"
        assert record["state"]["stages"]["relax"]["status"] == "running"
        assert "disconnected" in record["state"]["note"]
        monkeypatch.setattr(runner.client, "job_state", real)
        finish(state, stage_dir(store, campaign_id, "relax"), relax=True)
        for _ in range(3):
            record = runner.tick(campaign_id)
        assert record["state"]["stages"]["relax"]["status"] == "converged"
        assert "note" not in record["state"]

    def test_abort_cancels_the_running_job(self, chain):
        store, runner, state, start = chain
        campaign_id = start()
        runner.tick(campaign_id)
        record = runner.abort(campaign_id)
        assert record["state"]["status"] == "aborted"
        assert all(row["status"] == "aborted"
                   for row in record["state"]["stages"].values())
        cancels = [call for call in runner.client.transport.calls
                   if call and call[0] == "cancel"]
        assert len(cancels) == 1
        assert runner.tick(campaign_id)["state"]["status"] == "aborted"

    def test_an_approval_for_another_campaign_blocks_it(self, chain):
        store, runner, state, start = chain
        campaign_id = start()
        path = store.path(campaign_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["state"]["approval"]["campaign_hash"] = "f" * 64
        path.write_text(json.dumps(data), encoding="utf-8")
        record = runner.tick(campaign_id)
        assert record["state"]["status"] == "blocked"
        assert submits(state) == []

    def test_view_is_what_the_ui_shows(self, chain):
        store, runner, state, start = chain
        campaign_id = start()
        shown = view(runner.tick(campaign_id))
        assert shown["formula"] == "Si2"
        assert [s["label"] for s in shown["stages"]] == [
            "结构优化", "静态自洽", "能带", "态密度"]
        assert shown["stages"][0]["status"] == "running"
        assert shown["stages"][0]["job_id"]


class TestLibraryBinding:
    LIB = "/data/pot/PBE.54"

    def test_the_library_is_part_of_what_gets_approved(self):
        with_lib = plan_campaign(
            vasp_text=VASP_FILE, file_name="Si.vasp", recipe=recipe(),
            server_entry=ENTRY, profile={**READY, "potcar_library": self.LIB},
            potcar_library=self.LIB)
        assert with_lib["campaign"]["vaspkit"]["potcar_library"] == self.LIB
        assert planned()["campaign"]["vaspkit"]["potcar_library"] == ""

    def test_a_probe_of_another_setting_is_refused(self):
        with pytest.raises(ValidationError, match="vaspkit_doctor again"):
            plan_campaign(vasp_text=VASP_FILE, file_name="Si.vasp",
                          recipe=recipe(), server_entry=ENTRY, profile=READY,
                          potcar_library=self.LIB)

    def test_the_runner_generates_potcar_from_the_approved_library(self, chain):
        store, runner, state, _ = chain
        record = store.create(plan_campaign(
            vasp_text=VASP_FILE, file_name="Si.vasp", recipe=recipe(),
            server_entry=ENTRY, profile={**READY, "potcar_library": self.LIB},
            potcar_library=self.LIB))
        store.approve(record["campaign_id"], via="test")
        assert runner.tick(record["campaign_id"])["state"]["stages"]["relax"][
            "status"] == "running"
        script = state.vaspkit_scripts[-1]
        assert f"lib={self.LIB}" in script
        assert 'HOME="$vh"' in script
