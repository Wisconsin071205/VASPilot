"""自检：把 VASPKIT 刚写出来的 INCAR 和批准过的断言对一遍。

方案 A 里你批准的是配方而不是文件，这个模块就是替代文件哈希的那道锁。
所以它只报告、不修正——断言对不上意味着服务器上的 VASPKIT 行为和探测结论
不符，那是要人来看的事。
"""

from __future__ import annotations

from typing import Any

from ..core.errors import ValidationError
from ..hpc.vasp import parse_incar

OPS = ("eq", "ne", "ge", "gt", "le", "lt")

_TRUE = {".TRUE.", ".T.", "T", "TRUE"}
_FALSE = {".FALSE.", ".F.", "F", "FALSE"}


def normalize_value(raw: str) -> str:
    """One spelling per value, so `.TRUE.`/`T` and `3`/`3.0` compare equal."""
    token = str(raw).strip().rstrip(";").strip()
    upper = token.upper()
    if upper in _TRUE:
        return ".TRUE."
    if upper in _FALSE:
        return ".FALSE."
    try:
        number = float(token)
    except ValueError:
        return upper
    return str(int(number)) if number == int(number) else repr(number)


def compare(op: str, actual: str, expected: str) -> bool:
    if op not in OPS:
        raise ValidationError(f"unknown comparison {op!r}")
    left, right = normalize_value(actual), normalize_value(expected)
    if op == "eq":
        return left == right
    if op == "ne":
        return left != right
    try:
        a, b = float(left), float(right)
    except ValueError as exc:
        raise ValidationError(
            f"{op} needs numbers, got {actual!r} and {expected!r}") from exc
    return {"ge": a >= b, "gt": a > b, "le": a <= b, "lt": a < b}[op]


def check_incar(text: str, assertions: list[dict[str, str]]) -> dict[str, Any]:
    """Report every violation, not just the first -- one round trip is all
    the operator gets before the stage is blocked."""
    values = parse_incar(text).values
    failures: list[dict[str, Any]] = []
    for item in assertions:
        key, op, expected = item["key"], item["op"], item["value"]
        if key not in values:
            failures.append({"key": key, "op": op, "expected": expected,
                             "actual": None, "reason": "missing"})
            continue
        actual = values[key]
        try:
            satisfied = compare(op, actual, expected)
        except ValidationError:
            failures.append({"key": key, "op": op, "expected": expected,
                             "actual": actual, "reason": "not_numeric"})
            continue
        if not satisfied:
            failures.append({"key": key, "op": op, "expected": expected,
                             "actual": actual, "reason": "mismatch"})
    return {"ok": not failures, "checked": len(assertions), "failures": failures}
