"""General-project UI/API and specialist tool isolation regression tests."""
import base64
import json
import urllib.request

import pytest

from rsci.agents.runtime import AgentRuntime
from rsci.core.errors import ValidationError
from rsci.workflow.research import project_store
from tests.test_ui import call, ui


def test_default_identity_and_explicit_specialist(ui):
    app = ui["app"]
    general = call(ui, "project.create", {"name": "general"})
    assert general["kind"] == "general"
    registry = app.build_registry(project_root=general["path"])
    assert {"project_files", "research_csv_template", "research_prepare"} <= set(registry.names())
    assert not any(n.startswith(("vasp_", "vaspkit_", "campaign_", "potcar_")) for n in registry.names())
    with pytest.raises(ValidationError):
        registry.dispatch("vasp_validate", {"directory": "/anywhere"})
    runtime = AgentRuntime(provider=ui["scripted"], registry=registry, mode="full")
    assert "RSCI 科研助手" in runtime.system_prompt
    assert "Active specialist module" not in runtime.system_prompt
    old = call(ui, "project.create", {"name": "materials", "kind": "vasp"})
    specialized = app.build_registry(project_root=old["path"])
    assert {"workflow_prepare", "vasp_progress", "campaign_plan"} <= set(specialized.names())
    runtime = AgentRuntime(provider=ui["scripted"], registry=specialized, mode="full")
    assert "Active specialist module" in runtime.system_prompt
    assert call(ui, "project.list")["projects"][0]["kind"] == "general"


def test_ui_import_edit_plan_approval_and_safe_download(ui):
    created = call(ui, "project.create", {"name": "csv-project"})
    assert created["kind"] == "general"
    data = b"x,y\n1,2\n3,4\n"
    body = {"name": "csv-project", "file": "data/data.csv", "content_base64": base64.b64encode(data).decode()}
    imported = call(ui, "project.import", body)
    assert imported["ok"]
    assert not call(ui, "project.import", body)["ok"]  # no silent overwrite
    assert not call(ui, "project.import", {**body, "file": "../escape.csv"})["ok"]
    assert not call(ui, "project.import", {**body, "content_base64": "!!!"})["ok"]
    template = call(ui, "project.csv_template", {"name": "csv-project", "file": "data/data.csv"})
    assert template["ok"]
    assert not call(ui, "project.csv_template", {"name": "csv-project", "file": "data/data.csv"})["ok"]
    plan = call(ui, "workflow.prepare", {"spec": template["spec"]})
    assert plan["ok"] and plan["plan"]["domain"] == "general"
    assert plan["plan"]["job_script_content"].startswith('"""Descriptive')
    assert not ui["app"].engine().run_path(plan["plan_id"]).exists()
    refused = call(ui, "workflow.approve", {"plan_id": plan["plan_id"], "phrase": "wrong"})
    assert not refused["ok"]
    doc = call(ui, "project.read", {"name": "csv-project", "file": "data/data.csv"})
    assert doc["sha256"] == imported["sha256"]
    assert not call(ui, "project.write", {"name": "csv-project", "file": "data/data.csv",
                                          "content": "overwrite", "expected_sha256": "missing"})["ok"]
    request = urllib.request.Request(ui["base"] + "/api/project.download",
        data=json.dumps({"name": "csv-project", "file": "data/data.csv"}).encode(),
        headers={"Content-Type": "application/json", "X-RSCI-Token": ui["token"]})
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.read() == data
        assert response.headers["Content-Type"] == "application/octet-stream"
        assert "attachment" in response.headers["Content-Disposition"]


def test_chat_research_prepare_binds_active_project(ui):
    app = ui["app"]
    store = project_store(app.config)
    created = store.create("chat-data")
    store.write_file("chat-data", "data/test.csv", "x\n1\n")
    registry = app.build_registry(project_root=created["path"])
    template = registry.dispatch("research_csv_template", {"file": "data/test.csv"})
    result = registry.dispatch("research_prepare", {"spec": template["spec"]})
    assert result["status"] == "pending_approval"
    assert result["chat_plan"]["domain"] == "general"
    assert result["chat_plan"]["job_script_content"] == result["plan"]["job_script_content"]
    assert not app.engine().run_path(result["plan_id"]).exists()
    assert not app.config.load_approvals()["consumed"]


def test_knowledge_api_evidence_and_experiment(ui, monkeypatch):
    from rsci.workflow import knowledge
    call(ui, "project.create", {"name": "knowledge"})
    row = {"DOI": "10.1234/synthetic", "title": ["Synthetic evidence"]}
    monkeypatch.setattr(knowledge, "_fetch", lambda url: {"items": [row]} if "?" in url else row)
    search = call(ui, "project.literature_search", {"query": "synthetic"})
    assert search["verification_status"] == "search_results_only"
    ref = call(ui, "project.reference_add", {"name": "knowledge", "doi": row["DOI"], "title": row["title"][0]})
    checked = call(ui, "project.reference_check", {"name": "knowledge", "reference_id": ref["id"]})
    assert checked["status"] == "metadata_matched"
    assert checked["metadata_sha256"]
    note = call(ui, "project.experiment_record", {"name": "knowledge", "title": "Synthetic test",
        "method": "No physical experiment", "observations": "Metadata matched", "interpretation": "Not a scientific conclusion"})
    assert note["file"].startswith("notes/")
    assert note["record"]["observations"] == "Metadata matched"
    assert not call(ui, "project.reference_check", {"name": "knowledge", "reference_id": "../escape"})["ok"]
