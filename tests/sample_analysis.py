"""General research planning: explicit Python code, immutable inputs and outputs."""
from __future__ import annotations

import ast
import csv
import io
import json
import shutil
import sys
from pathlib import Path

from rsci.core.errors import ValidationError
from rsci.core.hashing import file_sha256, obj_sha256, text_sha256
from rsci.core.project_paths import MAX_IMPORT_BYTES, confined_path, general_name
from rsci.workflow.projects import ProjectStore


from rsci.workflow.research import project_store

def csv_preview(store: ProjectStore, name: str, file: str) -> dict:
    if store.kind(name) != "general":
        raise ValidationError("CSV analysis requires a general project")
    path = store._file(name, file)
    if path.suffix.lower() not in {".csv", ".tsv"} or not path.is_file():
        raise ValidationError("select an existing CSV or TSV file")
    if path.stat().st_size > MAX_IMPORT_BYTES:
        raise ValidationError("CSV exceeds 16 MiB")
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, delimiter="\t" if path.suffix.lower() == ".tsv" else ",")
            header = next(reader, [])
            sample = []
            for row in reader:
                sample.append(row)
                if len(sample) == 5:
                    break
    except (UnicodeError, csv.Error) as exc:
        raise ValidationError(f"cannot read UTF-8 CSV: {exc}") from exc
    if not header or len(header) > 200 or len(set(header)) != len(header) or any(not v.strip() for v in header):
        raise ValidationError("CSV needs 1–200 nonempty, unique column names")
    return {"file": file, "sha256": file_sha256(path), "columns": header,
            "sample": sample, "sample_only": True}


def csv_script(input_file: str) -> str:
    """A dependency-free, editable analysis. No statistical claims are implied."""
    general_name(input_file)
    delimiter = "\t" if input_file.lower().endswith(".tsv") else ","
    return '''"""Descriptive CSV analysis; review and adapt before approval.
Missing/non-numeric values are counted, never silently imputed.
SVG uses a common value scale; compare only columns with comparable units.
"""
import csv
import html
import json
import math
import statistics
from pathlib import Path

source = Path(''' + repr(input_file) + ''')
out = Path("results")
out.mkdir(exist_ok=True)
with source.open(encoding="utf-8-sig", newline="") as handle:
    reader = csv.DictReader(handle, delimiter=''' + repr(delimiter) + ''')
    names = reader.fieldnames or []
    if not names or len(names) > 200 or len(set(names)) != len(names) or any(not n.strip() for n in names):
        raise ValueError("Column names must be nonempty and unique (maximum 200).")
    values = {name: [] for name in names}
    missing = {name: 0 for name in names}
    non_numeric = {name: 0 for name in names}
    rows = 0
    for row in reader:
        rows += 1
        if rows > 100000:
            raise ValueError("This starter script supports at most 100,000 rows.")
        if None in row or any(row.get(n) is None for n in names):
            raise ValueError(f"Malformed CSV row {rows + 1}: unexpected field count")
        for name in names:
            cell = row[name].strip()
            if not cell:
                missing[name] += 1
                continue
            try:
                number = float(cell)
                if not math.isfinite(number):
                    raise ValueError("nonfinite")
            except ValueError:
                non_numeric[name] += 1
            else:
                values[name].append(number)
summary = {"source": str(source), "rows": rows, "columns": {},
           "limitations": ["Descriptive statistics only; no causal or significance claim.",
                           "Units are not inferred; compare only commensurate columns.",
                           "Missing and non-numeric cells are excluded and counted."]}
for name in names:
    nums = values[name]
    summary["columns"][name] = {"numeric_count": len(nums), "missing_count": missing[name],
                               "non_numeric_count": non_numeric[name],
                               "mean": statistics.fmean(nums) if nums else None,
                               "min": min(nums) if nums else None,
                               "max": max(nums) if nums else None}
(out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
lines = ["# CSV descriptive analysis", "", f"Rows: {rows}", "", "```json", json.dumps(summary["columns"], ensure_ascii=False, indent=2), "```", "", "## Limitations", ""]
lines += ["- " + text for text in summary["limitations"]]
(out / "report.md").write_text("\\n".join(lines), encoding="utf-8")
numeric = [(name, item["mean"]) for name, item in summary["columns"].items() if item["mean"] is not None]
height = max(160, 100 + 36 * len(numeric))
svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="960" height="{height}" viewBox="0 0 960 {height}">',
       '<rect width="100%" height="100%" fill="#ffffff"/>',
       '<text x="24" y="30" fill="#172334" font-size="18">Column means — descriptive only</text>',
       '<text x="24" y="53" fill="#4d5e73" font-size="12">Common scale; compare only compatible units. Missing/non-numeric cells excluded.</text>']
scale = max([abs(value) for _, value in numeric] + [1.0])
for index, (name, value) in enumerate(numeric):
    y = 86 + 36 * index
    width = abs(value) / scale * 240
    x = 540 if value >= 0 else 540 - width
    label = html.escape(name[:36])
    svg += [f'<text x="24" y="{y + 16}" fill="#172334" font-size="13">{label}</text>',
            f'<rect x="{x}" y="{y}" width="{width}" height="22" fill="#165bb6"/>',
            f'<text x="820" y="{y + 16}" fill="#172334" font-size="13">{value:.6g}</text>']
if not numeric:
    svg.append('<text x="24" y="100" fill="#4d5e73" font-size="14">No finite numeric values were found.</text>')
svg += [f'<line x1="540" y1="72" x2="540" y2="{height - 15}" stroke="#78879b"/>', '</svg>']
(out / "means.svg").write_text("\\n".join(svg), encoding="utf-8")
print(f"Analyzed {rows} rows; wrote results/summary.json, report.md and means.svg")
'''


def prepare_csv_template(config, name: str, file: str, *, script="scripts/analyze.py", expected_sha256=None, audit=None):
    store = project_store(config, audit)
    preview = csv_preview(store, name, file)
    script = general_name(script)
    if not script.startswith("scripts/") or not script.endswith(".py"):
        raise ValidationError("choose a scripts/*.py filename")
    target = store._file(name, script)
    if target.exists() and not expected_sha256:
        raise ValidationError("analysis script already exists; review it instead of replacing it")
    written = store.write_file(name, script, csv_script(file), expected_sha256=expected_sha256)
    return {"project": name, "preview": preview, "script": written,
            "spec": {"domain": "general", "project": name, "script": script, "inputs": [file],
                     "outputs": ["results/summary.json", "results/report.md", "results/means.svg"]},
            "message": "已生成可编辑脚本，尚未执行；审阅后生成分析计划并审批。"}
