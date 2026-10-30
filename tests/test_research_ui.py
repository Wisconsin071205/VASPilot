"""General-project UI/API and specialist tool isolation regression tests."""
import base64
import json
import urllib.request

import pytest

from rsci.agents.runtime import AgentRuntime
from rsci.core.errors import ValidationError
from rsci.workflow.research import project_store
from sample_analysis import prepare_csv_template
from tests.test_ui import call, ui


def test_default_identity_and_explicit_specialist(ui):
    app = ui["app"]
    general = call(ui, "project.create", {"name": "general"})
    assert general["kind"] == "general"
    registry = app.build_registry(project_root=general["path"])
    assert {"project_files", "research_prepare"} <= set(registry.names())
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
    template = prepare_csv_template(ui["app"].config, "csv-project", "data/data.csv")
    plan = call(ui, "workflow.prepare", {"spec": template["spec"]})
    assert plan["ok"] and plan["plan"]["domain"] == "general"
    assert plan["plan"]["job_script_content"].startswith('"""Descriptive')
    assert not ui["app"].engine().run_path(plan["plan_id"]).exists()
    refused = call(ui, "workflow.approve", {"plan_id": plan["plan_id"], "phrase": "wrong"})
    assert not refused["ok"]
    doc = call(ui, "project.read", {"name": "csv-project", "file": "data/data.csv"})
    assert doc["sha256"] == imported["sha256"]
    from pathlib import Path
    assert Path(doc["path"]) == Path(created["path"]) / "data" / "data.csv"
    assert Path(doc["path"]).read_bytes() == data
    listing = call(ui, "project.files", {"name": "csv-project"})
    assert Path(listing["path"]) == Path(created["path"])
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
    template = prepare_csv_template(app.config, "chat-data", "data/test.csv")
    result = registry.dispatch("research_prepare", {"spec": template["spec"]})
    assert result["status"] == "pending_approval"
    assert result["chat_plan"]["domain"] == "general"
    assert result["chat_plan"]["job_script_content"] == result["plan"]["job_script_content"]
    assert not app.engine().run_path(result["plan_id"]).exists()
    assert not app.config.load_approvals()["consumed"]



def test_removed_research_endpoints_and_tools(ui):
    names = set(ui["app"].build_registry().names())
    assert not names.intersection({"research_csv_template", "research_csv_preview", "literature_search", "reference_add", "reference_check", "experiment_record"})
    for action in ("research_task.list", "document.list", "report.create", "ocr.status"):
        with pytest.raises(urllib.error.HTTPError) as error:
            call(ui, action)
        assert error.value.code == 404
