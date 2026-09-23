# VASPKIT 接入 · 第一批（不联网的纯函数层）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把「一个 VESTA 导出的 `.vasp` + 末尾中文标注」变成「经过校验的配方 + 阶段图 + 每阶段的 INCAR 断言」，全部是纯函数，一行都不碰网络。

**Architecture:** 四个新模块组成一条单向管道：`structure.py` 切标注并体检结构 → `recipe.py` 校验模型产出的配方 → `verify.py` 提供值归一化与比较原语 → `stages.py` 把配方展开成阶段图并挂上断言。`stages.py` 复用 `verify.py` 的比较函数来做「override 与断言冲突」检查，因此没有第二份比较逻辑。

**Tech Stack:** Python 3.11+，仅标准库。错误一律抛 `vaspilot.core.errors.ValidationError`。INCAR 解析复用既有的 `vaspilot.hpc.vasp.parse_incar`。

**Spec:** `docs/superpowers/specs/2026-09-23-vaspkit-campaign-design.md`

## Global Constraints

- 工作目录是 Windows 上的 `D:\VASP_new`，通过 `ssh win` 操作；不在 Ubuntu 上跑。
- `src/` 内**禁止第三方依赖**，只用标准库（项目既有原则）。
- 最低 Python 版本 3.11；所有新模块首行 `from __future__ import annotations`。
- `.py` 在仓库中以 LF 存储（`.gitattributes` 已配置），不要引入 CRLF。
- 测试命令（每次都要换随机 basetemp，固定 basetemp 会因残留的 `pytest-current` 符号链接报 `PermissionError: [WinError 5]`）：
  `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=%TEMP%\vp-pt-%RANDOM%%RANDOM%`
- 本批**不新增任何会联网的代码**；`adapter.py`（VASPKIT 命令构造）与 `campaign.py`（阶段串联）属第二批，不在本计划内。
- 提交信息末尾附 `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`。

## File Structure

| 路径 | 职责 |
| --- | --- |
| `src/vaspilot/vaspkit/__init__.py` | 包标记，无逻辑 |
| `src/vaspilot/vaspkit/structure.py` | `.vasp` 的切分、解析、体检、结构摘要 |
| `src/vaspilot/vaspkit/recipe.py` | 配方 schema、范围校验、阶段依赖展开 |
| `src/vaspilot/vaspkit/verify.py` | INCAR 值归一化、比较原语、断言比对 |
| `src/vaspilot/vaspkit/stages.py` | 配方 → 阶段图 + 断言表 + override 冲突检查 |
| `tests/test_vaspkit_structure.py` | 对应 Task 1 |
| `tests/test_vaspkit_recipe.py` | 对应 Task 2 |
| `tests/test_vaspkit_verify.py` | 对应 Task 3 |
| `tests/test_vaspkit_stages.py` | 对应 Task 4 |

---

### Task 1: 切标注、体检结构

**Files:**
- Create: `src/vaspilot/vaspkit/__init__.py`
- Create: `src/vaspilot/vaspkit/structure.py`
- Test: `tests/test_vaspkit_structure.py`

**Interfaces:**
- Consumes: `vaspilot.core.errors.ValidationError`
- Produces:
  - `Poscar` 冻结数据类，字段 `comment: str`、`scale: float`、`lattice: tuple[tuple[float,float,float], ...]`、`symbols: tuple[str, ...]`、`counts: tuple[int, ...]`、`selective: bool`、`coord_mode: str`、`positions: tuple[str, ...]`、`text: str`，属性 `natoms: int`
  - `split_annotation(text: str) -> tuple[str, str]`
  - `parse_poscar(text: str) -> Poscar`
  - `structure_summary(poscar: Poscar) -> dict[str, Any]`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_vaspkit_structure.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_vaspkit_structure.py -q -p no:cacheprovider --basetemp=%TEMP%\vp-pt-%RANDOM%%RANDOM%`
Expected: 收集阶段即 FAIL，`ModuleNotFoundError: No module named 'vaspilot.vaspkit'`

- [ ] **Step 3: 写实现**

创建 `src/vaspilot/vaspkit/__init__.py`：

```python
"""VASPKIT 接入：从一个 VESTA 导出的 .vasp 到一条可执行的计算链。

本包按「越靠前越确定」排列：structure/recipe/verify/stages 全是纯函数，
不碰网络；真正调用 VASPKIT 的适配层单独成文件，便于审计。
"""
```

创建 `src/vaspilot/vaspkit/structure.py`：

```python
"""VESTA 导出的 `.vasp`：切标注、体检、摘要。全是文本上的纯函数。

VESTA 导出的文件本身就是 POSCAR 格式，所以这里做的不是格式转换而是体检：
元素符号行必须在（VASPKIT task 103 靠它挑赝势），原子数必须和坐标块对得上，
用户写在下面的那段话必须干净地摘出来。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from ..core.errors import ValidationError

_SYMBOL_RE = re.compile(r"^[A-Z][a-z]?$")


@dataclass(frozen=True)
class Poscar:
    comment: str
    scale: float
    lattice: tuple[tuple[float, float, float], ...]
    symbols: tuple[str, ...]
    counts: tuple[int, ...]
    selective: bool
    coord_mode: str
    positions: tuple[str, ...]
    text: str

    @property
    def natoms(self) -> int:
        return sum(self.counts)


def _floats(line: str, *, label: str) -> tuple[float, float, float]:
    parts = line.split()
    if len(parts) < 3:
        raise ValidationError(f"{label} needs three numbers, got {line.strip()!r}")
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError as exc:
        raise ValidationError(f"{label} is not numeric: {line.strip()!r}") from exc


def _scan(lines: list[str]) -> dict[str, Any]:
    """Read the header and report where the coordinate block starts."""
    if len(lines) < 8:
        raise ValidationError("not a POSCAR: the file has fewer than 8 lines")
    try:
        scale = float(lines[1].split()[0])
    except (IndexError, ValueError) as exc:
        raise ValidationError(
            f"line 2 must be the scaling factor, got {lines[1].strip()!r}") from exc
    lattice = tuple(_floats(lines[i], label=f"the lattice vector on line {i + 1}")
                    for i in (2, 3, 4))

    tokens = lines[5].split()
    if not tokens or all(token.isdigit() for token in tokens):
        raise ValidationError(
            "this file has no element-symbol line (the old VASP 4 layout). "
            "VASPKIT picks pseudopotentials from that line, so export from "
            "VESTA in the VASP 5 format instead.")
    if not all(_SYMBOL_RE.fullmatch(token) for token in tokens):
        raise ValidationError(
            f"line 6 is not a list of element symbols: {lines[5].strip()!r}")
    symbols = tuple(tokens)

    count_tokens = lines[6].split()
    if not count_tokens or not all(token.isdigit() for token in count_tokens):
        raise ValidationError(
            f"line 7 must be the atom counts, got {lines[6].strip()!r}")
    counts = tuple(int(token) for token in count_tokens)
    if len(counts) != len(symbols):
        raise ValidationError(
            f"{len(symbols)} element symbols but {len(counts)} counts")
    if any(count <= 0 for count in counts):
        raise ValidationError("every atom count must be positive")

    index = 7
    selective = lines[index].strip()[:1].upper() == "S"
    if selective:
        index += 1
    if index >= len(lines):
        raise ValidationError("the coordinate-mode line is missing")
    marker = lines[index].strip()[:1].upper()
    if marker == "D":
        coord_mode = "Direct"
    elif marker in ("C", "K"):
        coord_mode = "Cartesian"
    else:
        raise ValidationError(
            f"unknown coordinate mode {lines[index].strip()!r}")

    return {"scale": scale, "lattice": lattice, "symbols": symbols,
            "counts": counts, "selective": selective,
            "coord_mode": coord_mode, "coords_start": index + 1}


def split_annotation(text: str) -> tuple[str, str]:
    """Cut a VESTA export into (POSCAR text, whatever was typed below it).

    POSCAR has no comment syntax -- as far as VASP is concerned anything
    after the coordinates is the velocity block -- so the only sound way to
    find the end of the structure is to count atoms.
    """
    lines = text.splitlines()
    scan = _scan(lines)
    start = scan["coords_start"]
    natoms = sum(scan["counts"])
    end = start + natoms
    if end > len(lines):
        raise ValidationError(
            f"the file declares {natoms} atoms but only "
            f"{max(0, len(lines) - start)} coordinate lines follow")
    return "\n".join(lines[:end]) + "\n", "\n".join(lines[end:]).strip()


def parse_poscar(text: str) -> Poscar:
    lines = text.splitlines()
    scan = _scan(lines)
    start = scan["coords_start"]
    natoms = sum(scan["counts"])
    positions = tuple(lines[start:start + natoms])
    if len(positions) != natoms:
        raise ValidationError(
            f"the file declares {natoms} atoms but only {len(positions)} "
            "coordinate lines follow")
    for offset, line in enumerate(positions):
        _floats(line, label=f"the coordinates of atom {offset + 1}")
    return Poscar(comment=lines[0].strip(), scale=scan["scale"],
                  lattice=scan["lattice"], symbols=scan["symbols"],
                  counts=scan["counts"], selective=scan["selective"],
                  coord_mode=scan["coord_mode"], positions=positions,
                  text="\n".join(lines[:start + natoms]) + "\n")


def _determinant(rows: tuple[tuple[float, float, float], ...]) -> float:
    (a, b, c), (d, e, f), (g, h, i) = rows
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def _norm(vector: tuple[float, float, float]) -> float:
    return math.sqrt(sum(component * component for component in vector))


def _angle(u: tuple[float, float, float], v: tuple[float, float, float]) -> float:
    dot = sum(a * b for a, b in zip(u, v))
    cosine = dot / (_norm(u) * _norm(v))
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def structure_summary(poscar: Poscar) -> dict[str, Any]:
    """The numbers a human needs to spot a bad export before anything runs."""
    raw_volume = abs(_determinant(poscar.lattice))
    if raw_volume <= 0.0:
        raise ValidationError("the three lattice vectors are coplanar")
    if poscar.scale < 0.0:
        # VASP reads a negative scaling factor as the target cell volume.
        volume = -poscar.scale
        factor = (volume / raw_volume) ** (1.0 / 3.0)
    else:
        factor = poscar.scale
        volume = raw_volume * factor ** 3
    vectors = tuple(tuple(component * factor for component in row)
                    for row in poscar.lattice)

    totals: dict[str, int] = {}
    for symbol, count in zip(poscar.symbols, poscar.counts):
        totals[symbol] = totals.get(symbol, 0) + count

    return {
        "formula": "".join(symbol if count == 1 else f"{symbol}{count}"
                           for symbol, count in totals.items()),
        "elements": tuple(totals),
        "counts": totals,
        "natoms": poscar.natoms,
        "abc": tuple(_norm(vector) for vector in vectors),
        "angles": (_angle(vectors[1], vectors[2]),
                   _angle(vectors[0], vectors[2]),
                   _angle(vectors[0], vectors[1])),
        "volume": volume,
        "coord_mode": poscar.coord_mode,
        "selective_dynamics": poscar.selective,
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_vaspkit_structure.py -q -p no:cacheprovider --basetemp=%TEMP%\vp-pt-%RANDOM%%RANDOM%`
Expected: PASS（14 项）

- [ ] **Step 5: 提交**

```bash
git add src/vaspilot/vaspkit/__init__.py src/vaspilot/vaspkit/structure.py tests/test_vaspkit_structure.py
git commit -m "feat(vaspkit): read a VESTA .vasp file and its annotation"
```

---

### Task 2: 配方校验

**Files:**
- Create: `src/vaspilot/vaspkit/recipe.py`
- Test: `tests/test_vaspkit_recipe.py`

**Interfaces:**
- Consumes: `vaspilot.core.errors.ValidationError`
- Produces:
  - 常量 `STAGES: tuple[str, ...]`、`STAGE_REQUIRES: dict[str, str]`、`DEFAULT_KSPACING: dict[str, float]`
  - `expand_stages(names: list[str], *, structure_relaxed: bool = False) -> tuple[str, ...]`
  - `validate_recipe(raw: dict[str, Any], *, elements: tuple[str, ...]) -> dict[str, Any]`，返回的字典含键 `structure`、`functional`、`stages`、`kspacing`、`spin`、`hubbard_u`、`overrides`、`resources`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_vaspkit_recipe.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_vaspkit_recipe.py -q -p no:cacheprovider --basetemp=%TEMP%\vp-pt-%RANDOM%%RANDOM%`
Expected: FAIL，`ModuleNotFoundError: No module named 'vaspilot.vaspkit.recipe'`

- [ ] **Step 3: 写实现**

创建 `src/vaspilot/vaspkit/recipe.py`：

```python
"""配方：模型唯一被允许产出的结构化东西。

下游全是确定性代码，所以这里就是半懂的意图必须被挡下来的地方。一条都不
联网，校验不过就抛错，绝不带着一份猜出来的意图往服务器走。
"""

from __future__ import annotations

import re
from typing import Any

from ..core.errors import ValidationError

STAGES = ("relax", "static", "band", "dos")
# 后者依赖前者：能带和态密度都要读自洽算出的电荷密度。
STAGE_REQUIRES = {"static": "relax", "band": "static", "dos": "static"}
FUNCTIONALS = {"pbe": "PBE", "pbesol": "PBEsol", "lda": "LDA",
               "scan": "SCAN", "hse06": "HSE06"}
DEFAULT_KSPACING = {"relax": 0.04, "static": 0.03, "band": 0.03, "dos": 0.02}
KSPACING_RANGE = (0.01, 0.5)

_INCAR_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,15}$")
_SERVER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_WALLTIME_RE = re.compile(r"^\d{1,5}(:\d{1,2}){0,2}$")


def _int_in(value: Any, *, label: str, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} must be an integer") from exc
    if not low <= number <= high:
        raise ValidationError(f"{label} must be in {low}..{high}")
    return number


def _float_in(value: Any, *, label: str, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} must be a number") from exc
    if not low <= number <= high:
        raise ValidationError(f"{label} must be in {low}..{high}")
    return number


def expand_stages(names: list[str], *,
                  structure_relaxed: bool = False) -> tuple[str, ...]:
    """Add the stages the requested ones depend on, in canonical order."""
    requested = tuple(dict.fromkeys(names))
    wanted = set(requested)
    changed = True
    while changed:
        changed = False
        for stage in tuple(wanted):
            needed = STAGE_REQUIRES.get(stage)
            if needed and needed not in wanted:
                wanted.add(needed)
                changed = True
    # "already relaxed" removes the relaxation we inferred, never one that
    # was asked for outright.
    if structure_relaxed and "relax" not in requested:
        wanted.discard("relax")
    return tuple(stage for stage in STAGES if stage in wanted)


def _validate_structure(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("structure must be an object")
    file_name = str(raw.get("file", "")).strip()
    digest = str(raw.get("sha256", "")).strip().lower()
    if not file_name:
        raise ValidationError("structure.file is required")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValidationError("structure.sha256 must be a SHA-256 hex digest")
    return {"file": file_name, "formula": str(raw.get("formula", "")).strip(),
            "sha256": digest}


def validate_recipe(raw: dict[str, Any], *,
                    elements: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("the recipe must be a JSON object")

    structure = _validate_structure(raw.get("structure"))

    functional = str(raw.get("functional", "PBE")).strip()
    canonical = FUNCTIONALS.get(functional.lower())
    if canonical is None:
        raise ValidationError(
            f"unknown functional {functional!r}; supported: "
            + ", ".join(sorted(set(FUNCTIONALS.values()))))

    names = raw.get("stages") or []
    if not isinstance(names, list) or not names:
        raise ValidationError("the recipe must ask for at least one stage")
    unknown = [str(name) for name in names if name not in STAGES]
    if unknown:
        raise ValidationError(
            f"unknown stage(s) {', '.join(map(repr, unknown))}; supported: "
            + ", ".join(STAGES))
    stages = expand_stages(list(names),
                           structure_relaxed=bool(raw.get("structure_relaxed")))
    if not stages:
        raise ValidationError("no stages are left once dependencies resolve")

    kspacing = {stage: DEFAULT_KSPACING[stage] for stage in stages}
    given = raw.get("kspacing") or {}
    if not isinstance(given, dict):
        raise ValidationError("kspacing must map a stage to a spacing")
    low, high = KSPACING_RANGE
    for stage, value in given.items():
        if stage not in STAGES:
            raise ValidationError(f"kspacing names unknown stage {stage!r}")
        try:
            spacing = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"kspacing[{stage}] is not a number") from exc
        if not low <= spacing <= high:
            raise ValidationError(
                f"kspacing[{stage}] = {spacing} is outside {low}..{high}")
        if stage in kspacing:
            kspacing[stage] = spacing

    hubbard: dict[str, dict[str, float]] = {}
    given_u = raw.get("hubbard_u") or {}
    if not isinstance(given_u, dict):
        raise ValidationError("hubbard_u must map an element to its parameters")
    for symbol, params in given_u.items():
        if symbol not in elements:
            raise ValidationError(
                f"Hubbard U names {symbol!r}, which is not in the structure "
                f"({', '.join(elements)})")
        if not isinstance(params, dict):
            raise ValidationError(f"hubbard_u[{symbol}] must be an object")
        hubbard[symbol] = {
            "L": _int_in(params.get("L", 2), label=f"hubbard_u[{symbol}].L",
                         low=-1, high=3),
            "U": _float_in(params.get("U", 0.0), label=f"hubbard_u[{symbol}].U",
                           low=0.0, high=10.0),
            "J": _float_in(params.get("J", 0.0), label=f"hubbard_u[{symbol}].J",
                           low=0.0, high=10.0),
        }

    overrides: dict[str, dict[str, str]] = {}
    given_overrides = raw.get("overrides") or {}
    if not isinstance(given_overrides, dict):
        raise ValidationError("overrides must map a stage to INCAR parameters")
    for stage, values in given_overrides.items():
        if stage not in stages:
            raise ValidationError(
                f"overrides names stage {stage!r}, which this recipe "
                "does not run")
        if not isinstance(values, dict):
            raise ValidationError(f"overrides[{stage}] must be an object")
        cleaned: dict[str, str] = {}
        for key, value in values.items():
            token = str(key).strip().upper()
            if not _INCAR_KEY_RE.fullmatch(token):
                raise ValidationError(f"{key!r} is not an INCAR parameter name")
            cleaned[token] = str(value).strip()
        overrides[stage] = cleaned

    resources = raw.get("resources") or {}
    if not isinstance(resources, dict):
        raise ValidationError("resources must be an object")
    server = str(resources.get("server", "")).strip()
    if not _SERVER_RE.fullmatch(server):
        raise ValidationError(
            "resources.server must be a registered server name")
    walltime = str(resources.get("walltime", "24:00:00")).strip()
    if not _WALLTIME_RE.fullmatch(walltime):
        raise ValidationError(
            f"resources.walltime {walltime!r} is not HH:MM:SS")
    partition = str(resources.get("partition", "")).strip()

    return {
        "structure": structure,
        "functional": canonical,
        "stages": stages,
        "kspacing": kspacing,
        "spin": bool(raw.get("spin", False)),
        "hubbard_u": hubbard,
        "overrides": overrides,
        "resources": {
            "server": server,
            "partition": partition,
            "ntasks": _int_in(resources.get("ntasks", 8),
                              label="resources.ntasks", low=1, high=4096),
            "walltime": walltime,
        },
    }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_vaspkit_recipe.py -q -p no:cacheprovider --basetemp=%TEMP%\vp-pt-%RANDOM%%RANDOM%`
Expected: PASS（24 项）

- [ ] **Step 5: 提交**

```bash
git add src/vaspilot/vaspkit/recipe.py tests/test_vaspkit_recipe.py
git commit -m "feat(vaspkit): validate the recipe the model produces"
```

---

### Task 3: INCAR 断言比对

**Files:**
- Create: `src/vaspilot/vaspkit/verify.py`
- Test: `tests/test_vaspkit_verify.py`

**Interfaces:**
- Consumes: `vaspilot.hpc.vasp.parse_incar`、`vaspilot.core.errors.ValidationError`
- Produces:
  - `OPS: tuple[str, ...]`
  - `normalize_value(raw: str) -> str`
  - `compare(op: str, actual: str, expected: str) -> bool`
  - `check_incar(text: str, assertions: list[dict[str, str]]) -> dict[str, Any]`，返回 `{"ok": bool, "checked": int, "failures": [...]}`；每个 failure 含 `key`/`op`/`expected`/`actual`/`reason`，`reason` ∈ `{"missing", "mismatch", "not_numeric"}`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_vaspkit_verify.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_vaspkit_verify.py -q -p no:cacheprovider --basetemp=%TEMP%\vp-pt-%RANDOM%%RANDOM%`
Expected: FAIL，`ModuleNotFoundError: No module named 'vaspilot.vaspkit.verify'`

- [ ] **Step 3: 写实现**

创建 `src/vaspilot/vaspkit/verify.py`：

```python
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


def check_incar(text: str,
                assertions: list[dict[str, str]]) -> dict[str, Any]:
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
    return {"ok": not failures, "checked": len(assertions),
            "failures": failures}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_vaspkit_verify.py -q -p no:cacheprovider --basetemp=%TEMP%\vp-pt-%RANDOM%%RANDOM%`
Expected: PASS（22 项）

- [ ] **Step 5: 提交**

```bash
git add src/vaspilot/vaspkit/verify.py tests/test_vaspkit_verify.py
git commit -m "feat(vaspkit): check a generated INCAR against the approved assertions"
```

---

### Task 4: 阶段图与断言表

**Files:**
- Create: `src/vaspilot/vaspkit/stages.py`
- Test: `tests/test_vaspkit_stages.py`

**Interfaces:**
- Consumes: `vaspilot.vaspkit.recipe.validate_recipe` 的返回值；`vaspilot.vaspkit.verify.compare`
- Produces:
  - `STAGE_DIRS: dict[str, str]`、`KPOINTS_TASK: dict[str, int]`、`BASE_ASSERTIONS: dict[str, tuple]`
  - `build_campaign(recipe: dict[str, Any]) -> dict[str, Any]`，返回 `{"stages": [...], "server": str, "functional": str}`；每个 stage 含 `index`/`name`/`dir`/`poscar_from`/`chgcar_from`/`kpoints_task`/`kspacing`/`requires`/`assertions`/`overrides`

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_vaspkit_stages.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_vaspkit_stages.py -q -p no:cacheprovider --basetemp=%TEMP%\vp-pt-%RANDOM%%RANDOM%`
Expected: FAIL，`ModuleNotFoundError: No module named 'vaspilot.vaspkit.stages'`

- [ ] **Step 3: 写实现**

创建 `src/vaspilot/vaspkit/stages.py`：

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_vaspkit_stages.py -q -p no:cacheprovider --basetemp=%TEMP%\vp-pt-%RANDOM%%RANDOM%`
Expected: PASS（18 项）

- [ ] **Step 5: 跑全量套件**

Run: `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=%TEMP%\vp-pt-%RANDOM%%RANDOM%`
Expected: 既有的 345 passed / 1 skipped 全部仍然通过，加上本批新增的用例

- [ ] **Step 6: 提交**

```bash
git add src/vaspilot/vaspkit/stages.py tests/test_vaspkit_stages.py
git commit -m "feat(vaspkit): expand a recipe into the stage graph and its assertions"
```

---

## 本批之后还剩什么

第二批（需要真机）：`adapter.py`（`vaspkit_doctor` 探针与命令构造）、`workflow/campaign.py`（阶段串联与依赖判定）、`plan.py` 的新步骤类型、`ServerEntry` 新字段、五个工具的注册、UI 的「我的计算」列表。

本批交付后，`campaign_plan` 工具所需的全部确定性逻辑都已就位且有测试覆盖——缺的只是把它们接到工具层，以及真正会动服务器的那半边。
