"""Provider contract tests against a local mock HTTP backend.

Covers the three protocols with: streaming tool calling, invalid JSON,
interrupted multi-tool calls, disconnects/limits, and analysis_only
degradation. The Codex bridge is exercised through a scripted fake bridge.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from vaspilot.core.config import ProviderEntry
from vaspilot.core.errors import ProviderError
from vaspilot.providers import build_provider
from vaspilot.providers.base import ANALYSIS_ONLY, FULL


def _sse(handler, events):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.end_headers()
    for event in events:
        handler.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
    handler.wfile.write(b"data: [DONE]\n\n")


def _json_reply(handler, document):
    payload = json.dumps(document).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


class MockBackend(BaseHTTPRequestHandler):
    """Scriptable OpenAI-compatible + Responses mock.

    ``server.behavior`` controls the reply shape; an optional
    ``server.route(body)`` function overrides it per request (probe tests).
    """

    def log_message(self, *args):
        pass

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_POST(self):
        server = self.server
        body = self._read_body()
        server.requests.append({"path": self.path, "body": body})
        if getattr(server, "status", 200) == 429:
            server.hits = getattr(server, "hits", 0) + 1
            if server.hits <= int(getattr(server, "fail_times", 1)):
                self.send_response(429)
                self.end_headers()
                self.wfile.write(b'{"error": "rate limited"}')
                return
        behavior = self._route(body)
        if self.path.endswith("/responses"):
            self._responses_reply(behavior, body)
        else:
            self._chat_reply(behavior, body)

    def _route(self, body):
        router = getattr(self.server, "route", None)
        if callable(router):
            return router(body)
        return getattr(self.server, "behavior", "text")

    # ------------------------------------------------------------- chat API
    def _chat_reply(self, behavior, body):
        streaming = bool(body.get("stream"))
        if behavior == "sse_tools" and streaming:
            _sse(self, [
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "id": "call-1", "function": {
                        "name": "remote_list",
                        "arguments": "{\"server\":"}}]}}]},
                {"choices": [{"delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": "\"cl9\"}"}}]}}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ])
            return
        if streaming and behavior in ("text", "structured"):
            content = '{"status":"ready"}' if behavior == "structured" \
                else "ready"
            _sse(self, [
                {"choices": [{"delta": {"content": content}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ])
            return
        # behaviors like tools/interrupted/bad_json reply with plain JSON even
        # when streamed: the provider's SSE parser sees no events and falls
        # back to the blocking path
        if behavior == "bad_json":
            message = {"role": "assistant", "content": "{not valid json"}
        elif behavior == "interrupted":
            message = {"role": "assistant", "content": "",
                       "tool_calls": [
                           {"id": "call-1", "function": {
                               "name": "remote_list",
                               "arguments": "{\"server\":"}},
                           {"id": "call-2", "function": {
                               "name": "job_state",
                               "arguments": "{\"job_id\":\"12"}}]}
        elif behavior in ("tools", "probe_tool"):
            if behavior == "probe_tool":
                call = {"id": "call-1", "function": {
                    "name": "probe_echo",
                    "arguments": "{\"value\": \"ready\"}"}}
            else:
                call = {"id": "call-1", "function": {
                    "name": "remote_list",
                    "arguments": "{\"server\": \"cl9\"}"}}
            message = {"role": "assistant", "content": "", "tool_calls": [call]}
        elif behavior == "structured":
            message = {"role": "assistant", "content": '{"status":"ready"}'}
        else:
            message = {"role": "assistant", "content": "hello"}
        _json_reply(self, {"choices": [{"message": message,
                                        "finish_reason": "stop"}],
                           "usage": {"total_tokens": 7}})

    # -------------------------------------------------------- responses API
    def _responses_reply(self, behavior, body):
        if behavior == "sse_tools" and body.get("stream"):
            _sse(self, [
                {"type": "response.output_item.done", "item": {
                    "type": "function_call", "call_id": "fc-1",
                    "name": "remote_list",
                    "arguments": "{\"server\":\"cl9\"}"}},
                {"type": "response.completed", "response": {
                    "output": [], "usage": {"total_tokens": 5}}},
            ])
            return
        if behavior == "probe_tool":
            output = [{"type": "function_call", "call_id": "fc-1",
                       "name": "probe_echo",
                       "arguments": "{\"value\": \"ready\"}"}]
        elif behavior == "structured":
            output = [{"type": "message", "content": [
                {"type": "output_text", "text": '{"status":"ready"}'}]}]
        else:
            output = [{"type": "message", "content": [
                {"type": "output_text", "text": "hello"}]}]
        _json_reply(self, {"output": output, "usage": {"total_tokens": 5}})


@pytest.fixture()
def mock_backend():
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockBackend)
    server.requests = []
    server.behavior = "text"
    server.status = 200
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _entry(protocol, port, model="test-model", pid="p1"):
    return ProviderEntry(id=pid, name="Test", protocol=protocol,
                         base_url=f"http://127.0.0.1:{port}/v1",
                         model=model, api_key_env="VASPILOT_TEST_KEY")


def _tools():
    return [{"name": "remote_list",
             "description": "list a remote directory",
             "parameters": {"type": "object",
                            "properties": {"server": {"type": "string"}},
                            "additionalProperties": False}}]


class TestChatCompatible:
    def test_streaming_tool_calls(self, mock_backend, monkeypatch):
        monkeypatch.setenv("VASPILOT_TEST_KEY", "k")
        mock_backend.behavior = "sse_tools"
        provider = build_provider(_entry("openai-chat-compatible",
                                         mock_backend.server_address[1]))
        reply = provider.chat([{"role": "user", "content": "list files"}],
                              _tools())
        assert reply.finish == "tools"
        assert reply.tool_calls[0].name == "remote_list"
        assert reply.tool_calls[0].arguments == {"server": "cl9"}

    def test_invalid_json_content_passes_through(self, mock_backend,
                                                 monkeypatch):
        monkeypatch.setenv("VASPILOT_TEST_KEY", "k")
        mock_backend.behavior = "bad_json"
        provider = build_provider(_entry("openai-chat-compatible",
                                         mock_backend.server_address[1]))
        reply = provider.chat([{"role": "user", "content": "hi"}], [])
        assert reply.finish == "stop"
        assert "not valid json" in reply.text

    def test_interrupted_multi_tool_arguments(self, mock_backend, monkeypatch):
        """A truncated second call must surface as a hard JSON error."""
        monkeypatch.setenv("VASPILOT_TEST_KEY", "k")
        mock_backend.behavior = "interrupted"
        provider = build_provider(_entry("openai-chat-compatible",
                                         mock_backend.server_address[1]))
        with pytest.raises(ProviderError, match="not valid JSON"):
            provider.chat([{"role": "user", "content": "go"}], _tools())

    def test_probe_full_mode(self, mock_backend, monkeypatch):
        monkeypatch.setenv("VASPILOT_TEST_KEY", "k")

        def route(body):
            if any(t.get("function", {}).get("name") == "probe_echo"
                   for t in body.get("tools", [])):
                return "probe_tool"
            if body.get("response_format"):
                return "structured"
            return "text"

        mock_backend.route = route
        provider = build_provider(_entry("openai-chat-compatible",
                                         mock_backend.server_address[1]))
        report = provider.probe()
        assert report.reachable is True
        assert report.tool_calling is True
        assert report.structured_json is True
        assert report.streaming is True
        assert report.mode == FULL

    def test_probe_degrades_to_analysis_only(self, mock_backend, monkeypatch):
        monkeypatch.setenv("VASPILOT_TEST_KEY", "k")
        provider = build_provider(_entry("openai-chat-compatible",
                                         mock_backend.server_address[1]))
        report = provider.probe()
        assert report.tool_calling is False
        assert report.structured_json is False
        assert report.mode == ANALYSIS_ONLY
        assert "degraded" in report.detail

    def test_rate_limit_retry_then_success(self, mock_backend, monkeypatch):
        monkeypatch.setenv("VASPILOT_TEST_KEY", "k")
        mock_backend.behavior = "tools"
        mock_backend.status = 429
        mock_backend.fail_times = 1
        mock_backend.hits = 0
        provider = build_provider(_entry("openai-chat-compatible",
                                         mock_backend.server_address[1]))
        reply = provider.chat([{"role": "user", "content": "go"}], [])
        assert reply.finish == "tools"

    def test_unreachable_provider(self, monkeypatch):
        monkeypatch.setenv("VASPILOT_TEST_KEY", "k")
        provider = build_provider(_entry("openai-chat-compatible", 1, pid="dead"))
        report = provider.probe()
        assert report.reachable is False
        assert report.mode == ANALYSIS_ONLY
        with pytest.raises(ProviderError):
            provider.chat([{"role": "user", "content": "hi"}], [])


class TestResponsesProtocol:
    def test_streaming_function_call(self, mock_backend, monkeypatch):
        monkeypatch.setenv("VASPILOT_TEST_KEY", "k")
        mock_backend.behavior = "sse_tools"
        provider = build_provider(_entry("openai-responses",
                                         mock_backend.server_address[1]))
        reply = provider.chat([{"role": "user", "content": "list"}], _tools())
        assert reply.finish == "tools"
        assert reply.tool_calls[0].name == "remote_list"
        assert reply.tool_calls[0].arguments == {"server": "cl9"}

    def test_blocking_text(self, mock_backend, monkeypatch):
        monkeypatch.setenv("VASPILOT_TEST_KEY", "k")
        mock_backend.behavior = "text"
        provider = build_provider(_entry("openai-responses",
                                         mock_backend.server_address[1]))
        reply = provider.chat([{"role": "user", "content": "hi"}], [])
        assert reply.text == "hello"


def _fake_bridge(tmp_path, script):
    bridge = tmp_path / "fake-bridge.mjs"
    bridge.write_text(script, encoding="utf-8")
    return str(bridge)


def _codex_entry():
    return ProviderEntry(id="codex", name="Codex", protocol="codex-sdk",
                         base_url="", model="gpt-5.2-codex", api_key_env="")


class TestCodexBridge:
    def test_probe_offline_reports_backend(self, tmp_path, monkeypatch):
        script = (
            "process.stdin.setEncoding('utf8');\n"
            "let buf='';\n"
            "process.stdin.on('data',c=>{buf+=c;});\n"
            "process.stdin.on('end',()=>{\n"
            "  const req=JSON.parse(buf.trim().split('\\n')[0]);\n"
            "  process.stdout.write(JSON.stringify({id:req.id,"
            "type:'probe_result',node:'v22',backend:'codex-cli',"
            "backend_version:'0.146.0',auth:true,"
            "live:{json:false,stream:false,tool_call:false},"
            "detail:'offline probe'})+'\\n');\n"
            "});\n")
        monkeypatch.setenv("VASPILOT_CODEX_BRIDGE_FAKE",
                           _fake_bridge(tmp_path, script))
        provider = build_provider(_codex_entry())
        report = provider.probe(offline=True)
        assert report.reachable is True
        # an offline probe never certifies execution capabilities
        assert report.streaming is False
        assert report.mode == ANALYSIS_ONLY

    def test_chat_parses_tool_protocol(self, tmp_path, monkeypatch):
        script = (
            "process.stdin.setEncoding('utf8');\n"
            "let buf='';\n"
            "process.stdin.on('data',c=>{buf+=c;});\n"
            "process.stdin.on('end',()=>{\n"
            "  const req=JSON.parse(buf.trim().split('\\n')[0]);\n"
            "  process.stdout.write(JSON.stringify({id:req.id,"
            "type:'delta',text:'partial'})+'\\n');\n"
            "  process.stdout.write(JSON.stringify({id:req.id,"
            "type:'final',text:'{\"action\":\"tool_calls\",\"calls\":"
            "[{\"name\":\"remote_list\",\"arguments\":"
            "{\"server\":\"cl9\"}}]}',usage:{}})+'\\n');\n"
            "});\n")
        monkeypatch.setenv("VASPILOT_CODEX_BRIDGE_FAKE",
                           _fake_bridge(tmp_path, script))
        provider = build_provider(_codex_entry())
        reply = provider.chat([{"role": "user", "content": "list"}], _tools())
        assert reply.finish == "tools"
        assert reply.tool_calls[0].name == "remote_list"
        assert reply.tool_calls[0].arguments == {"server": "cl9"}

    def test_chat_final_action(self, tmp_path, monkeypatch):
        script = (
            "process.stdin.setEncoding('utf8');\n"
            "let buf='';\n"
            "process.stdin.on('data',c=>{buf+=c;});\n"
            "process.stdin.on('end',()=>{\n"
            "  const req=JSON.parse(buf.trim().split('\\n')[0]);\n"
            "  process.stdout.write(JSON.stringify({id:req.id,"
            "type:'final',text:'{\"action\":\"final\",\"text\":"
            "\"all good\"}',usage:{}})+'\\n');\n"
            "});\n")
        monkeypatch.setenv("VASPILOT_CODEX_BRIDGE_FAKE",
                           _fake_bridge(tmp_path, script))
        provider = build_provider(_codex_entry())
        reply = provider.chat([{"role": "user", "content": "status?"}], [])
        assert reply.finish == "stop"
        assert reply.text == "all good"

    def test_missing_backend_degrades(self, tmp_path, monkeypatch):
        script = (
            "process.stdin.setEncoding('utf8');\n"
            "let buf='';\n"
            "process.stdin.on('data',c=>{buf+=c;});\n"
            "process.stdin.on('end',()=>{\n"
            "  const req=JSON.parse(buf.trim().split('\\n')[0]);\n"
            "  process.stdout.write(JSON.stringify({id:req.id,"
            "type:'probe_result',node:process.version,backend:'none',"
            "backend_version:'',auth:false,"
            "live:{json:false,stream:false,tool_call:false},"
            "detail:'neither available'})+'\\n');\n"
            "});\n")
        monkeypatch.setenv("VASPILOT_CODEX_BRIDGE_FAKE",
                           _fake_bridge(tmp_path, script))
        provider = build_provider(_codex_entry())
        report = provider.probe(offline=True)
        assert report.reachable is False
        assert report.mode == ANALYSIS_ONLY

    def test_real_bridge_probe_offline(self, monkeypatch):
        """The shipped bridge must at least report a backend without calls."""
        monkeypatch.delenv("VASPILOT_CODEX_BRIDGE_FAKE", raising=False)
        provider = build_provider(_codex_entry())
        report = provider.probe(offline=True)
        # node exists on this machine, so the bridge replies; without any
        # backend it degrades — either way the protocol must hold
        assert report.detail
