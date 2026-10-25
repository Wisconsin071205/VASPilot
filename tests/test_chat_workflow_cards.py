"""Durable approval cards and Markdown table rendering; no paid model calls."""
import concurrent.futures
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from rsci.agents.memory import ConversationStore
from rsci.core.errors import ValidationError
from rsci.providers.base import ToolCall
from rsci.workflow.research import project_store
from tests.test_ui import call, sse_chat, ui  # noqa: F401


def test_plan_links_are_atomic_idempotent_and_independent_of_messages(tmp_path):
    store = ConversationStore(tmp_path)
    sid = store.create_session()["session_id"]
    ids = [f"{i:016x}" for i in range(24)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda pid: store.bind_workflow_plan(sid, pid), ids * 2))
    for i in range(65):
        store.append(sid, "user", str(i))
    store.compact(sid)
    assert set(ConversationStore(tmp_path).snapshot(sid)["workflow_plans"]) == set(ids)
    assert len(store.load(sid)["workflow_plans"]) == len(ids)
    assert store.fork(sid)["workflow_plans"] == store.load(sid)["workflow_plans"]
    other = store.create_session()["session_id"]
    assert store.snapshot(other)["workflow_plans"] == []
    with pytest.raises(ValidationError):
        store.bind_workflow_plan(sid, "../escape")
    store.clear(sid)
    assert store.load(sid) is None


def test_research_plan_survives_sse_and_session_reload_without_execution(ui):
    store = project_store(ui["app"].config)
    store.create("cards")
    store.write_file("cards", "data/data.csv", "x\n1\n")
    registry = ui["app"].build_registry(project_root=store._project_dir("cards"))
    spec = registry.dispatch("research_csv_template", {"file":"data/data.csv"})["spec"]
    sid = call(ui, "chat.new", {"project":"cards"})["session_id"]
    ui["scripted"].script = [{"tool_calls":[ToolCall(
        call_id="plan", name="research_prepare", arguments={"spec":spec})]}]
    frames = sse_chat(ui, {"provider":"mock", "session_id":sid, "message":"prepare only", "request_id":"cards"})
    tool = next(frame for frame in frames if frame["type"] == "tool")
    pid = tool["outcome"]["chat_plan"]["plan_id"]
    assert tool["tool"] == "research_prepare" and frames[-1]["result"]["ok"]
    for action in ("chat.load", "chat.status"):
        assert call(ui, action, {"session_id":sid})["workflow_plans"] == [pid]
    assert ConversationStore(ui["app"].config.chat_dir).snapshot(sid)["workflow_plans"] == [pid]
    assert call(ui, "workflow.get", {"plan_id":pid})["run"] is None
    assert not ui["app"].engine().run_path(pid).exists()
    assert not ui["app"].config.load_approvals()["consumed"]
    assert len(ui["scripted"].calls) == 1


def test_frontend_cards_recovery_races_and_table_headers():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required for frontend behavior checks")
    root = Path(__file__).resolve().parents[1]
    script = root / "tests/frontend/chat_workflow_cards.cjs"
    result = subprocess.run([node, str(script), str(root / "src/rsci/ui/static/index.html")],
                            capture_output=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
