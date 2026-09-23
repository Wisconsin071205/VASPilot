"""配方是模型唯一的产出物，所以这里是半懂的意图被挡下来的地方。"""

import pytest

from vaspilot.core.errors import ValidationError
from vaspilot.vaspkit.recipe import (DEFAULT_KSPACING, expand_stages,
                                     validate_recipe)

ELEMENTS = ("Fe", "O")
STRUCTURE = {"file": "Fe2O3.vasp", "formula": "Fe2O3", "sha256": "ab" * 32}


def recipe(**overrides):
    base = {"structure": STRUCTURE, "functional": "PBE",
            "stages": ["relax"],
            "resources": {"server": "hpc1", "ntasks": 32,
                          "walltime": "24:00:00"}}
    base.update(overrides)
    return base


class TestExpandStages:
    def test_band_pulls_in_static_and_relax(self):
        assert expand_stages(["band"]) == ("relax", "static", "band")

    def test_canonical_order_regardless_of_input_order(self):
        assert expand_stages(["dos", "relax"]) == ("relax", "static", "dos")

    def test_duplicates_collapse(self):
        assert expand_stages(["relax", "relax"]) == ("relax",)

    def test_structure_relaxed_drops_only_the_implied_relax(self):
        assert expand_stages(["band"], structure_relaxed=True) == ("static", "band")

    def test_structure_relaxed_keeps_an_explicit_relax(self):
        assert expand_stages(["relax", "band"], structure_relaxed=True) == (
            "relax", "static", "band")


class TestStages:
    def test_unknown_stage_is_refused(self):
        with pytest.raises(ValidationError, match="unknown stage"):
            validate_recipe(recipe(stages=["phonon"]), elements=ELEMENTS)

    def test_empty_stage_list_is_refused(self):
        with pytest.raises(ValidationError, match="at least one stage"):
            validate_recipe(recipe(stages=[]), elements=ELEMENTS)


class TestFunctional:
    def test_case_is_normalised(self):
        assert validate_recipe(recipe(functional="pbe"),
                               elements=ELEMENTS)["functional"] == "PBE"

    def test_unknown_functional_lists_the_supported_ones(self):
        with pytest.raises(ValidationError, match="unknown functional"):
            validate_recipe(recipe(functional="B3LYP"), elements=ELEMENTS)


class TestKspacing:
    def test_defaults_fill_in(self):
        result = validate_recipe(recipe(stages=["relax"]), elements=ELEMENTS)
        assert result["kspacing"]["relax"] == DEFAULT_KSPACING["relax"]

    def test_given_value_wins(self):
        result = validate_recipe(recipe(kspacing={"relax": 0.05}),
                                 elements=ELEMENTS)
        assert result["kspacing"]["relax"] == 0.05

    def test_out_of_range_is_refused(self):
        with pytest.raises(ValidationError, match="outside"):
            validate_recipe(recipe(kspacing={"relax": 5.0}), elements=ELEMENTS)

    def test_non_numeric_is_refused(self):
        with pytest.raises(ValidationError, match="not a number"):
            validate_recipe(recipe(kspacing={"relax": "dense"}),
                            elements=ELEMENTS)


class TestHubbardU:
    def test_accepted_for_an_element_in_the_structure(self):
        result = validate_recipe(
            recipe(hubbard_u={"Fe": {"L": 2, "U": 4.0}}), elements=ELEMENTS)
        assert result["hubbard_u"]["Fe"] == {"L": 2, "U": 4.0, "J": 0.0}

    def test_element_not_in_the_structure_is_refused(self):
        with pytest.raises(ValidationError, match="not in the structure"):
            validate_recipe(recipe(hubbard_u={"Ni": {"U": 4.0}}),
                            elements=ELEMENTS)

    def test_u_out_of_range_is_refused(self):
        with pytest.raises(ValidationError, match=r"hubbard_u\[Fe\]\.U"):
            validate_recipe(recipe(hubbard_u={"Fe": {"U": 99.0}}),
                            elements=ELEMENTS)


class TestOverrides:
    def test_keys_are_upper_cased(self):
        result = validate_recipe(recipe(overrides={"relax": {"nsw": 200}}),
                                 elements=ELEMENTS)
        assert result["overrides"]["relax"] == {"NSW": "200"}

    def test_override_for_a_stage_not_being_run_is_refused(self):
        with pytest.raises(ValidationError, match="does not run"):
            validate_recipe(recipe(overrides={"dos": {"NEDOS": 3000}}),
                            elements=ELEMENTS)

    def test_a_key_that_is_not_an_incar_parameter_is_refused(self):
        with pytest.raises(ValidationError, match="not an INCAR parameter"):
            validate_recipe(recipe(overrides={"relax": {"go fast": 1}}),
                            elements=ELEMENTS)


class TestResources:
    def test_bad_server_name_is_refused(self):
        with pytest.raises(ValidationError, match="resources.server"):
            validate_recipe(
                recipe(resources={"server": "../etc", "ntasks": 8,
                                  "walltime": "1:00:00"}), elements=ELEMENTS)

    def test_bad_walltime_is_refused(self):
        with pytest.raises(ValidationError, match="walltime"):
            validate_recipe(
                recipe(resources={"server": "hpc1", "ntasks": 8,
                                  "walltime": "forever"}), elements=ELEMENTS)

    def test_ntasks_range(self):
        with pytest.raises(ValidationError, match="resources.ntasks"):
            validate_recipe(
                recipe(resources={"server": "hpc1", "ntasks": 0,
                                  "walltime": "1:00:00"}), elements=ELEMENTS)


class TestShape:
    def test_a_non_object_recipe_is_refused(self):
        with pytest.raises(ValidationError, match="JSON object"):
            validate_recipe(["relax"], elements=ELEMENTS)

    def test_structure_must_carry_a_file_and_a_hash(self):
        with pytest.raises(ValidationError, match="structure"):
            validate_recipe(recipe(structure={"file": "Fe2O3.vasp"}),
                            elements=ELEMENTS)
