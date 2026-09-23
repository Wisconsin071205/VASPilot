"""自检：VASPKIT 写出来的 INCAR 必须和批准的断言对得上。"""

import pytest

from vaspilot.core.errors import ValidationError
from vaspilot.vaspkit.verify import check_incar, compare, normalize_value

RELAX = """SYSTEM = Fe2O3
ISIF = 3        # full relaxation
IBRION = 2
NSW = 200
ISPIN = 2
LCHARG = .TRUE.
"""


def a(key, op, value):
    return {"key": key, "op": op, "value": value}


class TestNormalizeValue:
    @pytest.mark.parametrize("raw", [".TRUE.", "T", "true", ".T."])
    def test_true_spellings_collapse(self, raw):
        assert normalize_value(raw) == ".TRUE."

    @pytest.mark.parametrize("raw", [".FALSE.", "F", "false", ".F."])
    def test_false_spellings_collapse(self, raw):
        assert normalize_value(raw) == ".FALSE."

    def test_integers_compare_across_spellings(self):
        assert normalize_value("3") == normalize_value("3.0")

    def test_words_upper_case(self):
        assert normalize_value("auto") == "AUTO"


class TestCompare:
    def test_eq(self):
        assert compare("eq", "3", "3.0") is True
        assert compare("eq", "2", "3") is False

    def test_ne(self):
        assert compare("ne", "0", "3") is True

    def test_gt_and_ge(self):
        assert compare("gt", "200", "0") is True
        assert compare("gt", "0", "0") is False
        assert compare("ge", "1000", "1000") is True

    def test_unknown_operator_is_refused(self):
        with pytest.raises(ValidationError, match="unknown comparison"):
            compare("approx", "1", "1")

    def test_ordering_needs_numbers(self):
        with pytest.raises(ValidationError, match="needs numbers"):
            compare("gt", "AUTO", "0")


class TestCheckIncar:
    def test_all_satisfied(self):
        result = check_incar(RELAX, [a("ISIF", "eq", "3"),
                                     a("IBRION", "eq", "2"),
                                     a("NSW", "gt", "0")])
        assert result == {"ok": True, "checked": 3, "failures": []}

    def test_missing_key_is_reported_as_missing(self):
        result = check_incar(RELAX, [a("LORBIT", "eq", "11")])
        assert result["ok"] is False
        assert result["failures"][0] == {"key": "LORBIT", "op": "eq",
                                         "expected": "11", "actual": None,
                                         "reason": "missing"}

    def test_wrong_value_is_reported_with_both_sides(self):
        result = check_incar(RELAX, [a("ISIF", "eq", "2")])
        failure = result["failures"][0]
        assert failure["reason"] == "mismatch"
        assert failure["actual"] == "3"
        assert failure["expected"] == "2"

    def test_the_classic_disaster_is_caught(self):
        # A relaxation whose NSW is 0 does nothing but look successful.
        broken = RELAX.replace("NSW = 200", "NSW = 0")
        result = check_incar(broken, [a("NSW", "gt", "0")])
        assert result["ok"] is False
        assert result["failures"][0]["reason"] == "mismatch"

    def test_comments_do_not_confuse_the_comparison(self):
        assert check_incar(RELAX, [a("ISIF", "eq", "3")])["ok"] is True

    def test_non_numeric_value_under_an_ordering_operator(self):
        text = "NEDOS = AUTO\n"
        result = check_incar(text, [a("NEDOS", "ge", "1000")])
        assert result["failures"][0]["reason"] == "not_numeric"

    def test_every_failure_is_reported_not_just_the_first(self):
        result = check_incar(RELAX, [a("ISIF", "eq", "2"),
                                     a("LORBIT", "eq", "11")])
        assert len(result["failures"]) == 2
