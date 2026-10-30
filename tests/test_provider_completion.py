"""A complete-looking tool argument is not evidence of a complete response."""
import pytest

from rsci.agents.runtime import AgentRuntime
from rsci.core.config import ProviderEntry
from rsci.core.errors import ProviderError
from rsci.providers import build_provider


@pytest.mark.parametrize("protocol", ["openai-chat-compatible", "openai-responses"])
@pytest.mark.parametrize("ending", ["disconnect", "limit", "error"])
def test_incomplete_provider_turn_never_dispatches_tools(protocol, ending, monkeypatch):
    provider = build_provider(ProviderEntry(id="local", name="Local", protocol=protocol,
        base_url="http://127.0.0.1:1/v1", model="test"))
    if protocol == "openai-chat-compatible":
        events = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1",
            "function": {"name": "project_write", "arguments": '{"content":"valid JSON"}'}}]}}]}]
        if ending == "limit":
            events.append({"choices": [{"finish_reason": "length", "delta": {}}]})
        elif ending == "error":
            events.append({"error": {"message": "model failed"}})
        monkeypatch.setattr(provider, "_stream_events", lambda *args, **kwargs: iter(events))
    else:
        events = [{"type": "response.output_item.done", "item": {"type": "function_call",
            "call_id": "call-1", "name": "project_write", "arguments": '{"content":"valid JSON"}'}}]
        if ending != "disconnect":
            events.append({"type": "response.incomplete" if ending == "limit" else "response.failed"})
        monkeypatch.setattr(provider, "_sse_events", lambda *args, **kwargs: iter(events))
    class Registry:
        calls = []
        def list_tools(self):
            return []
        def dispatch(self, *args, **kwargs):
            self.calls.append(args)
            return {"ok": True}
    registry = Registry()
    with pytest.raises(ProviderError):
        AgentRuntime(provider=provider, registry=registry, mode="full").run("write a file")
    assert registry.calls == []


def test_finish_reason_without_done_is_valid_completion(monkeypatch):
    provider = build_provider(ProviderEntry(id="local", name="Local", protocol="openai-chat-compatible",
        base_url="http://127.0.0.1:1/v1", model="test"))
    monkeypatch.setattr(provider, "_stream_events", lambda *args, **kwargs: iter([
        {"choices": [{"delta": {"content": "complete answer"}, "finish_reason": "stop"}]}]))
    assert provider.chat([], []).text == "complete answer"


def test_empty_sse_does_not_silently_call_model_again(monkeypatch):
    provider = build_provider(ProviderEntry(id="local", name="Local", protocol="openai-chat-compatible",
        base_url="http://127.0.0.1:1/v1", model="test"))
    def empty(*args, state=None, **kwargs):
        state["is_sse"] = True
        return iter(())
    monkeypatch.setattr(provider, "_stream_events", empty)
    monkeypatch.setattr(provider, "_chat_blocking", lambda *args: pytest.fail("must not resubmit"))
    with pytest.raises(ProviderError, match="有效回答"):
        provider.chat([], [])
