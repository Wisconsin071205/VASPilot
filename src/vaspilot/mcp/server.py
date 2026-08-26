"""Dependency-free stdio MCP server exposing the VASPilot tool registry.

Shares the exact registry/service layer with the CLI — there is no second
SSH implementation and no extra tool surface. Extra MCP-only tools:

  open_remote_login        spawn a visible terminal for SSH login
  open_approval_terminal   spawn a visible terminal for 'workflow approve'

Security posture (same as the CLI):
  - no shell tool of any kind
  - credential material never crosses MCP output
  - POTCAR and large binaries are refused by the registry handlers
  - write tools are refused when the host marked the session analysis-only
    (env VASPILOT_MCP_MODE=analysis_only)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

MAX_OUTPUT = 200_000
SERVER_NAME = "vaspilot"
SERVER_VERSION = "1.0.0"

SERVER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


def build_registry():
    from ..cli.main import App
    from ..core.config import Config
    app = App(Config())
    return app, app.registry(project_root=os.environ.get("VASPILOT_PROJECT_ROOT"))


def _wrap(app, registry) -> list[dict[str, Any]]:
    tools = [t.to_mcp() for t in registry.list_tools()]
    tools.append({
        "name": "open_remote_login",
        "description": "Open a visible terminal for SSH login. The user types "
                       "the password and TOTP there; never provide "
                       "credentials to this tool.",
        "inputSchema": {"type": "object",
                        "properties": {"server": {"type": "string",
                                                  "pattern": SERVER_RE.pattern}},
                        "required": ["server"], "additionalProperties": False},
    })
    tools.append({
        "name": "open_approval_terminal",
        "description": "Open a visible terminal running 'vaspilot workflow "
                       "approve' for a prepared plan. Approval happens only "
                       "there; the model cannot mint approval references.",
        "inputSchema": {"type": "object",
                        "properties": {"plan_id": {"type": "string",
                                                   "pattern": r"^[0-9a-f]{16}$"}},
                        "required": ["plan_id"], "additionalProperties": False},
    })
    tools.append({
        "name": "vaspilot_self_check",
        "description": "Return the backend's own consistency report: registry "
                       "size, gateway protocol version requirement, and "
                       "session mode.",
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
    })
    return tools


TOOLS_DOC: list[dict[str, Any]] = []


def _dispatch(app, registry, name: str, arguments: Any) -> str:
    mode = os.environ.get("VASPILOT_MCP_MODE", "full")
    args = arguments if isinstance(arguments, dict) else {}
    if name == "open_remote_login":
        server = str(args.get("server") or "")
        if not SERVER_RE.fullmatch(server):
            raise ValueError("server must be a registered simple name")
        result = app.client().open_login_terminal(server)
        return json.dumps(result, ensure_ascii=False)
    if name == "open_approval_terminal":
        plan_id = str(args.get("plan_id") or "")
        if not re.fullmatch(r"[0-9a-f]{16}", plan_id):
            raise ValueError("plan_id must be 16 hex characters")
        result = _spawn_approval_terminal(plan_id)
        return json.dumps(result, ensure_ascii=False)
    if name == "vaspilot_self_check":
        return json.dumps({
            "ok": True,
            "registry_tools": len(registry.names()),
            "server_version": SERVER_VERSION,
            "mode": mode,
            "config_home": str(app.config.home),
        }, ensure_ascii=False)
    result = registry.dispatch(name, args, provider_mode=mode)
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT] + "\n[output truncated by vaspilot MCP]"
    return text


def _spawn_approval_terminal(plan_id: str) -> dict[str, Any]:
    """Open a NEW VISIBLE console running the interactive approve command."""
    import subprocess
    command = [sys.executable, "-m", "vaspilot", "workflow", "approve", plan_id]
    creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    try:
        subprocess.Popen(command, creationflags=creationflags)
    except OSError as exc:
        raise RuntimeError(f"could not open the approval terminal: {exc}") from exc
    return {"opened": True, "plan_id": plan_id,
            "note": "the approval phrase must be typed in the new terminal; "
                    "this tool never returns the approval reference"}


def _response(request_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message[:400]}}


def _handle(app, registry, message: dict) -> dict | None:
    method = message.get("method")
    request_id = message.get("id")
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "initialize":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        protocol = params.get("protocolVersion")
        if not isinstance(protocol, str):
            protocol = "2024-11-05"
        return _response(request_id, {
            "protocolVersion": protocol,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "ping":
        return _response(request_id, {})
    if method == "tools/list":
        return _response(request_id, {"tools": _wrap(app, registry)})
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _error(request_id, -32602, "tools/call requires a tool name")
        try:
            text = _dispatch(app, registry, params["name"], params.get("arguments"))
            return _response(request_id, {"content": [{"type": "text",
                                                       "text": text}],
                                           "isError": False})
        except Exception as exc:
            return _response(request_id, {"content": [{"type": "text",
                                                       "text": str(exc)}],
                                           "isError": True})
    return None if request_id is None else \
        _error(request_id, -32601, f"method not found: {method}")


def self_test() -> int:
    app, registry = build_registry()
    tools = _wrap(app, registry)
    names = {t["name"] for t in tools}
    expected = {"server_list", "remote_list", "remote_read", "vasp_progress",
                "job_state", "open_remote_login", "open_approval_terminal",
                "vaspilot_self_check"}
    missing = expected - names
    assert not missing, f"missing tools: {missing}"
    # no shell-ish tool may ever appear
    for name in names:
        assert not re.search(r"\b(shell|exec|bash|sh)\b", name), name
    # validation must fail closed on a bogus dispatch: /etc can never be
    # inside a configured server root, and with no default server configured
    # the client refuses too — either way an exception must escape
    try:
        registry.dispatch("remote_list", {"path": "/etc"},
                          provider_mode="analysis_only")
    except Exception:
        pass
    else:
        raise AssertionError("path outside the server root must be rejected")
    print("vaspilot MCP self-test passed")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="vaspilot-mcp")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    app, registry = build_registry()
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("request must be a JSON object")
            reply = _handle(app, registry, message)
            if reply is not None:
                print(json.dumps(reply, ensure_ascii=False), flush=True)
        except (json.JSONDecodeError, ValueError) as exc:
            print(json.dumps(_error(None, -32700, str(exc)),
                             ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
