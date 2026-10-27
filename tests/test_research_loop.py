"""Numerical correctness, evidence revisions and reproducible exports."""
import io
import json
import subprocess
import sys
import time
import zipfile

import pytest

from rsci.cli.main import App
from rsci.core.errors import ValidationError
from rsci.research import analysis, reports, tasks, literature
from rsci.research.common import location, save
from tests.test_evidence_center import center, import_pdf, seed_evidence


def test_profile_flags_without_modifying_inputs(center):
    config, store = center
    text = "x,y,label,split\n1,2,A,train\n1,2,A,test\n1,2,A,test\n,NaN,A,train\nbad,8,A,train\n"
    store.write_file("study", "data/sample.csv", text)
    result = analysis.profile(config, "study", "data/sample.csv", "label", "split")
    assert result["duplicate_rows"] == 1
    assert result["columns"]["x"]["type"] == "mixed"
    assert result["columns"]["y"]["nonfinite"] == 1
    assert result["split_overlap"]["cross_split_duplicate_patterns"] == 1
    assert result["warnings"]
    assert store.read_file("study", "data/sample.csv")["content"] == text


@pytest.mark.parametrize("template", ["descriptive", "correlation", "regression"])
def test_analysis_known_values_and_approval_binding(center, template):
    config, store = center
    store.write_file("study", "data/input.csv", "x,y\n1,3\n2,5\n3,7\n4,9\n")
    prepared = analysis.prepare_template(config, "study", "data/input.csv", template, ["x", "y"], "error")
    app = App(config)
    plan = app.engine().prepare(prepared["spec"])
    token = app.engine().approve(plan["plan_id"], stdin_lines=["approve " + plan["plan_id"]])
    state = app.engine().run(plan["plan_id"], token["approval_ref"], poll_seconds=1)
    assert state["execution_state"] == "COMPLETED", state
    result = json.loads(location(config, "study", f"results/{plan['plan_id']}/statistics.json").read_text(encoding="utf-8"))
    assert result["n"] == 4
    if template == "descriptive":
        assert result["statistics"]["y"]["mean"] == 6
    elif template == "correlation":
        assert result["statistics"]["pearson_r"] == pytest.approx(1)
        assert result["statistics"]["spearman_rho"] == pytest.approx(1)
    else:
        assert result["statistics"]["slope"] == pytest.approx(2)
        assert result["statistics"]["intercept"] == pytest.approx(1)
    report = reports.create(config, "study", "可追溯结果 <script>alert(1)</script>", plan_ids=[plan["plan_id"]])
    bundle = reports.export_path(config, "study", report["report_id"], "zip")
    assert reports.verify(bundle)["valid"]
    with zipfile.ZipFile(bundle) as archive:
        assert not any("approv" in n or "settings" in n or "receipt" in n for n in archive.namelist())
        assert "execution_receipt" not in archive.read(f"analyses/{plan['plan_id']}/run.json").decode()
        page = archive.read("report.html").decode()
        assert "<script>" not in page
        assert "&lt;script&gt;" in page
    store.write_file("study", "data/input.csv", "x,y\n1,2\n")
    assert reports.get(config, "study", report["report_id"])["stale"]


@pytest.mark.parametrize("text,missing,match", [
    ("x,y\n1,2\n,4\n", "error", "Missing"),
    ("x,y\n1,2\n1,3\n1,4\n", "drop", "nonconstant"),
    ("x,y\n1,NaN\n2,3\n3,4\n", "drop", "Nonfinite"),
    ("x,y\n,\n", "drop", "No complete")])
def test_template_edge_cases(center, text, missing, match):
    config, store = center
    store.write_file("study", "data/input.csv", text)
    prepared = analysis.prepare_template(config, "study", "data/input.csv", "regression", ["x", "y"], missing)
    root = store._project_dir("study")
    result = subprocess.run([sys.executable, "-I", prepared["spec"]["script"]], cwd=root, capture_output=True, text=True)
    assert result.returncode != 0
    assert match in result.stderr


def test_real_profile_and_report_worker(center):
    config, store = center
    store.write_file("study", "data/input.csv", "x,y\n1,2\n3,4\n")
    task = tasks.enqueue(config, "study", "profile", {"file": "data/input.csv"})
    def wait(task):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            row = tasks.get(config, task["task_id"])
            if row["status"] in {"completed", "failed"}:
                assert row["status"] == "completed", row
                return row
            time.sleep(.1)
        pytest.fail(str(row))
    assert wait(task)["result"]["rows"] == 2
    report = tasks.enqueue(config, "study", "report", {"question": "检查结果", "profile_task_ids": [task["task_id"]]})
    row = wait(report)
    assert reports.verify(reports.export_path(config, "study", row["result"]["report_id"], "zip"))["valid"]


def test_compare_and_glossary_require_per_document_evidence(center):
    config, _ = center
    doc, block = seed_evidence(config, import_pdf(config))
    selected = [{"document_id": doc["document_id"], "revision": doc["revision"]}]
    evidence = literature.retrieve(config, "study", selected, "slope")
    claim = {"text": "Slope 2", "citations": [block["citation_id"]]}
    row = {"document_id": doc["document_id"], "cells": {key: claim for key in ("研究问题", "方法", "数据", "主要结果", "局限性")}}
    result, citations = literature.validate_answer("compare", {"rows": [row]}, evidence, selected)
    assert len(citations) == 1
    result, citations = literature.validate_answer("glossary", {"terms": [{"term": "slope", "definition": claim}]}, evidence, selected)
    assert result["terms"][0]["definition"]["citations"]
    with pytest.raises(ValidationError):
        literature.validate_answer("compare", {"rows": [row, row]}, evidence, selected)


def test_export_verification_rejects_tamper_and_traversal(tmp_path):
    from rsci.research.reports import verify
    path = tmp_path / "bad.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../escape", "evil")
        archive.writestr("manifest.json", json.dumps({"schema": "rsci.reproduction/1", "files": {"../escape": "a"}}))
    assert not verify(path)["valid"]
