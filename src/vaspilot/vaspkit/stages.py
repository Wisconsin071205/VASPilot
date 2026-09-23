"""把配方展开成阶段图，并给每个阶段挂上运行时自检要核对的断言。

设计文档里把「断言」和「危险偏离表」分开讲，但一个带比较运算符的列表两者
都能表达（危险偏离不过是 `ne`/`gt` 形式的断言），所以这里只有一套机制、
一个地方可供审阅。
"""

from __future__ import annotations

from typing import Any

from ..core.errors import ValidationError
from .verify import compare

STAGE_DIRS = {"relax": "01-relax", "static": "02-static",
              "band": "03-band", "dos": "04-dos"}
# 102 = 按 K 点间距生成网格；303 = 沿高对称路径生成能带 K 点。
KPOINTS_TASK = {"relax": 102, "static": 102, "band": 303, "dos": 102}

BASE_ASSERTIONS: dict[str, tuple[tuple[str, str, str], ...]] = {
    # NSW > 0 才真的在动结构：NSW=0 的"优化"看起来一切正常，什么也没做。
    "relax": (("ISIF", "eq", "3"), ("IBRION", "eq", "2"), ("NSW", "gt", "0")),
    "static": (("NSW", "eq", "0"), ("ICHARG", "eq", "2"),
               ("LCHARG", "eq", ".TRUE.")),
    "band": (("ICHARG", "eq", "11"), ("LORBIT", "eq", "11"),
             ("NSW", "eq", "0")),
    "dos": (("ICHARG", "eq", "11"), ("LORBIT", "eq", "11"),
            ("NEDOS", "ge", "1000"), ("NSW", "eq", "0")),
}


def _assertion(key: str, op: str, value: str) -> dict[str, str]:
    return {"key": key, "op": op, "value": value}


def _poscar_source(name: str, running: tuple[str, ...]) -> str:
    """Where this stage's structure comes from."""
    if name == "relax":
        return "input"
    if name == "static":
        return f"{STAGE_DIRS['relax']}/CONTCAR" if "relax" in running else "input"
    # A static run leaves the structure untouched (NSW=0), so band and DOS
    # reuse its POSCAR rather than its CONTCAR.
    return f"{STAGE_DIRS['static']}/POSCAR"


def build_campaign(recipe: dict[str, Any]) -> dict[str, Any]:
    running: tuple[str, ...] = tuple(recipe["stages"])
    if ("band" in running or "dos" in running) and "static" not in running:
        raise ValidationError(
            "band and DOS read the charge density a static run writes, "
            "but this recipe has no static stage")

    stages: list[dict[str, Any]] = []
    previous = ""
    for index, name in enumerate(running, start=1):
        assertions = [_assertion(*item) for item in BASE_ASSERTIONS[name]]
        if recipe["spin"]:
            assertions.append(_assertion("ISPIN", "eq", "2"))
        if recipe["hubbard_u"]:
            assertions.append(_assertion("LDAU", "eq", ".TRUE."))

        overrides = dict(recipe["overrides"].get(name, {}))
        for key, value in overrides.items():
            for item in assertions:
                if item["key"] != key:
                    continue
                if not compare(item["op"], value, item["value"]):
                    raise ValidationError(
                        f"overrides[{name}].{key} = {value} contradicts the "
                        f"safety assertion {key} {item['op']} {item['value']} "
                        "for this stage; that assertion is what protects the "
                        "result, so the override is refused")

        stages.append({
            "index": index,
            "name": name,
            "dir": STAGE_DIRS[name],
            "poscar_from": _poscar_source(name, running),
            "chgcar_from": STAGE_DIRS["static"] if name in ("band", "dos") else "",
            "kpoints_task": KPOINTS_TASK[name],
            "kspacing": recipe["kspacing"][name],
            "requires": previous,
            "assertions": assertions,
            "overrides": overrides,
        })
        previous = name

    return {"stages": stages, "server": recipe["resources"]["server"],
            "functional": recipe["functional"]}
