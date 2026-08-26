"""OpenAI Responses API provider.

``POST {base_url}/responses`` with ``tools: [{type: "function", ...}]`` and
``stream: true``. Event shapes handled:

  - response.output_text.delta        -> streamed text
  - response.output_item.done         -> message / function_call items
  - response.function_call_arguments.delta -> argument fragments
  - response.completed                -> final usage
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from ..core.errors import ProviderError
from .base import BaseProvider, CapabilityProbe, ProviderReply, ToolCall, parse_tool_arguments


class OpenAIResponsesProvider(BaseProvider):
    protocol = "openai-responses"

    def __init__(self, entry) -> None:
        super().__init__(entry)
        base = (entry.base_url or "").rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise ProviderError("provider base_url must be HTTP(S)")
        self.url = base if base.endswith("/responses") else base + "/responses"
        self.opener = build_opener(ProxyHandler({}))

    def _post(self, payload: dict, *, timeout: int = 120):
        headers = {"Content-Type": "application/json",
                   "Accept": "text/event-stream" if payload.get("stream")
                   else "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        request = Request(self.url, data=json.dumps(payload).encode("utf-8"),
                          method="POST", headers=headers)
        return self.opener.open(request, timeout=timeout)

    def _sse_events(self, payload: dict, timeout: int = 120):
        with self._post({**payload, "stream": True}, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    yield event

    def _chat(self, messages: list[dict], tools: list[dict],
              *, stream_cb: Callable[[str], None] | None) -> ProviderReply:
        payload: dict = {
            "model": self.entry.model,
            "input": messages,
        }
        if tools:
            payload["tools"] = [
                {"type": "function", "name": tool["name"],
                 "description": tool.get("description", ""),
                 "parameters": tool.get("parameters", {})}
                for tool in tools]
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        usage: dict = {}
        events = 0
        # function_call items can arrive whole (output_item.done) or as
        # argument deltas; accumulate per call_id
        arg_fragments: dict[str, list[str]] = {}
        pending: dict[str, dict] = {}
        for event in self._sse_events(payload):
            events += 1
            kind = event.get("type", "")
            if kind == "response.output_text.delta":
                fragment = str(event.get("delta") or "")
                if fragment:
                    text_parts.append(fragment)
                    if stream_cb:
                        stream_cb(fragment)
            elif kind == "response.function_call_arguments.delta":
                call_id = str(event.get("item_id") or event.get("output_index")
                              or event.get("call_id") or "")
                arg_fragments.setdefault(call_id, []).append(
                    str(event.get("delta") or ""))
            elif kind == "response.output_item.done":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    call_id = str(item.get("call_id") or item.get("id") or "")
                    name = str(item.get("name") or "")
                    raw_args = item.get("arguments")
                    if call_id in arg_fragments and not raw_args:
                        raw_args = "".join(arg_fragments[call_id])
                    pending[call_id or name] = {
                        "call_id": call_id, "name": name,
                        "arguments": parse_tool_arguments(raw_args or "{}")}
            elif kind == "response.completed":
                response = event.get("response") or {}
                usage = response.get("usage") or usage
                for item in (response.get("output") or []):
                    if item.get("type") == "function_call" and item.get("call_id"):
                        call_id = str(item["call_id"])
                        if call_id not in [c["call_id"] for c in pending.values()]:
                            pending[call_id] = {
                                "call_id": call_id,
                                "name": str(item.get("name") or ""),
                                "arguments": parse_tool_arguments(
                                    item.get("arguments") or "{}")}
        calls = [ToolCall(**slot) for slot in pending.values() if slot["name"]]
        reply = ProviderReply(text="".join(text_parts))
        if calls:
            reply.tool_calls = calls
            reply.finish = "tools"
        reply.usage = usage
        reply.raw_events = events
        if not events:
            return self._chat_blocking(payload)
        return reply

    def _chat_blocking(self, payload: dict) -> ProviderReply:
        with self._post({k: v for k, v in payload.items() if k != "stream"}) as response:
            document = json.load(response)
        reply = ProviderReply()
        calls: list[ToolCall] = []
        for item in document.get("output") or []:
            if item.get("type") == "message":
                for part in item.get("content") or []:
                    if part.get("type") in ("output_text", "text"):
                        reply.text += str(part.get("text") or "")
            elif item.get("type") == "function_call":
                calls.append(ToolCall(
                    call_id=str(item.get("call_id") or item.get("id") or ""),
                    name=str(item.get("name") or ""),
                    arguments=parse_tool_arguments(item.get("arguments") or "{}")))
        if calls:
            reply.tool_calls = calls
            reply.finish = "tools"
        reply.usage = document.get("usage") or {}
        return reply

    def probe(self) -> CapabilityProbe:
        report = CapabilityProbe(provider_id=self.entry.id,
                                 checked_at=datetime.now(timezone.utc)
                                 .isoformat(timespec="seconds"))
        probe_tool = {
            "type": "function", "name": "probe_echo",
            "description": "Return the probe acknowledgement.",
            "parameters": {"type": "object",
                           "properties": {"value": {"type": "string"}},
                           "required": ["value"], "additionalProperties": False},
        }
        try:
            blocking = self._chat_blocking({
                "model": self.entry.model,
                "input": [{"role": "user", "content":
                           "Call the probe_echo function with value=ready. "
                           "Do not answer in prose."}],
                "tools": [probe_tool],
            })
            report.reachable = True
            report.tool_calling = bool(blocking.tool_calls)
        except (ProviderError, HTTPError, URLError, OSError, ValueError) as exc:
            report.detail = str(exc)
            return report
        try:
            structured = self._chat_blocking({
                "model": self.entry.model,
                "input": [{"role": "user", "content":
                           'Reply with the JSON object {"status":"ready"} '
                           'and nothing else.'}],
                "text": {"format": {"type": "json_object"}},
            })
            content = structured.text.strip()
            parsed = json.loads(content[content.find("{"):])
            report.structured_json = isinstance(parsed, dict) and "status" in parsed
        except (ProviderError, HTTPError, URLError, OSError, ValueError,
                json.JSONDecodeError):
            report.structured_json = False
        try:
            events = 0
            for _event in self._sse_events({
                "model": self.entry.model,
                "input": [{"role": "user", "content": "Say ready."}]}):
                events += 1
                break
            report.streaming = events >= 1
        except (ProviderError, HTTPError, URLError, OSError):
            report.streaming = False
        report.detail = "probe completed" if report.full else \
            "provider degraded: " + ",".join(
                name for name, ok in (("streaming", report.streaming),
                                      ("tool_calling", report.tool_calling),
                                      ("structured_json", report.structured_json))
                if not ok)
        return report
