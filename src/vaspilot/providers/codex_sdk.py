"""Codex provider via the Node bridge.

The bridge (``codex_bridge.mjs``) drives either the official Codex SDK or the
``codex`` CLI's ``--json`` event stream under a read-only sandbox. Tool
execution ALWAYS stays in this Python process: Codex only proposes calls as
one structured JSON document, which this provider parses into the same
:class:`ToolCall` shape every other protocol produces. The permission rules
therefore cannot diverge between providers.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..core.errors import ProviderError
from .base import BaseProvider, CapabilityProbe, ProviderReply, ToolCall

BRIDGE_PATH = Path(__file__).with_name("codex_bridge.mjs")

TOOL_PROTOCOL = """You are driving VASPilot, a restricted VASP/HPC agent.
You have NO shell. You act only by proposing one JSON document as your entire
reply. The available tools are listed after the conversation. Reply with
exactly one JSON object and nothing else:

  to call tools: {"action":"tool_calls","calls":[{"name":"<tool>","arguments":{...}}]}
  to finish:     {"action":"final","text":"<your answer for the user>"}

Rules:
  - never invent tool names; use only the listed tools
  - never propose a shell command; there is none
  - one JSON document per turn, no markdown fences, no prose around it
"""


def _extract_json_object(text: str) -> dict | None:
    trimmed = (text or "").strip()
    if trimmed.startswith("```"):
        # tolerate fenced output from chatty models
        lines = [ln for ln in trimmed.splitlines()
                 if not ln.strip().startswith("```")]
        trimmed = "\n".join(lines).strip()
    start = trimmed.find("{")
    end = trimmed.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(trimmed[start:end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


class CodexSdkProvider(BaseProvider):
    protocol = "codex-sdk"

    def __init__(self, entry, config=None) -> None:
        super().__init__(entry, config=config)
        self.node_binary = os.environ.get("VASPILOT_NODE", "node")
        override = os.environ.get("VASPILOT_CODEX_BRIDGE_FAKE", "").strip()
        self.bridge = Path(override) if override else BRIDGE_PATH
        self.timeout_s = int(os.environ.get("VASPILOT_CODEX_TIMEOUT", "600"))

    # -- bridge io ------------------------------------------------------------
    def _bridge_request(self, request: dict, *, timeout: int) -> list[dict]:
        """Send one request line; collect events until a terminal reply."""
        try:
            process = subprocess.Popen(
                [self.node_binary, str(self.bridge)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", cwd=str(self.bridge.parent))
        except FileNotFoundError as exc:
            raise ProviderError(
                f"node is unavailable for the Codex bridge: {exc}") from exc
        events: list[dict] = []
        terminal = threading.Event()

        def reader() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                events.append(event)
                if event.get("type") in ("final", "error", "probe_result"):
                    terminal.set()
                    return

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            assert process.stdin is not None
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        if not terminal.wait(timeout=timeout):
            process.kill()
            raise ProviderError("Codex bridge timed out")
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        stderr = ""
        try:
            stderr = (process.stderr.read() or "")[:300] \
                if process.stderr else ""
        except (OSError, ValueError):
            stderr = ""
        terminal_events = [e for e in events
                           if e.get("type") in ("final", "error", "probe_result")]
        if not terminal_events:
            raise ProviderError(
                "Codex bridge produced no terminal reply",
                detail={"stderr": stderr})
        return events

    # -- chat -------------------------------------------------------------------
    def _chat(self, messages: list[dict], tools: list[dict],
              *, stream_cb: Callable[[str], None] | None) -> ProviderReply:
        prompt = self._compose_prompt(messages, tools)
        events = self._bridge_request(
            {"id": "chat", "type": "chat", "prompt": prompt,
             "timeout_s": self.timeout_s, "model": self.entry.model},
            timeout=self.timeout_s + 60)
        reply = ProviderReply()
        final_text = ""
        for event in events:
            kind = event.get("type")
            if kind == "delta" and stream_cb:
                stream_cb(str(event.get("text") or ""))
            elif kind == "final":
                final_text = str(event.get("text") or "")
                reply.usage = event.get("usage") or {}
            elif kind == "error":
                raise ProviderError(f"codex bridge error: "
                                    f"{event.get('message', 'unknown')}")
        reply.raw_events = len(events)
        action = _extract_json_object(final_text)
        if action and action.get("action") == "tool_calls":
            calls = []
            for call in action.get("calls") or []:
                if not isinstance(call, dict):
                    continue
                name = str(call.get("name") or "")
                if not name:
                    continue
                arguments = call.get("arguments")
                if not isinstance(arguments, dict):
                    arguments = {}
                calls.append(ToolCall(call_id=f"codex-{len(calls)}",
                                      name=name, arguments=arguments))
            if calls:
                reply.tool_calls = calls
                reply.finish = "tools"
                reply.text = ""
                return reply
        if action and action.get("action") == "final" and \
                isinstance(action.get("text"), str):
            reply.text = action["text"]
        else:
            reply.text = final_text
        return reply

    def _compose_prompt(self, messages: list[dict], tools: list[dict]) -> str:
        lines = [TOOL_PROTOCOL, "## Conversation"]
        for message in messages:
            role = str(message.get("role", "user"))
            content = message.get("content")
            if isinstance(content, list):
                content = " ".join(str(part) for part in content)
            lines.append(f"[{role}] {content}")
        lines.append("## Available tools")
        if tools:
            lines.append(json.dumps(tools, ensure_ascii=False, indent=2))
        else:
            lines.append("(none — reply with the final action)")
        lines.append('## Your turn\nReply with exactly one JSON document '
                     '({"action":"tool_calls",...} or {"action":"final",...}).')
        return "\n".join(lines)

    # -- probe --------------------------------------------------------------------
    def probe(self, *, offline: bool = False) -> CapabilityProbe:
        report = CapabilityProbe(provider_id=self.entry.id,
                                 checked_at=datetime.now(timezone.utc)
                                 .isoformat(timespec="seconds"))
        try:
            events = self._bridge_request(
                {"id": "probe", "type": "probe", "offline": offline},
                timeout=360 if not offline else 60)
        except ProviderError as exc:
            report.detail = str(exc)
            return report
        result = next((e for e in events if e.get("type") == "probe_result"), None)
        if result is None:
            report.detail = "bridge did not return a probe_result"
            return report
        live = result.get("live") or {}
        backend = str(result.get("backend", "none"))
        if backend == "none":
            report.detail = str(result.get("detail",
                                            "no Codex backend available"))
            return report
        report.reachable = True
        if offline:
            # offline probes never claim execution capabilities
            report.detail = f"offline probe: backend={backend} " \
                            f"version={result.get('backend_version', '')}"
            return report
        report.streaming = bool(live.get("stream"))
        report.structured_json = bool(live.get("json"))
        report.tool_calling = bool(live.get("tool_call"))
        report.detail = f"backend={backend} " + str(result.get("detail", ""))
        return report
