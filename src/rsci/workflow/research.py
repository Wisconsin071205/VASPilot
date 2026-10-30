"""General research planning: explicit Python code, immutable inputs and outputs."""
from __future__ import annotations

import ast
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

