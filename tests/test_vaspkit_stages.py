"""阶段图：文件从哪来、依赖谁、锁哪些 INCAR 参数。"""

import pytest

from vaspilot.core.errors import ValidationError
from vaspilot.vaspkit.recipe import validate_recipe
from vaspilot.vaspkit.stages import build_campaign

ELEMENTS = ("Fe", "O")
STRUCTURE = {"file": "Fe2O3.vasp", "formula": "Fe2O3", "sha256": "ab" * 32}


def campaign(**overrides):
    raw = {"structure": STRUCTURE, "functional": "PBE", "stages": ["dos", "band"],
           "resources": {"server": "hpc1", "ntasks": 32, "walltime": "24:00:00"}}
    raw.update(overrides)
    return build_campaign(validate_recipe(raw, elements=ELEMENTS))


def stage_of(result, name):
    return next(s for s in result["stages"] if s["name"] == name)


def keys_of(stage):
    return {item["key"] for item in stage["assertions"]}


class TestGraph:
    def test_full_chain_in_order(self):
        result = campaign()
        assert [s["name"] for s in result["stages"]] == [
            "relax", "static", "band", "dos"]
        assert [s["dir"] for s in result["stages"]] == [
            "01-relax", "02-static", "03-band", "04-dos"]

    def test_first_stage_reads_the_uploaded_structure(self):
        assert stage_of(campaign(), "relax")["poscar_from"] == "input"

    def test_static_continues_from_the_relaxed_structure(self):
        assert stage_of(campaign(), "static")["poscar_from"] == "01-relax/CONTCAR"

    def test_band_and_dos_reuse_the_static_structure(self):
        result = campaign()
        assert stage_of(result, "band")["poscar_from"] == "02-static/POSCAR"
        assert stage_of(result, "dos")["poscar_from"] == "02-static/POSCAR"

    def test_band_and_dos_take_the_charge_density_from_static(self):
        result = campaign()
        assert stage_of(result, "band")["chgcar_from"] == "02-static"
        assert stage_of(result, "dos")["chgcar_from"] == "02-static"
        assert stage_of(result, "relax")["chgcar_from"] == ""

    def test_band_uses_the_high_symmetry_path_task(self):
        assert stage_of(campaign(), "band")["kpoints_task"] == 303
        assert stage_of(campaign(), "dos")["kpoints_task"] == 102

    def test_already_relaxed_structure_starts_at_static(self):
        result = campaign(stages=["band"], structure_relaxed=True)
        assert [s["name"] for s in result["stages"]] == ["static", "band"]
        assert stage_of(result, "static")["poscar_from"] == "input"

    def test_each_stage_names_the_one_it_waits_for(self):
        result = campaign()
        assert stage_of(result, "relax")["requires"] == ""
        assert stage_of(result, "static")["requires"] == "relax"
        assert stage_of(result, "band")["requires"] == "static"


class TestAssertions:
    def test_relax_locks_the_parameters_that_make_it_a_relaxation(self):
        assert keys_of(stage_of(campaign(), "relax")) >= {"ISIF", "IBRION", "NSW"}

    def test_static_writes_the_charge_density(self):
        keys = keys_of(stage_of(campaign(), "static"))
        assert {"NSW", "ICHARG", "LCHARG"} <= keys

    def test_band_and_dos_read_the_charge_density(self):
        for name in ("band", "dos"):
            items = {i["key"]: i for i in stage_of(campaign(), name)["assertions"]}
            assert items["ICHARG"]["value"] == "11"
            assert items["LORBIT"]["value"] == "11"

    def test_dos_requires_a_usable_grid(self):
        items = {i["key"]: i for i in stage_of(campaign(), "dos")["assertions"]}
        assert items["NEDOS"]["op"] == "ge"

    def test_spin_adds_ispin_to_every_stage(self):
        result = campaign(spin=True)
        for stage in result["stages"]:
            assert "ISPIN" in keys_of(stage)

    def test_hubbard_u_adds_ldau_to_every_stage(self):
        result = campaign(hubbard_u={"Fe": {"L": 2, "U": 4.0}})
        for stage in result["stages"]:
            assert "LDAU" in keys_of(stage)


class TestOverrideConflicts:
    def test_an_override_that_agrees_is_kept(self):
        result = campaign(overrides={"dos": {"NEDOS": 3000}})
        assert stage_of(result, "dos")["overrides"] == {"NEDOS": "3000"}

    def test_an_override_that_contradicts_an_assertion_is_refused(self):
        with pytest.raises(ValidationError, match="contradicts"):
            campaign(overrides={"relax": {"NSW": 0}})

    def test_the_refusal_explains_why_the_assertion_exists(self):
        with pytest.raises(ValidationError, match="protects"):
            campaign(overrides={"band": {"ICHARG": 2}})

    def test_an_unrelated_override_is_untouched(self):
        result = campaign(overrides={"relax": {"ENCUT": 520}})
        assert stage_of(result, "relax")["overrides"] == {"ENCUT": "520"}
