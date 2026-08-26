"""Local web UI for VASPilot — one more view over the SAME service layer.

Security model:
  - binds 127.0.0.1 only; no remote access
  - every page load gets a fresh random session token; all /api/* calls must
    carry it in the X-Vaspilot-Token header (blocks drive-by localhost pages)
  - the UI never sees or accepts passwords, TOTP, API keys or private keys;
    interactive SSH login opens a SEPARATE visible terminal (system terminal)
  - workflow approval still requires the human to type the confirmation
    phrase ("approve <plan_id>") — the phrase is verified server-side exactly
    like the CLI does; a model cannot forge it
  - write/scheduler actions flow through the same registry enforcement
    (analysis_only providers are refused) and the same audit log

Routes: /t/<token> serves the single-page app; /api/* is JSON-in/JSON-out
except /api/agent/chat, which streams Server-Sent-Events frames.
"""

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, parse_qs

from .. import __version__
from ..core.errors import VaspilotError

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 1 << 20  # 1 MiB request cap


class UiState:
    """Shared per-server state: app wiring, token, background runs."""

    def __init__(self, app) -> None:
        self.app = app
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.runs: dict[str, dict[str, Any]] = {}  # plan_id -> thread info


def build_state(app) -> UiState:
    return UiState(app)


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


class UiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "vaspilot-ui/" + __version__

    # injected by serve()
    state: UiState = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):  # quiet default access log
        pass

    # ------------------------------------------------------------- helpers
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        if status == 200:
            payload.setdefault("ok", True)
        self._send(status, _json_bytes(payload), "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0 or length > MAX_BODY:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _authorized(self) -> bool:
        return self.headers.get("X-Vaspilot-Token", "") == self.state.token

    def _sse_frame(self, payload: dict) -> None:
        chunk = f"data: {_json_bytes(payload).decode('utf-8')}\n\n"
        self.wfile.write(chunk.encode("utf-8"))
        self.wfile.flush()

    # --------------------------------------------------------------- verbs
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/t/"):
            token = path[len("/t/"):]
            if token != self.state.token:
                self._send(403, b"session expired; relaunch 'vaspilot ui'",
                           "text/plain; charset=utf-8")
                return
            page = (STATIC_DIR / "index.html").read_bytes()
            self._send(200, page, "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self._send(404, b"", "image/x-icon")
            return
        if path == "/healthz":
            self._send_json({"ok": True, "version": __version__})
            return
        if path.startswith("/api/") and not self._authorized():
            self._send_json({"ok": False, "error": {
                "code": "unauthorized", "message": "missing or wrong token"}},
                status=403)
            return
        if path == "/api/workflow/status":
            query = parse_qs(parsed.query)
            self._handle_api("workflow.status",
                             {"plan_id": (query.get("plan_id") or [""])[0]})
            return
        self._send(404, _json_bytes({"ok": False, "error": {
            "code": "not_found", "message": path}}),
            "application/json; charset=utf-8")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        if not self._authorized():
            self._send_json({"ok": False, "error": {
                "code": "unauthorized", "message": "missing or wrong token"}},
                status=403)
            return
        body = self._read_json()
        if parsed.path == "/api/agent/chat":
            self._chat_stream(body)
            return
        action = parsed.path[len("/api/"):].replace("/", ".")
        self._handle_api(action, body)

    # ------------------------------------------------------------ dispatch
    def _handle_api(self, action: str, body: dict) -> None:
        state = self.state
        app = state.app
        try:
            client = app.client()
            engine = app.engine()
            if action == "state":
                self._send_json(self._state_payload())
            elif action == "monitor.snapshot":
                from ..cli.monitor import _snapshot
                self._send_json(_snapshot(app))
            elif action == "server.connect":
                # never accept credentials here: open a VISIBLE terminal
                result = client.open_login_terminal(str(body.get("server") or ""))
                self._send_json(result)
            elif action == "server.disconnect":
                self._send_json(client.disconnect(str(body.get("server") or "")))
            elif action == "server.status":
                self._send_json(client.status(str(body.get("server") or "")))
            elif action == "remote.list":
                self._send_json(client.list_dir(str(body.get("path") or ""),
                                                server=body.get("server")))
            elif action == "remote.read":
                self._send_json(client.read(str(body.get("path") or ""),
                                            server=body.get("server")))
            elif action == "job.list":
                self._send_json(client.jobs(server=body.get("server")))
            elif action == "job.recent":
                self._send_json(client.recent_jobs(server=body.get("server")))
            elif action == "vasp.progress":
                self._send_json(client.vasp_progress(
                    str(body.get("directory") or ""), server=body.get("server")))
            elif action == "workflow.prepare":
                spec = body.get("spec") or {}
                self._send_json(engine.prepare(spec))
            elif action == "workflow.approve":
                # the human typed the phrase in THIS page; verified like CLI
                phrase = str(body.get("phrase") or "")
                plan_id = str(body.get("plan_id") or "")
                self._send_json(engine.approve(plan_id, stdin_lines=[phrase]))
            elif action == "workflow.run":
                self._start_run(str(body.get("plan_id") or ""),
                                str(body.get("approval_ref") or ""),
                                poll_seconds=body.get("poll_seconds"))
            elif action == "workflow.status":
                result = engine.status(str(body.get("plan_id") or ""))
                with state.lock:
                    info = state.runs.get(str(body.get("plan_id") or ""))
                if info and info.get("error"):
                    result["worker_error"] = info["error"]
                self._send_json(result)
            elif action == "provider.probe":
                from ..providers import provider_by_id
                _, provider = provider_by_id(app.config,
                                             str(body.get("id") or ""))
                report = provider.probe()
                cache = app.config.load_settings().get("provider_probes") or {}
                cache[provider.entry.id] = report.to_dict()
                app.config.update_settings(provider_probes=cache)
                self._send_json(report.to_dict())
            else:
                self._send_json({"ok": False, "error": {
                    "code": "unknown_action", "message": action}}, status=404)
        except VaspilotError as exc:
            self._send_json({"ok": False, "error": exc.to_dict()},
                            status=200)
        except Exception as exc:  # surface unexpected errors as JSON
            self._send_json({"ok": False, "error": {
                "code": "ui_error", "message": f"{type(exc).__name__}: {exc}"}},
                status=200)

    def _state_payload(self) -> dict:
        app = self.state.app
        servers = []
        for entry in app.config.load_servers():
            servers.append({**entry.to_dict(),
                            "is_default": entry.name == app.config.default_server()})
        cached = app.config.load_settings().get("provider_probes") or {}
        providers = []
        for p in app.config.load_providers():
            probe = cached.get(p.id) if isinstance(cached.get(p.id), dict) else None
            providers.append({**p.to_dict(),
                              "is_default": p.id == app.config.default_provider(),
                              "mode": (probe or {}).get("mode"),
                              "probed_at": (probe or {}).get("checked_at")})
        return {"ok": True, "version": __version__, "servers": servers,
                "providers": providers,
                "default_provider": app.config.default_provider()}

    # ----------------------------------------------------- background runs
    def _start_run(self, plan_id: str, approval_ref: str,
                   poll_seconds=None) -> None:
        state = self.state
        app = state.app
        with state.lock:
            info = state.runs.get(plan_id)
            thread = (info or {}).get("thread")
            if thread is not None and thread.is_alive():
                self._send_json({"ok": False, "error": {
                    "code": "already_running",
                    "message": f"plan {plan_id} is already executing"}})
                return
            state.runs[plan_id] = {"alive": True}

        interval = int(poll_seconds) if poll_seconds is not None else 30

        def worker() -> None:
            try:
                app.engine().run(plan_id, approval_ref, poll_seconds=interval)
            except VaspilotError as exc:
                with state.lock:
                    state.runs[plan_id] = {"alive": False,
                                           "error": exc.to_dict()}
            except Exception as exc:  # keep the worker observable
                with state.lock:
                    state.runs[plan_id] = {
                        "alive": False,
                        "error": {"code": "worker_error",
                                  "message": f"{type(exc).__name__}: {exc}"}}
            else:
                with state.lock:
                    state.runs[plan_id] = {"alive": False}

        thread = threading.Thread(target=worker, daemon=True,
                                  name=f"workflow-run-{plan_id}")
        with state.lock:
            state.runs[plan_id] = {"alive": True, "thread": thread}
        thread.start()
        self._send_json({"ok": True, "started": plan_id})

    # ----------------------------------------------------------- chat SSE
    def _chat_stream(self, body: dict) -> None:
        state = self.state
        app = state.app
        message = str(body.get("message") or "").strip()
        if not message:
            self._send_json({"ok": False, "error": {
                "code": "empty_message", "message": "message is required"}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        guards = {"sentinel": False}

        def frame(payload: dict) -> None:
            if guards["sentinel"]:
                return
            self._sse_frame(payload)

        try:
            from ..agents.runtime import AgentRuntime
            from ..cli.agent import _resolve_mode
            entry, provider, mode = _resolve_mode(app, body.get("provider"))
            frame({"type": "meta", "provider": entry.id, "mode": mode})
            runtime = AgentRuntime(
                provider=provider,
                registry=app.registry(project_root=body.get("project_root")),
                mode=mode, audit=app.audit,
                stream_cb=lambda fragment: frame({"type": "delta",
                                                  "text": fragment}),
                event_cb=lambda kind, payload: frame({"type": kind, **payload}))
            result = runtime.run(message)
            frame({"type": "final", "result": result})
        except VaspilotError as exc:
            frame({"type": "error", "error": exc.to_dict()})
        except Exception as exc:
            frame({"type": "error", "error": {
                "code": "ui_error",
                "message": f"{type(exc).__name__}: {exc}"}})
        finally:
            guards["sentinel"] = True
            try:
                self.wfile.flush()
            except (OSError, ValueError):
                pass


def serve(app, *, host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = True, run_forever: bool = True) -> tuple:
    """Start the UI HTTP server; returns (httpd, url)."""
    state = build_state(app)

    class BoundHandler(UiHandler):
        pass

    BoundHandler.state = state
    httpd = ThreadingHTTPServer((host, port), BoundHandler)
    httpd.daemon_threads = True
    # port=0 lets the OS pick a free port; report the ACTUAL bound port
    bound_port = httpd.server_address[1]
    url = f"http://{host}:{bound_port}/t/{state.token}"
    print(f"VASPilot UI ready: {url}", flush=True)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    if run_forever:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
        return httpd, url
    return httpd, url


def main(argv: list[str] | None = None) -> int:
    import argparse
    from ..cli.main import App
    from ..core.config import Config

    parser = argparse.ArgumentParser(
        prog="vaspilot ui", description="VASPilot local web console")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8930)
    parser.add_argument("--no-open", dest="open_browser", action="store_false",
                        help="do not open the browser automatically")
    args = parser.parse_args(argv)
    app = App(Config())
    print(f"VASPilot UI {__version__} listening on "
          f"http://{args.host}:{args.port} (Ctrl-C to stop)")
    serve(app, host=args.host, port=args.port,
          open_browser=args.open_browser)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
