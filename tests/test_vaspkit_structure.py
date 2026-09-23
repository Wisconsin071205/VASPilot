"""VESTA 导出的 .vasp：切标注、体检、摘要。"""

import pytest

from vaspilot.core.errors import ValidationError
from vaspilot.vaspkit.structure import (Poscar, parse_poscar, split_annotation,
                                        structure_summary)

CUBIC = """Fe2O3 cell
1.0
  5.0 0.0 0.0
  0.0 5.0 0.0
  0.0 0.0 5.0
Fe O
2 3
Direct
 0.00 0.00 0.00
 0.50 0.50 0.50
 0.25 0.25 0.25
 0.75 0.75 0.75
 0.50 0.00 0.00
"""

ANNOTATED = CUBIC + "先做结构优化，然后算能带和态密度，用 PBE\n自旋打开\n"


class TestSplitAnnotation:
    def test_counts_atoms_to_find_the_end(self):
        poscar, annotation = split_annotation(ANNOTATED)
        assert poscar == CUBIC
        assert annotation == "先做结构优化，然后算能带和态密度，用 PBE\n自旋打开"

    def test_no_annotation_gives_empty_string(self):
        poscar, annotation = split_annotation(CUBIC)
        assert poscar == CUBIC
        assert annotation == ""

    def test_selective_dynamics_shifts_the_coordinate_block(self):
        text = CUBIC.replace("Direct\n", "Selective dynamics\nDirect\n")
        text = text.replace(" 0.00 0.00 0.00", " 0.00 0.00 0.00 T T T")
        poscar, annotation = split_annotation(text + "算静态自洽\n")
        assert annotation == "算静态自洽"
        assert poscar.splitlines()[-1].strip() == "0.50 0.00 0.00"

    def test_truncated_coordinates_are_refused(self):
        truncated = "\n".join(CUBIC.splitlines()[:-2]) + "\n"
        with pytest.raises(ValidationError, match="declares 5 atoms"):
            split_annotation(truncated)


class TestParsePoscar:
    def test_reads_the_header(self):
        poscar = parse_poscar(CUBIC)
        assert isinstance(poscar, Poscar)
        assert poscar.symbols == ("Fe", "O")
        assert poscar.counts == (2, 3)
        assert poscar.natoms == 5
        assert poscar.coord_mode == "Direct"
        assert poscar.selective is False
        assert poscar.text == CUBIC

    def test_cartesian_is_recognised(self):
        poscar = parse_poscar(CUBIC.replace("Direct", "Cartesian"))
        assert poscar.coord_mode == "Cartesian"

    def test_missing_symbol_line_names_the_real_problem(self):
        vasp4 = CUBIC.replace("Fe O\n", "")
        with pytest.raises(ValidationError, match="element-symbol line"):
            parse_poscar(vasp4)

    def test_symbol_and_count_mismatch_is_refused(self):
        bad = CUBIC.replace("Fe O\n", "Fe O N\n")
        with pytest.raises(ValidationError, match="3 element symbols but 2 counts"):
            parse_poscar(bad)

    def test_non_numeric_coordinates_are_refused(self):
        bad = CUBIC.replace(" 0.25 0.25 0.25", " 0.25 oops 0.25")
        with pytest.raises(ValidationError, match="atom 3"):
            parse_poscar(bad)


class TestStructureSummary:
    def test_cubic_cell(self):
        summary = structure_summary(parse_poscar(CUBIC))
        assert summary["formula"] == "Fe2O3"
        assert summary["natoms"] == 5
        assert summary["elements"] == ("Fe", "O")
        assert summary["abc"] == pytest.approx((5.0, 5.0, 5.0))
        assert summary["angles"] == pytest.approx((90.0, 90.0, 90.0))
        assert summary["volume"] == pytest.approx(125.0)

    def test_scale_multiplies_the_lattice(self):
        summary = structure_summary(parse_poscar(CUBIC.replace("1.0\n", "2.0\n", 1)))
        assert summary["abc"] == pytest.approx((10.0, 10.0, 10.0))
        assert summary["volume"] == pytest.approx(1000.0)

    def test_negative_scale_is_a_target_volume(self):
        # VASP reads a negative scaling factor as the desired cell volume.
        summary = structure_summary(parse_poscar(CUBIC.replace("1.0\n", "-8.0\n", 1)))
        assert summary["volume"] == pytest.approx(8.0)
        assert summary["abc"] == pytest.approx((2.0, 2.0, 2.0))

    def test_repeated_symbols_merge_into_one_formula_entry(self):
        text = CUBIC.replace("Fe O\n", "Fe O Fe\n").replace("2 3\n", "1 3 1\n")
        summary = structure_summary(parse_poscar(text))
        assert summary["formula"] == "Fe2O3"
        assert summary["counts"] == {"Fe": 2, "O": 3}

    def test_coplanar_lattice_is_refused(self):
        flat = CUBIC.replace("  0.0 0.0 5.0", "  5.0 0.0 0.0")
        with pytest.raises(ValidationError, match="coplanar"):
            structure_summary(parse_poscar(flat))
