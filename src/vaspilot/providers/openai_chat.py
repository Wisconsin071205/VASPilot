"""OpenAI Chat Completions compatible provider (DeepSeek, GLM, Ollama, LM Studio).

Zero-dependency urllib transport with SSE streaming and function tool calls.
The concrete class is protocol-generic by design — never name vendors here.
"""

from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from ..core.errors import ProviderError
from .base import (BaseProvider, CapabilityProbe, ProviderReply, ToolCall,
                   parse_tool_arguments, require_api_key_if_remote)

_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class OpenAIChatCompatibleProvider(BaseProvider):
    """``POST {base_url}/chat/completions`` with tools + streaming."""

    protocol = "openai-chat-compatible"

    def __init__(self, entry, config=None) -> None:
        super().__init__(entry, config=config)
        base = (entry.base_url or "").rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise ProviderError("provider base_url must be HTTP(S)")
        self.url = base if base.endswith("/chat/completions") \
            else base + "/chat/completions"
        # never route provider traffic through a system proxy silently
        self.opener = build_opener(ProxyHandler({}))

    # -- transport -------------------------------------------------------------
    def _request(self, payload: dict, *, stream: bool = False,
                 timeout: int = 120, retries: int = 3):
        headers = {"Content-Type": "application/json"}
        if stream:
            headers["Accept"] = "text/event-stream"
        else:
            headers["Accept"] = "application/json"
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        body = json.dumps(payload).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            self.raise_if_cancelled()
            request = Request(self.url, data=body, method="POST", headers=headers)
            try:
                return self.opener.open(request, timeout=timeout)
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
                last_error = ProviderError(
                    f"provider returned HTTP {exc.code}: {detail}")
                if exc.code in _RETRYABLE_STATUS and attempt < retries:
                    retry_after = exc.headers.get("Retry-After", "")
                    try:
                        delay = min(float(retry_after), 12.0)
                    except (TypeError, ValueError):
                        delay = min(0.75 * (2 ** attempt), 6.0)
                    self.wait_retry(delay)
                    continue
                raise last_error from exc
            except URLError as exc:
                reason = str(exc.reason)
                last_error = ProviderError(
                    "读取模型服务响应超时：已重试仍未收到完整响应；"
                    "请检查网络/VPN、服务端负载后重试"
                    if "timed out" in reason.lower()
                    else f"provider unreachable: {reason}")
                if attempt < retries:
                    self.wait_retry(min(0.75 * (2 ** attempt), 6.0))
                    continue
                raise last_error from exc
            except (TimeoutError, socket.timeout) as exc:
                last_error = ProviderError(
                    "读取模型服务响应超时：已重试仍未收到完整响应；"
                    "请检查网络/VPN、服务端负载后重试")
                if attempt < retries:
                    self.wait_retry(min(0.75 * (2 ** attempt), 6.0))
                    continue
                raise last_error from exc
        raise last_error or ProviderError("provider request failed")

    # -- streaming ----------------------------------------------------------------
    def _stream_events(self, payload: dict, timeout: int = 120):
        response = self._request(payload, stream=True, timeout=timeout)
        with self.track_response(response) as response:
            try:
                for raw_line in response:
                    self.raise_if_cancelled()
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
            except (TimeoutError, socket.timeout) as exc:
                raise ProviderError(
                    "读取模型流响应超时：模型服务在输出过程中长时间没有返回数据；"
                    "请重试或检查服务端负载") from exc

    # -- chat ------------------------------------------------------------------------
    def _chat(self, messages: list[dict], tools: list[dict],
              *, stream_cb: Callable[[str], None] | None) -> ProviderReply:
        payload: dict = {
            "model": self.entry.model,
            "messages": messages,
            "temperature": 0,
        }
        if tools:
            payload["tools"] = [
                {"type": "function", "function": tool} for tool in tools]
            payload["tool_choice"] = "auto"
        reply = ProviderReply()
        text_parts: list[str] = []
        # per streamed call: {"id": str, "name": str, "args": [fragments]}
        acc: dict[int, dict] = {}
        usage: dict = {}
        events = 0
        for event in self._stream_events({**payload, "stream": True}):
            events += 1
            usage = event.get("usage") or usage
            choices = event.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            fragment = delta.get("content")
            if isinstance(fragment, str) and fragment:
                text_parts.append(fragment)
                if stream_cb:
                    stream_cb(fragment)
            for call in delta.get("tool_calls") or []:
                try:
                    index = int(call.get("index", 0))
                except (TypeError, ValueError):
                    index = len(acc)
                slot = acc.setdefault(index, {"id": "", "name": "", "args": []})
                if call.get("id"):
                    slot["id"] = str(call["id"])
                function = call.get("function") or {}
                if function.get("name"):
                    slot["name"] = str(function["name"])
                if function.get("arguments"):
                    slot["args"].append(str(function["arguments"]))
            if choices[0].get("finish_reason"):
                reply.finish = str(choices[0]["finish_reason"])
        if not events:
            # stream unsupported or failed: single-shot fallback
            return self._chat_blocking(payload)
        merged: list[ToolCall] = []
        for index in sorted(acc):
            slot = acc[index]
            if not slot["name"]:
                continue
            merged.append(ToolCall(
                call_id=slot["id"] or f"call-{index}",
                name=slot["name"],
                arguments=parse_tool_arguments("".join(slot["args"]) or "{}")))
        if merged:
            reply.tool_calls = merged
            reply.finish = "tools"
        reply.text = "".join(text_parts)
        reply.usage = usage
        reply.raw_events = events
        return reply

    def _chat_blocking(self, payload: dict) -> ProviderReply:
        response = self._request(payload)
        with self.track_response(response) as response:
            self.raise_if_cancelled()
            try:
                document = json.load(response)
            except (TimeoutError, socket.timeout) as exc:
                raise ProviderError(
                    "读取模型服务响应超时：请检查网络/VPN、服务端负载后重试") from exc
        try:
            choice = document["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                "provider response was missing choices[0].message") from exc
        reply = ProviderReply(text=str(message.get("content") or ""))
        calls: list[ToolCall] = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            calls.append(ToolCall(
                call_id=str(call.get("id") or ""),
                name=str(function.get("name") or ""),
                arguments=parse_tool_arguments(function.get("arguments"))))
        if calls:
            reply.tool_calls = calls
            reply.finish = "tools"
        reply.usage = document.get("usage") or {}
        return reply

    # -- probe -----------------------------------------------------------------------
    def probe(self) -> CapabilityProbe:
        report = CapabilityProbe(provider_id=self.entry.id,
                                 checked_at=datetime.now(timezone.utc)
                                 .isoformat(timespec="seconds"))
        probe_tool = {
            "name": "probe_echo",
            "description": "Return the probe acknowledgement.",
            "parameters": {"type": "object",
                           "properties": {"value": {"type": "string"}},
                           "required": ["value"], "additionalProperties": False},
        }
        try:
            # 1) reachability + tool calling in one non-streaming call
            blocking = self._chat_blocking({
                "model": self.entry.model, "temperature": 0,
                "messages": [{"role": "user", "content":
                              "Call the probe_echo tool with value set to ready. "
                              "Do not answer in prose."}],
                "tools": [{"type": "function", "function": probe_tool}],
                "tool_choice": "auto",
            })
            report.reachable = True
            report.tool_calling = bool(blocking.tool_calls) and \
                all(call.arguments.get("value") == "ready"
                    for call in blocking.tool_calls[:1])
        except ProviderError as exc:
            report.detail = str(exc)
            return report
        # 2) structured JSON output
        try:
            structured = self._chat_blocking({
                "model": self.entry.model, "temperature": 0,
                "messages": [{"role": "user", "content":
                              'Reply with the JSON object {"status":"ready"} '
                              'and nothing else.'}],
                "response_format": {"type": "json_object"},
            })
            content = structured.text.strip()
            parsed = json.loads(content[content.find("{"):])
            report.structured_json = isinstance(parsed, dict) and \
                "status" in parsed
        except (ProviderError, ValueError, json.JSONDecodeError):
            report.structured_json = False
        # 3) streaming
        try:
            events = 0
            for event in self._stream_events({
                "model": self.entry.model, "temperature": 0, "stream": True,
                "messages": [{"role": "user", "content": "Say ready."}]}):
                events += 1
                if events >= 1:
                    break
            report.streaming = events >= 1
        except ProviderError:
            report.streaming = False
        required = [name for name, ok in (("streaming", report.streaming),
                                          ("tool_calling",
                                           report.tool_calling))
                    if not ok]
        if required:
            report.detail = "provider degraded: " + ",".join(required)
        elif report.structured_json:
            report.detail = "probe completed"
        else:
            # optional capability: informational, never gates write tools
            report.detail = ("probe completed; structured_json unsupported "
                             "(informational only)")
        return report
