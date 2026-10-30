"""Research guides reach the agent without starting computations during authoring."""
import pytest

from rsci.agents.research_workflows import ResearchWorkflowStore
from tests.test_ui import ui, call, sse_chat  # noqa: F401


def save_guide(ui):
    return call(ui, "research_workflow.save", {
        "title": "吸附能", "direction": "比较二维材料的吸附位点",
        "content": "先优化基底和吸附体系，再汇总能量差。标记：ADSORPTION_GUIDE",
    })["workflow"]


def test_generate_edit_save_and_delete(ui, monkeypatch):
    provider = ui["scripted"]
    provider.script = [{"text": "## 方法\n先优化结构，再计算能量差。"}]
    monkeypatch.setattr("rsci.providers.provider_by_id",
                        lambda config, pid: (config.load_providers()[0], provider))
    draft = call(ui, "research_workflow.generate", {
        "provider": "mock", "direction": "吸附能比较", "title": "吸附能"})
    assert "先优化" in draft["content"]
    assert call(ui, "research_workflow.list")["workflows"] == []
    saved = save_guide(ui)
    updated = call(ui, "research_workflow.save", {
        **saved, "content": draft["content"] + "\n汇总结果表。"})["workflow"]
    assert updated["id"] == saved["id"]
    store = ResearchWorkflowStore(ui["app"].config.home / "research-workflows")
    assert "汇总结果表" in store.read(saved["id"])["content"]
    assert len(store.list()) == 1
    assert call(ui, "research_workflow.delete", {"id": saved["id"]})["ok"]
    assert store.list() == []


@pytest.mark.parametrize("mode", ["manual", "auto", "off"])
def test_selection_reaches_agent_and_survives_reload(ui, mode):
    workflow = save_guide(ui)
    sid = call(ui, "chat.new")["session_id"]
    selection = {"mode": mode, "id": workflow["id"] if mode == "manual" else ""}
    ui["scripted"].script = ([{"text": workflow["id"]}] if mode == "auto" else []) + [
        {"text": "开始分析。"}]
    frames = sse_chat(ui, {"provider": "mock", "message": "分析吸附能",
                           "session_id": sid, "research_workflow": selection})
    assert frames[-1]["result"]["ok"]
    active = next(f for f in frames if f["type"] == "research_workflow")
    prompt = ui["scripted"].calls[-1][0]["content"]
    assert ("ADSORPTION_GUIDE" in prompt) == (mode != "off")
    assert active["id"] == (workflow["id"] if mode != "off" else "")
    loaded = call(ui, "chat.load", {"session_id": sid})
    assert loaded["research_workflow"] == selection
    assert loaded["messages"][-1]["research_workflow"]["mode"] == mode
    assert call(ui, "chat.fork", {"session_id": sid})["research_workflow"] == selection
    ui["scripted"].script = ([{"text": workflow["id"]}] if mode == "auto" else []) + [{"text": "继续。"}]
    assert sse_chat(ui, {"provider": "mock", "message": "继续", "session_id": sid})[-1]["result"]["ok"]


def test_auto_can_decline_and_off_overrides_previous_workflow(ui):
    workflow = save_guide(ui)
    sid = call(ui, "chat.new")["session_id"]
    ui["scripted"].script = [{"text": "开始。"}, {"text": "none"}, {"text": "你好。"}, {"text": "普通对话。"}]
    sse_chat(ui, {"provider": "mock", "message": "做吸附能", "session_id": sid,
                  "research_workflow": {"mode": "manual", "id": workflow["id"]}})
    frames = sse_chat(ui, {"provider": "mock", "message": "你好", "session_id": sid,
                           "research_workflow": {"mode": "auto"}})
    assert next(f for f in frames if f["type"] == "research_workflow")["id"] == ""
    assert "ADSORPTION_GUIDE" not in ui["scripted"].calls[-1][0]["content"]
    sse_chat(ui, {"provider": "mock", "message": "直接回答", "session_id": sid,
                  "research_workflow": {"mode": "off"}})
    assert "No research workflow is active" in ui["scripted"].calls[-1][0]["content"]


def test_deleted_manual_workflow_does_not_silently_execute(ui):
    workflow = save_guide(ui)
    call(ui, "research_workflow.delete", {"id": workflow["id"]})
    sid = call(ui, "chat.new")["session_id"]
    frames = sse_chat(ui, {"provider": "mock", "message": "执行", "session_id": sid,
                           "research_workflow": {"mode": "manual", "id": workflow["id"]}})
    assert not frames[-1]["result"]["ok"]
    assert ui["scripted"].calls == []
