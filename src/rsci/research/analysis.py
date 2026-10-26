"""Deterministic data profiling and reviewable, approval-bound templates."""
from __future__ import annotations

import csv
import math
import statistics
from collections import Counter
from pathlib import Path

from ..core.errors import ValidationError
from ..core.hashing import file_sha256
from ..core.project_paths import MAX_IMPORT_BYTES
from .common import project, fingerprint, stamp


def load(config, name, file):
    store, _, _ = project(config, name)
    path = store._file(name, file)
    if path.suffix.lower() not in {".csv", ".tsv"} or not path.is_file() or path.stat().st_size > MAX_IMPORT_BYTES:
        raise ValidationError("请选择不超过 16 MiB 的 UTF-8 CSV/TSV")
    digest = file_sha256(path)
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, delimiter="\t" if path.suffix.lower() == ".tsv" else ",")
            columns = next(reader, [])
            if not columns or len(columns) > 200 or len(set(columns)) != len(columns) or any(not c.strip() for c in columns):
                raise ValidationError("数据需要 1–200 个不为空且不重复的列名")
            rows = []
            for number, row in enumerate(reader, 2):
                if len(row) != len(columns):
                    raise ValidationError(f"第 {number} 行列数不正确")
                if len(rows) == 100000:
                    raise ValidationError("第一版最多检查 100000 行")
                rows.append([cell.strip() for cell in row])
    except (UnicodeError, csv.Error) as exc:
        raise ValidationError("无法解析 UTF-8 CSV/TSV") from exc
    if digest != file_sha256(path):
        raise ValidationError("检查期间输入发生变化，请重试")
    return columns, rows, digest


def profile(config, name, file, label_column="", split_column="", expected_sha256=None):
    columns, rows, digest = load(config, name, file)
    if expected_sha256 and expected_sha256 != digest:
        raise ValidationError("数据已在入队后改变，请重新发起检查")
    for column in (label_column, split_column):
        if column and column not in columns:
            raise ValidationError(f"没有找到列：{column}")
    result = {"schema": "rsci.data-profile/1", "project_id": project(config, name)[2],
              "file": file, "input_sha256": digest, "created_at": stamp(), "rows": len(rows),
              "duplicate_rows": len(rows) - len(set(tuple(r) for r in rows)), "columns": {},
              "warnings": [], "label_distribution": None, "split_overlap": {"checked": False}}
    for index, column in enumerate(columns):
        cells = [row[index] for row in rows]
        counts = Counter(cells)
        numbers, text, nonfinite = [], 0, 0
        for cell in cells:
            if not cell:
                continue
            try:
                value = float(cell)
                if math.isfinite(value):
                    numbers.append(value)
                else:
                    nonfinite += 1
            except ValueError:
                text += 1
        outliers = 0
        if len(numbers) >= 4:
            q1, _, q3 = statistics.quantiles(numbers, n=4, method="inclusive")
            outliers = sum(v < q1 - 1.5 * (q3 - q1) or v > q3 + 1.5 * (q3 - q1) for v in numbers)
        result["columns"][column] = {"type": "mixed" if text and (numbers or nonfinite) else "text" if text else "numeric" if numbers or nonfinite else "empty",
            "missing": counts.get("", 0), "numeric": len(numbers), "text": text, "nonfinite": nonfinite,
            "constant": len(set(cells) - {""}) == 1, "outlier_candidates": outliers,
            "outlier_method": "1.5 × IQR, inclusive quartiles; no values removed"}
    if label_column:
        distribution = Counter(r[columns.index(label_column)] for r in rows if r[columns.index(label_column)])
        result["label_distribution"] = dict(distribution)
        if distribution and (len(distribution) == 1 or max(distribution.values()) / sum(distribution.values()) > .9):
            result["warnings"].append("标签只有一种类别或最大类别占比超过 90%，请核对数据与评估方式。")
    if split_column:
        index = columns.index(split_column)
        groups = {}
        for row in rows:
            if row[index]:
                key = tuple(v for i, v in enumerate(row) if i != index and columns[i] != label_column)
                groups.setdefault(key, set()).add(row[index])
        result["split_overlap"] = {"checked": True, "cross_split_duplicate_patterns": sum(len(v) > 1 for v in groups.values()),
                                   "method": "Exact feature rows excluding split and optional label; not a general leakage detector"}
    result["limitations"] = ["检查不改变原始数据；异常值候选需要结合研究背景判断。", "列单位与采样独立性尚未核验。"]
    return result


def prepare_template(config, name, file, template, columns, missing):
    available, _, digest = load(config, name, file)
    if template not in {"descriptive", "correlation", "regression"} or missing not in {"error", "drop"}:
        raise ValidationError("请选择分析模板和缺失值处理方式")
    if not isinstance(columns, list) or not columns or len(columns) > 20 or len(set(columns)) != len(columns) or any(c not in available for c in columns):
        raise ValidationError("请选择 1–20 个不同的现有数据列")
    if template in {"correlation", "regression"} and len(columns) != 2:
        raise ValidationError("相关分析和回归需要两列；回归顺序为 X、Y")
    options = {"file": file, "template": template, "columns": columns, "missing": missing, "input_sha256": digest}
    ident = fingerprint(options)[:16]
    script = f"scripts/analysis-{ident}.py"
    requirements = f"scripts/analysis-{ident}-requirements.txt"
    import json
    source = "# Generated template: review before approving execution.\nimport json\nCONFIG = json.loads(" + repr(json.dumps(options, ensure_ascii=False)) + ")\n"
    source += Path(__file__).with_name("analysis_template.py").read_text(encoding="utf-8")
    store, _, _ = project(config, name)
    for filename, text in ((script, source), (requirements, "scipy==1.15.3\n")):
        target = store._file(name, filename)
        if target.exists() and target.read_text(encoding="utf-8") != text:
            raise ValidationError("同名分析脚本已被修改，请保留原文件并重新选择参数")
        if not target.exists():
            store.write_file(name, filename, text, expected_sha256="missing")
    return {"spec": {"domain": "general", "project": name, "script": script, "inputs": [file, requirements],
                     "outputs": ["results/statistics.json", "results/report.md", "results/plot.svg"]},
            "dependencies": ["scipy==1.15.3"], "message": "脚本已保存，尚未执行。所选 Python 需安装 scipy==1.15.3；请审阅后生成计划并审批。"}
