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
            elif action == "agent.submit_mode":
                mode = app.config.set_agent_submit_mode(
                    str(body.get("mode") or "confirm"))
                self._send_json({"agent_submit_mode": mode})
            elif action == "websearch.save":
                api_key = str(body.get("api_key") or "")
                if api_key.strip():
                    app.config.set_provider_key("websearch", api_key)
                if body.get("remove_key"):
                    app.config.remove_provider_key("websearch")
                saved = app.config.set_websearch(
                    provider=str(body.get("provider") or "zhipu"),
                    enabled=bool(body.get("enabled")))
                self._send_json({"websearch": saved})
            elif action == "server.metrics":
                self._server_metrics(str(body.get("server") or ""))
            elif action.startswith("project."):
                self._project_action(action, body)
            elif action.startswith("chat."):
                self._chat_action(action, body)
            elif action.startswith("skill."):
                self._skill_action(action, body)
            elif action == "submit.pending":
                from ..workflow.pending import PendingSubmitStore
                store = PendingSubmitStore(app.config.pending_submits_path)
                self._send_json({"pending": store.pending(
                    str(body.get("session_id") or ""))})
            elif action == "submit.confirm":
                self._confirm_submit(body)
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
            try:
                connected = bool(app.client().status(entry.name).get("connected"))
            except Exception:
                connected = False
            servers.append({**entry.to_dict(),
                            "connected": connected,
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
                "default_provider": app.config.default_provider(),
                "agent_submit_mode": app.config.agent_submit_mode(),
                "websearch": app.config.websearch()}

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
                "vlab": {**vlab, "identity_file_exists": identity_ok},
                "agent_submit_mode": app.config.agent_submit_mode(),
                "websearch": app.config.websearch()}

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

    # ---------------------------------------------------- projects / chat / skills
    def _project_action(self, action: str, body: dict) -> None:
        from ..workflow.projects import TEMPLATES, ProjectStore
        app = self.state.app
        store = ProjectStore(projects_dir=app.config.projects_dir,
                             index_path=app.config.projects_index_path,
                             audit=app.audit)
        name = str(body.get("name") or "").strip()
        if action == "project.templates":
            self._send_json({"templates": [
                {"name": key, "description": value["description"],
                 "files": {k: v for k, v in value.items()
                           if k != "description"}}
                for key, value in TEMPLATES.items()]})
        elif action == "project.create":
            files = body.get("files") or {}
            self._send_json(store.create(
                name, {str(k): str(v) for k, v in files.items()
                       if isinstance(v, str)},
                potcar_path=str(body.get("potcar_path") or "").strip()
                or None,
                potcar_remote=str(body.get("potcar_remote") or "")))
        elif action == "project.potcar_remote":
            self._send_json(store.set_potcar_remote(
                name, str(body.get("path") or "")))
        elif action == "project.list":
            self._send_json({"projects": store.list()})
        elif action == "project.read":
            self._send_json(store.read_file(
                name, str(body.get("file") or "")))
        elif action == "project.write":
            self._send_json(store.write_file(
                name, str(body.get("file") or ""),
                str(body.get("content") or "")))
        elif action == "project.potcar":
            self._send_json(store.copy_potcar(
                name, str(body.get("potcar_path") or "")))
        elif action == "project.validate":
            self._send_json(store.validate(name))
        elif action == "project.pin":
            self._send_json(store.pin(name, bool(body.get("pinned", True))))
        elif action == "project.delete":
            self._send_json(store.delete(name))
        else:
            self._send_json({"ok": False, "error": {
                "code": "unknown_action", "message": action}}, status=404)

    def _chat_action(self, action: str, body: dict) -> None:
        from ..agents.memory import ConversationStore
        from ..workflow.pending import PendingSubmitStore
        app = self.state.app
        memory = ConversationStore(app.config.chat_dir)
        pending = PendingSubmitStore(app.config.pending_submits_path)
        session_id = str(body.get("session_id") or "").strip()
        if action == "chat.sessions":
            sessions = memory.list_sessions()
            pending_rows = pending.pending()
            for row in sessions:
                row["pending"] = sum(
                    1 for e in pending_rows
                    if e.get("session_id") == row["session_id"])
            self._send_json({"sessions": sessions,
                             "pending": pending_rows})
        elif action == "chat.new":
            self._send_json(memory.create_session(
                project=str(body.get("project") or ""),
                title=str(body.get("title") or "")))
        elif action == "chat.load":
            payload = memory.load(session_id)
            if payload is None:
                from ..core.errors import ValidationError
                raise ValidationError(f"session {session_id!r} not found")
            self._send_json(payload)
        elif action == "chat.rename":
            self._send_json(memory.rename(
                session_id, str(body.get("title") or "")))
        elif action == "chat.setproject":
            self._send_json(memory.set_project(
                session_id, str(body.get("project") or "")))
        elif action == "chat.clear":
            self._send_json(memory.clear(session_id))
        else:
            self._send_json({"ok": False, "error": {
                "code": "unknown_action", "message": action}}, status=404)

    def _skill_action(self, action: str, body: dict) -> None:
        from ..agents.skills import SkillStore
        app = self.state.app
        store = SkillStore(app.config.skills_dir, audit=app.audit)
        name = str(body.get("name") or "").strip()
        if action == "skill.list":
            self._send_json({"skills": store.list()})
        elif action == "skill.read":
            self._send_json(store.read(name))
        elif action == "skill.save":
            self._send_json(store.write(
                name, str(body.get("description") or ""),
                str(body.get("body") or "")))
        elif action == "skill.delete":
            self._send_json(store.delete(name))
        else:
            self._send_json({"ok": False, "error": {
                "code": "unknown_action", "message": action}}, status=404)

    # ---------------------------------------------------- submit confirmation
    def _confirm_submit(self, body: dict) -> None:
        from ..core.errors import ValidationError
        from ..core.hashing import text_sha256
        from ..agents.memory import ConversationStore
        from ..workflow.pending import PendingSubmitStore
        app = self.state.app
        client = app.client()
        store = PendingSubmitStore(app.config.pending_submits_path)
        entry_id = str(body.get("id") or "").strip()
        entry = store.get(entry_id)  # raises on unknown/expired
        approve = bool(body.get("approve"))
        if not approve:
            settled = store.settle(entry_id, "rejected")
            self._note_session(app, entry, "[提交确认已拒绝] "
                               f"{entry['server']}:{entry['directory']}")
            self._send_json({"ok": True, "settled": "rejected",
                             "entry": self._pending_view(settled)})
            return
        # approved: submit exactly the frozen parameters; abort if the remote
        # script changed since the card was created
        if entry.get("script_sha256"):
            remote = f"{entry['directory'].rstrip('/')}/{entry['script']}"
            document = client.read(remote, server=entry["server"])
            if text_sha256(str(document.get("content", ""))) != \
                    entry["script_sha256"]:
                raise ValidationError(
                    "远端作业脚本在确认卡片生成之后被修改，已中止提交；"
                    "请让智能体重新发起 job_submit")
        result = client.submit(str(entry["directory"]), str(entry["script"]),
                               server=entry["server"] or None)
        settled = store.settle(entry_id, "approved", result)
        self._note_session(app, entry, "[提交确认已批准并执行] "
                           f"{entry['server']}:{entry['directory']}/"
                           f"{entry['script']} -> "
                           f"{json.dumps(result, ensure_ascii=False)[:300]}")
        self._send_json({"ok": True, "settled": "approved",
                         "result": result,
                         "entry": self._pending_view(settled)})

    @staticmethod
    def _pending_view(entry: dict) -> dict:
        # the card view never needs the full script content again
        return {k: v for k, v in entry.items() if k != "script_content"}

    @staticmethod
    def _note_session(app, entry: dict, note: str) -> None:
        session_id = str(entry.get("session_id") or "")
        if not session_id:
            return
        try:
            from ..agents.memory import ConversationStore
            memory = ConversationStore(app.config.chat_dir)
            memory.append(session_id, "user", note)
        except Exception:
            pass  # the note is best-effort context, never a hard failure

    # ---------------------------------------------------- server metrics
    def _server_metrics(self, server: str) -> None:
        import time as _time
        from ..core.validation import valid_server_name
        app = self.state.app
        client = app.client()
        name = valid_server_name(server or app.config.default_server())
        document = client.metrics(name)
        # rolling history for the trend strip (7 days, max 1000 rows)
        history_dir = app.config.metrics_dir
        history_dir.mkdir(parents=True, exist_ok=True)
        history_path = history_dir / f"{name}.jsonl"
        row = {"at": document.get("collected_at") or
               _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
               "cpu_pct": (document.get("cpu") or {}).get("usage_pct"),
               "mem_pct": (document.get("mem") or {}).get("used_pct"),
               "load_one": (document.get("load") or {}).get("one"),
               "gpu_pct": max([g.get("util_pct") or 0
                               for g in document.get("gpus") or ()] or [0])}
        try:
            with open(history_path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass
        history = []
        try:
            lines = history_path.read_text(
                encoding="utf-8", errors="replace").splitlines()
            if len(lines) > 1000:
                with open(history_path, "w", encoding="utf-8",
                          newline="\n") as f:
                    f.write("\n".join(lines[-1000:]) + "\n")
                lines = lines[-1000:]
            for line in lines[-60:]:
                try:
                    history.append(json.loads(line))
                except ValueError:
                    continue
        except OSError:
            pass
        self._send_json({**document, "history": history})

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
        session_id = str(body.get("session_id") or "").strip()
        history: list[dict[str, Any]] = []
        project_dir: Path | None = None
        project_name = ""
        memory = None
        if session_id:
            from ..agents.memory import ConversationStore
            memory = ConversationStore(app.config.chat_dir)
            session = memory.load(session_id)
            if session is not None:
                history = session.get("messages") or []
                project_name = str(session.get("project") or "")
                if project_name:
                    candidate = (app.config.projects_dir /
                                 project_name).resolve()
                    if candidate.is_dir():
                        project_dir = candidate
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
            from ..agents.skills import SkillStore
            from ..cli.agent import _resolve_mode
            entry, provider, mode = _resolve_mode(app, body.get("provider"))
            frame({"type": "meta", "provider": entry.id, "mode": mode,
                   "session_id": session_id, "project": project_name})
            system_extra_parts = []
            skills_index = SkillStore(
                app.config.skills_dir, audit=app.audit).index_prompt()
            if skills_index:
                system_extra_parts.append(skills_index)
            if project_name:
                from ..workflow.projects import ProjectStore
                meta = next((p for p in ProjectStore(
                    projects_dir=app.config.projects_dir,
                    index_path=app.config.projects_index_path).list()
                    if p["name"] == project_name), {})
                extra = (f"Active local project: {project_name} "
                         f"(directory {project_dir}). project_read/"
                         f"project_write with an empty project name target "
                         f"it implicitly; upload_file paths resolve inside "
                         f"it.")
                if meta.get("potcar_remote"):
                    extra += (
                        f"\nRemote POTCAR source recorded for this project: "
                        f"{meta['potcar_remote']}. When the run directory is "
                        f"ready but POTCAR is absent, propose ONE audited "
                        f"remote_run command that copies/concatenates it "
                        f"into place before job_submit (never print its "
                        f"content); ask which element-set to use if ambiguous.")
                system_extra_parts.append(extra)
            submit_mode = app.config.agent_submit_mode()
            system_extra_parts.append(
                f"agent_submit_mode={submit_mode}: " + (
                    "job_submit pauses for human confirmation — tell the user "
                    "to click the approval card, then verify next turn."
                    if submit_mode == "confirm" else
                    "job_submit submits directly (audit-only)."))
            runtime = AgentRuntime(
                provider=provider,
                registry=app.build_registry(
                    project_root=project_dir or body.get("project_root"),
                    session_id=session_id),
                mode=mode, audit=app.audit,
                system_extra="\n\n".join(system_extra_parts),
                stream_cb=lambda fragment: frame({"type": "delta",
                                                  "text": fragment}),
                event_cb=lambda kind, payload: frame({"type": kind, **payload}))
            result = runtime.run(message, history=history)
            frame({"type": "final", "result": result})
            if memory is not None and session_id and result.get("ok"):
                try:
                    memory.append(session_id, "user", message)
                    memory.append(session_id, "assistant",
                                  str(result.get("answer", "")))
                except Exception:
                    pass
            if session_id:
                try:
                    from ..workflow.pending import PendingSubmitStore
                    pending = PendingSubmitStore(
                        app.config.pending_submits_path).pending(session_id)
                    if pending:
                        frame({"type": "pending",
                               "pending": [self._pending_view(e)
                                           for e in pending]})
                except Exception:
                    pass
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
