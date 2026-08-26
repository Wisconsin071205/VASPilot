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

_EXPIRED_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>会话已过期 — VASPilot</title><style>
body{background:#0d1117;color:#e6edf3;font:15px/1.7 "Segoe UI","Microsoft YaHei",sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.card{max-width:520px;background:#161b22;border:1px solid #2d3644;border-radius:12px;padding:28px 32px}
h1{font-size:18px;margin:0 0 10px}p{color:#9aa7b8;margin:8px 0}
code{background:#1c2330;border-radius:5px;padding:2px 7px;font-size:13px;color:#4f9cf7}
</style></head><body><div class="card">
<h1>⏱ 会话已过期</h1>
<p>每次启动 <code>vaspilot ui</code> 都会生成新的会话令牌，旧标签页的链接随之失效。</p>
<p>请重新打开：<b>桌面「VASPilot 控制台」</b>快捷方式，或在终端运行
<code>vaspilot ui</code> —— 浏览器会自动打开新会话。</p>
<p style="font-size:12px">若提示端口被占用，服务会自动改用相邻端口并打印实际地址。</p>
</div></body></html>"""

_LANDING_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>VASPilot 控制台</title><style>
body{background:#0d1117;color:#e6edf3;font:15px/1.7 "Segoe UI","Microsoft YaHei",sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.card{max-width:520px;background:#161b22;border:1px solid #2d3644;border-radius:12px;padding:28px 32px}
h1{font-size:18px;margin:0 0 10px}p{color:#9aa7b8;margin:8px 0}
code{background:#1c2330;border-radius:5px;padding:2px 7px;font-size:13px;color:#4f9cf7}
</style></head><body><div class="card">
<h1>VASPilot 控制台</h1>
<p>控制台通过带会话令牌的地址访问，请从快捷方式或命令启动（浏览器会自动打开）：</p>
<p><code>vaspilot ui</code></p>
<p style="font-size:12px">这是设计如此：令牌防止本机其他页面盗用控制台 API。</p>
</div></body></html>"""


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
                self._send(403, _EXPIRED_PAGE.encode("utf-8"),
                           "text/html; charset=utf-8")
                return
            page = (STATIC_DIR / "index.html").read_bytes()
            self._send(200, page, "text/html; charset=utf-8")
            return
        if path == "/" or path == "/index.html":
            self._send(200, _LANDING_PAGE.encode("utf-8"),
                       "text/html; charset=utf-8")
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
            elif action == "server.add":
                self._add_server(body)
            elif action == "server.edit":
                self._edit_server(body)
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
            elif action == "settings":
                self._send_json(self._settings_payload())
            elif action == "provider.save":
                self._save_provider(body)
            elif action == "provider.delete":
                pid = str(body.get("id") or "")
                app.config.remove_provider(pid)
                app.config.remove_provider_key(pid)
                cached = app.config.load_settings().get("provider_probes") or {}
                if pid in cached:
                    del cached[pid]
                    app.config.update_settings(provider_probes=cached)
                if app.config.default_provider() == pid:
                    providers = app.config.load_providers()
                    nxt = providers[0].id if providers else ""
                    if nxt:
                        app.config.set_default_provider(nxt)
                    else:
                        app.config.update_settings(default_provider="")
                self._send_json({"deleted": pid})
            elif action == "vlab.save":
                saved = app.config.set_vlab(
                    identity_file=str(body.get("identity_file") or "").strip(),
                    host=str(body.get("host") or "").strip() or None,
                    user=str(body.get("user") or "").strip() or None)
                self._send_json({"saved": {k: v for k, v in saved.items()}})
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
                              "probed_at": (probe or {}).get("checked_at"),
                              "key_saved": app.config.provider_key_saved(p.id)})
        return {"ok": True, "version": __version__, "servers": servers,
                "providers": providers,
                "default_provider": app.config.default_provider()}

    def _add_server(self, body: dict) -> None:
        from ..core.errors import ValidationError
        client = self.state.app.client()
        name = str(body.get("name") or "").strip()
        target = str(body.get("target") or "").strip()
        if not target or "@" not in target:
            raise ValidationError("target must be user@host")
        result = client.server_add(
            name=name, target=target,
            port=int(body.get("port") or 22),
            remote_root=str(body.get("remote_root") or "").strip(),
            persist=str(body.get("persist") or "8h").strip() or "8h",
            scheduler=str(body.get("scheduler") or "auto"),
            set_default=bool(body.get("make_default")))
        self._send_json({"added": name, "result": result})

    def _edit_server(self, body: dict) -> None:
        from ..core.errors import ValidationError
        app = self.state.app
        client = app.client()
        old = str(body.get("id") or "").strip()
        new_name = str(body.get("new_name") or "").strip()
        entry = client.server_entry(old)
        try:
            connected = bool(client.status(old).get("connected"))
        except Exception:
            connected = False
        fields = dict(
            target=str(body.get("target") or "").strip() or entry.target,
            port=int(body.get("port") or 0) or entry.port,
            remote_root=(str(body.get("remote_root") or "").strip()
                         if body.get("remote_root") is not None
                         else entry.remote_root),
            persist=str(body.get("persist") or "").strip() or entry.persist,
            scheduler=str(body.get("scheduler") or "").strip() or entry.scheduler)

        identity_changed = (new_name and new_name != old) or \
            fields["target"] != entry.target or fields["port"] != entry.port
        if connected and identity_changed:
            raise ValidationError(
                f"{old} 已连接：改名或修改地址/端口前请先断开该服务器；"
                "根路径与保持时长可随时修改")

        if not identity_changed:
            edited = client.server_edit(
                old, target=None, port=None,
                remote_root=fields["remote_root"],
                persist=fields["persist"], scheduler=fields["scheduler"])
            self._send_json({"edited": old, "result": edited})
            return

        if not new_name or new_name == old:
            # target/port change while disconnected: apply all fields
            edited = client.server_edit(
                old, target=fields["target"], port=fields["port"],
                remote_root=fields["remote_root"],
                persist=fields["persist"], scheduler=fields["scheduler"])
            self._send_json({"edited": old, "result": edited})
            return

        # rename while disconnected: add the new identity, retire the old
        keep_default = app.config.default_server() == old
        result = client.server_add(
            name=new_name, target=fields["target"], port=fields["port"],
            remote_root=fields["remote_root"], persist=fields["persist"],
            scheduler=fields["scheduler"])
        client.server_remove(old)
        if keep_default:
            client.server_set_default(new_name)
        self._send_json({"renamed": [old, new_name], "result": result})

    def _settings_payload(self) -> dict:
        """Full settings view for the console's 配置 page. Never includes key
        material — only whether a DPAPI-protected key exists."""
        app = self.state.app
        providers = []
        for p in app.config.load_providers():
            providers.append({**p.to_dict(),
                              "is_default": p.id == app.config.default_provider(),
                              "key_saved": app.config.provider_key_saved(p.id)})
        vlab = app.config.vlab
        identity_ok = False
        if vlab["identity_file"]:
            try:
                from pathlib import Path
                identity_ok = Path(vlab["identity_file"]).expanduser().is_file()
            except OSError:
                identity_ok = False
        return {"ok": True,
                "providers": providers,
                "default_provider": app.config.default_provider(),
                "vlab": {**vlab, "identity_file_exists": identity_ok}}

    def _save_provider(self, body: dict) -> None:
        from ..core.config import ProviderEntry
        from ..core.errors import ValidationError
        from ..providers import build_provider

        app = self.state.app
        pid = str(body.get("id") or "").strip()
        name = str(body.get("name") or "").strip() or pid
        protocol = str(body.get("protocol") or "").strip()
        base_url = str(body.get("base_url") or "").strip()
        model = str(body.get("model") or "").strip()
        api_key_env = str(body.get("api_key_env") or "").strip()
        api_key = str(body.get("api_key") or "")  # optional; empty keeps vault
        make_default = bool(body.get("make_default"))

        existing_ids = [p.id for p in app.config.load_providers()]
        if not pid:
            # auto id for new cards
            import re as _re
            base = _re.sub(r"[^A-Za-z0-9._-]", "-", name or "provider").strip("-")
            pid = (base[:40] or "provider") + "-" + secrets.token_hex(3)
        is_new = pid not in existing_ids
        if is_new and not base_url and protocol != "codex-sdk":
            raise ValidationError("new HTTP provider needs an API base URL")

        app.config.add_provider(ProviderEntry(
            id=pid, name=name[:40], protocol=protocol,
            base_url=base_url, model=model[:64], api_key_env=api_key_env))
        if api_key.strip():
            app.config.set_provider_key(pid, api_key)
        if make_default or not app.config.default_provider():
            app.config.set_default_provider(pid)
        # sanity-check through the same resolution path the agent uses; a
        # missing key must NOT block saving the card — it only flips a flag
        auth_ready = True
        try:
            from ..providers import build_provider as _build
            entry = next(p for p in app.config.load_providers() if p.id == pid)
            _build(entry, app.config)
        except Exception:
            auth_ready = False
        self._send_json({"saved": pid,
                         "default": app.config.default_provider() == pid,
                         "key_saved": app.config.provider_key_saved(pid),
                         "auth_ready": auth_ready})

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


def _bind_with_fallback(handler_class, host: str, port: int, attempts: int = 10):
    """Bind the requested port; on conflict try the next ports (8930..8939).

    Windows may also refuse ports inside Hyper-V exclusion ranges; falling
    forward keeps the desktop shortcut working without manual edits.
    """
    import socket
    last_error: OSError | None = None
    for candidate in range(port, port + attempts):
        try:
            return ThreadingHTTPServer((host, candidate), handler_class)
        except OSError as exc:
            last_error = exc
    raise last_error or OSError("could not bind any port")


def serve(app, *, host: str = "127.0.0.1", port: int = 8930,
          open_browser: bool = True, run_forever: bool = True) -> tuple:
    """Start the UI HTTP server; returns (httpd, url)."""
    state = build_state(app)

    class BoundHandler(UiHandler):
        pass

    BoundHandler.state = state
    httpd = _bind_with_fallback(BoundHandler, host, port)
    httpd.daemon_threads = True
    bound_port = httpd.server_address[1]
    if port and bound_port != port:
        print(f"port {port} unavailable; using {bound_port} instead", flush=True)
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
