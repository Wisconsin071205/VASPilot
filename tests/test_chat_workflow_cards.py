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
    from sample_analysis import prepare_csv_template
    spec = prepare_csv_template(ui["app"].config, "cards", "data/data.csv")["spec"]
    sid = call(ui, "chat.new", {"project":"cards"})["session_id"]
    ui["scripted"].script = [{"tool_calls":[ToolCall(
        call_id="plan", name="research_prepare", arguments={"spec":spec})]}]
    frames = sse_chat(ui, {"provider":"mock", "session_id":sid, "message":"prepare only", "request_id":"cards"})
    tool = next(frame for frame in frames if frame["type"] == "tool")
    pid = tool["outcome"]["chat_plan"]["plan_id"]
    assert tool["tool"] == "research_prepare" and frames[-1]["result"]["ok"]
    for action in ("chat.load", "chat.status"):
        saved = call(ui, action, {"session_id":sid})
        assert saved["workflow_plans"] == [pid]
        assert saved["workflow_plan_anchors"][pid]["request_id"] == "cards"
        assert saved["workflow_plan_anchors"][pid]["message_index"] == 1
        assert saved["workflow_plan_anchors"][pid] == tool["outcome"]["chat_plan"]["chat_anchor"]
    assert ConversationStore(ui["app"].config.chat_dir).snapshot(sid)["workflow_plans"] == [pid]
    assert call(ui, "workflow.get", {"plan_id":pid})["run"] is None
    assert not ui["app"].engine().run_path(pid).exists()
    assert not ui["app"].config.load_approvals()["consumed"]
    assert len(ui["scripted"].calls) == 1


def test_plan_position_survives_later_turns_compaction_and_fork(tmp_path):
    store = ConversationStore(tmp_path)
    sid = store.create_session()["session_id"]
    pid = "0123456789abcdef"
    store.begin_turn(sid, "original", "prepare")
    first = store.bind_workflow_plan(sid, pid)["workflow_plan_anchors"][pid]
    store.update_turn(sid, "original", answer="prepared", status="completed")
    for i in range(35):
        store.begin_turn(sid, f"later-{i}", "continue")
        store.update_turn(sid, f"later-{i}", answer="done", status="completed")
    # Preparing an identical plan again must not move its existing card.
    store.bind_workflow_plan(sid, pid)
    store.compact(sid)
    for saved in (ConversationStore(tmp_path).load(sid), store.fork(sid)):
        assert saved["workflow_plan_anchors"][pid] == first
        assert first["request_id"] == "original"
        rows = saved["archived_messages"] + saved["messages"]
        assert rows[first["message_index"]]["request_id"] == "original"
        assert rows[first["message_index"]]["role"] == "assistant"


def test_submit_card_keeps_original_reply_after_reload_and_regeneration(ui):
    from rsci.workflow.pending import PendingSubmitStore
    app = ui["app"]
    app.config.set_agent_submit_mode("confirm")
    project_store(app.config).create("submit-cards", kind="vasp")
    sid = call(ui, "chat.new", {"project": "submit-cards"})["session_id"]
    ui["scripted"].script = [{"tool_calls": [ToolCall(
        call_id="submit", name="job_submit", arguments={
            "server": "cl9", "directory": "/case", "script": "run.sh"})]},
        {"text": "请审阅提交卡片。"}]
    frames = sse_chat(ui, {"provider": "mock", "session_id": sid, "message": "prepare submit",
                           "request_id": "submit-turn"})
    assert any(frame["type"] == "pending" for frame in frames), frames
    entry = next(frame for frame in frames if frame["type"] == "pending")["pending"][0]
    anchor = entry["chat_anchor"]
    assert anchor["request_id"] == "submit-turn" and anchor["message_index"] == 1
    memory = ConversationStore(app.config.chat_dir)
    memory.append(sid, "user", "a later question")
    memory.append(sid, "assistant", "a later reply")
    assert call(ui, "submit.history", {"session_id": sid})["entries"][0]["chat_anchor"] == anchor
    store = PendingSubmitStore(app.config.pending_submits_path)
    store.settle(entry["id"], "expired")
    regenerated = call(ui, "submit.regenerate", {"id": entry["id"]})
    assert regenerated["ok"] and regenerated["entry"]["chat_anchor"] == anchor
    assert not ui["state"].jobs["cl9"]


def test_frontend_cards_recovery_races_and_table_headers():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js required for frontend behavior checks")
    root = Path(__file__).resolve().parents[1]
    script = root / "tests/frontend/chat_workflow_cards.cjs"
    result = subprocess.run([node, str(script), str(root / "src/rsci/ui/static/index.html")],
                            capture_output=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
