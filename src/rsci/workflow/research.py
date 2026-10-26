"""General research planning: explicit Python code, immutable inputs and outputs."""
from __future__ import annotations

import ast
import csv
import io
import json
import shutil
import sys
from pathlib import Path

from ..core.errors import ValidationError
from ..core.hashing import file_sha256, obj_sha256, text_sha256
from ..core.project_paths import MAX_IMPORT_BYTES, confined_path, general_name
from .projects import ProjectStore


def project_store(config, audit=None):
    return ProjectStore(projects_dir=config.projects_dir,
                        index_path=config.projects_index_path, audit=audit)


def python_executable(config, requested="") -> Path:
    configured = str(config.load_settings().get("research_python") or "")
    fallback = "python" if getattr(sys, "frozen", False) else sys.executable
    if getattr(sys, "frozen", False) and not requested and not configured:
        from ..research.ocr_runtime import status
        runtime = status(config)
        if runtime["ready"]:
            fallback = runtime["executable"]
    value = str(requested or configured or fallback)
    found = shutil.which(value) or value
    path = Path(found).expanduser().absolute()
    if not path.is_file():
        raise ValidationError("找不到 Python 解释器，请安装 Python 或在分析计划中指定解释器路径。")
    if getattr(sys, "frozen", False) and path.resolve() == Path(sys.executable).resolve():
        raise ValidationError("RSCI EXE 不是 Python 解释器，请指定独立的 Python。")
    return path


def build_python_plan(config, spec: dict) -> dict:
    store = project_store(config)
    name = str(spec.get("project") or "")
    if not name and spec.get("from_dir"):
        root = Path(spec["from_dir"]).resolve()
        if root.parent == config.projects_dir.resolve():
            name = root.name
    if not name or store.kind(name) != "general":
        raise ValidationError("本地 Python 分析需要已创建的通用科研项目。")
    root = store._project_dir(name)
    script = general_name(str(spec.get("script") or "scripts/analyze.py"))
    if not script.startswith("scripts/") or not script.endswith(".py"):
        raise ValidationError("analysis script must be a .py file inside scripts/")
    inputs = spec.get("inputs") or []
    outputs = spec.get("outputs") or []
    if not isinstance(inputs, list) or not inputs or len(inputs) > 100:
        raise ValidationError("declare 1–100 input files for the analysis")
    if not isinstance(outputs, list) or not outputs or len(outputs) > 100:
        raise ValidationError("declare 1–100 result files for the analysis")
    names = list(dict.fromkeys([script] + [general_name(str(n)) for n in inputs]))
    result_names = list(dict.fromkeys(general_name(str(n)) for n in outputs))
    if any(not n.startswith("results/") for n in result_names):
        raise ValidationError("analysis outputs must be inside results/")
    if set(names) & set(result_names):
        raise ValidationError("an output cannot overwrite a planned input")
    files, total = [], 0
    for relative in names:
        path = confined_path(root, relative)
        if not path.is_file() or path.stat().st_size > MAX_IMPORT_BYTES:
            raise ValidationError(f"input missing or larger than 16 MiB: {relative}")
        total += path.stat().st_size
        files.append({"name": relative, "local_path": str(path), "size": path.stat().st_size,
                      "sha256": file_sha256(path)})
    if total > 64 * 1024 * 1024:
        raise ValidationError("analysis inputs exceed 64 MiB")
    text = confined_path(root, script).read_text(encoding="utf-8-sig")
    try:
        ast.parse(text, filename=script)
    except SyntaxError as exc:
        raise ValidationError(f"Python syntax error at line {exc.lineno}: {exc.msg}") from exc
    # Syntax checking is not execution and is not a security certification.
    executable = python_executable(config, spec.get("python", ""))
    args = spec.get("args") or []
    if not isinstance(args, list) or len(args) > 32 or any(not isinstance(a, str) or len(a) > 1024 or "\0" in a for a in args):
        raise ValidationError("args must contain at most 32 bounded strings")
    timeout = int(spec.get("timeout_seconds") or 600)
    if not 1 <= timeout <= 3600:
        raise ValidationError("Python timeout must be between 1 and 3600 seconds")
    body = {
        "schema": "rsci.plan/2", "domain": "general", "project": name,
        "project_id": store._metadata(name)["project_id"],
        "project_root": str(root), "server": "local", "remote_dir": str(root),
        "execution": {"kind": "local_python", "python": str(executable),
                      "python_sha256": file_sha256(executable), "script": script,
                      "args": args, "timeout_seconds": timeout},
        "files": files, "outputs": result_names,
        "job_script": {"name": script, "content_sha256": text_sha256(text),
                       "source": "project", "scheduler": "local", "job_name": name,
                       "partition": "", "ntasks": 1, "walltime": f"{timeout}s"},
        "steps": [{"id": "snapshot", "type": "snapshot"},
                  {"id": "execute", "type": "execute", "script": script},
                  {"id": "collect", "type": "collect", "files": result_names},
                  {"id": "parse", "type": "parse"}],
        "risk_summary": {"level": "high", "touches_scheduler": False,
                         "execution_target": "本机 Python", "ntasks": 1,
                         "walltime": f"{timeout}s", "creates_remote": [],
                         "overwrites": "results are isolated by plan id; changed files are never overwritten",
                         "notes": ["Python 以当前用户权限运行，不是安全沙箱；请审阅完整脚本。",
                                   "仅自动传入基础系统环境，不传入模型 API Key 或 SSH 配置。",
                                   "计划固定代码、输入哈希、解释器、参数和输出；修改后必须重新审批。",
                                   "执行成功与结果回收不代表科学结论已通过验证。"]},
    }
    # An explicit label allows a human to request a NEW logical experiment;
    # resume/force never silently reruns an already started one.
    if spec.get("run_label"):
        label = str(spec["run_label"])
        if len(label) > 120 or any(ord(c) < 32 for c in label):
            raise ValidationError("invalid run label")
        body["run_label"] = label
    digest = obj_sha256(body)
    return {**body, "plan_id": digest[:16], "plan_hash": digest, "job_script_content": text}


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
