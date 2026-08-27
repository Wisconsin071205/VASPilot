"""Local web UI tests over the fake transport: token guard, views,
workflow approval loop and the SSE chat stream."""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from tests.conftest import ROOT


@pytest.fixture()
def ui(config_home, fake_state, monkeypatch):
    """A running UI server wired to the fake gateway transport."""
    from vaspilot.ui import server as ui_server
    from vaspilot.cli.main import App
    from vaspilot.core.config import Config, ServerEntry, ProviderEntry
    from tests.conftest import FakeTransport

    config = Config(config_home)
    config.upsert_server(ServerEntry(
        name="cl9", target="user@cl9", port=22, remote_root=ROOT,
        persist="8h", scheduler="slurm"))
    config.set_default_server("cl9")
    config.add_provider(ProviderEntry(
        id="mock", name="Mock", protocol="openai-chat-compatible",
        base_url="http://127.0.0.1:1/v1", model="mock-model"))
    config.set_default_provider("mock")

    app = App(config)
    transport = FakeTransport(fake_state)
    monkeypatch.setattr(App, "transport", lambda self: transport)
    # bind a scripted provider so agent chat works without network; patch the
    # name as imported INSIDE vaspilot.cli.agent (module-level import)
    scripted = ScriptedProvider([
        {"tool_calls": [type("C", (), {"call_id": "1", "name": "remote_pwd",
                                       "arguments": {}})()]},
        {"text": "远端根目录已确认。"},
    ])
    monkeypatch.setattr(
        "vaspilot.cli.agent.provider_by_id",
        lambda config, pid: (config.load_providers()[0], scripted))

    httpd, url = ui_server.serve(app, host="127.0.0.1", port=0,
                                 open_browser=False, run_forever=False)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    token = url.rsplit("/", 1)[1]
    base = url.rsplit("/t/", 1)[0]
    yield {"base": base, "token": token, "app": app, "state": fake_state,
           "scripted": scripted}
    httpd.shutdown()
    httpd.server_close()


class ScriptedProvider:
    protocol = "openai-chat-compatible"

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[list] = []
        self.entry = type("E", (), {"id": "mock", "name": "Mock",
                                    "protocol": "openai-chat-compatible",
                                    "base_url": "", "model": "m",
                                    "api_key_env": ""})()

    def probe(self):
        from vaspilot.providers.base import CapabilityProbe
        from datetime import datetime, timezone
        return CapabilityProbe(reachable=True, streaming=True,
                               tool_calling=True, structured_json=True,
                               provider_id="mock",
                               checked_at=datetime.now(timezone.utc)
                               .isoformat(timespec="seconds"))

    def chat(self, messages, tools, *, stream_cb=None):
        from vaspilot.providers.base import ProviderReply
        self.calls.append([dict(m) for m in messages])
        action = self.script.pop(0)
        return ProviderReply(**action)


def call(ui, action, body=None, token=None, method=None):
    if method == "GET":
        from urllib.parse import urlencode
        url = f"{ui['base']}/api/{action}"
        if body:
            url += "?" + urlencode(body)
        data = None
    else:
        url = f"{ui['base']}/api/{action}"
        data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, method=method or "POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Vaspilot-Token", token if token is not None else ui["token"])
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


class TestTokenGuard:
    def test_api_without_token_rejected(self, ui):
        with pytest.raises(urllib.error.HTTPError) as exc:
            call(ui, "state", token="wrong-token")
        assert exc.value.code == 403

    def test_page_requires_exact_token(self, ui):
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{ui['base']}/t/wrong", timeout=10)
        assert exc.value.code == 403

    def test_expired_token_shows_friendly_page(self, ui):
        """The old plain-text 'session expired' is now a helpful page."""
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{ui['base']}/t/old-token", timeout=10)
        body = exc.value.read().decode("utf-8")
        assert exc.value.code == 403
        assert "会话已过期" in body and "vaspilot ui" in body

    def test_landing_page_without_token(self, ui):
        with urllib.request.urlopen(f"{ui['base']}/", timeout=10) as response:
            body = response.read().decode("utf-8")
        assert "VASPilot" in body and "vaspilot ui" in body

    def test_page_served_with_valid_token(self, ui):
        with urllib.request.urlopen(f"{ui['base']}/t/{ui['token']}",
                                    timeout=10) as response:
            html = response.read().decode("utf-8")
        assert "VASPilot" in html and "chatinput" in html


class TestPortFallback:
    def test_busy_port_falls_forward(self, ui, monkeypatch):
        """When the requested port refuses to bind, the next port is used.

        Windows SO_REUSEADDR would let a second bind succeed silently, so
        the failure is injected at the constructor instead.
        """
        from vaspilot.ui import server as ui_server
        base_port = int(ui["base"].rsplit(":", 1)[1])
        real_server = ui_server.ThreadingHTTPServer

        class FlakyServer:
            def __init__(self, addr, handler):
                if addr[1] == base_port:
                    raise OSError(10013, "injected bind refusal")
                self._real = real_server(addr, handler)
                self.server_address = self._real.server_address

            def __getattr__(self, name):
                return getattr(self._real, name)

        monkeypatch.setattr(ui_server, "ThreadingHTTPServer", FlakyServer)
        httpd, url = ui_server.serve(ui["app"], host="127.0.0.1",
                                     port=base_port, open_browser=False,
                                     run_forever=False)
        try:
            used = int(url.rsplit(":", 1)[1].split("/")[0])
            assert used == base_port + 1
        finally:
            httpd.server_close()


class TestViews:
    def test_state_lists_servers_and_providers(self, ui):
        payload = call(ui, "state")
        assert payload["ok"]
        names = [s["name"] for s in payload["servers"]]
        assert "cl9" in names
        assert payload["default_provider"] == "mock"
        # provider metadata only — no key material in the payload
        assert "api_key" not in json.dumps(payload["providers"]).replace(
            "api_key_env", "")

    def test_remote_list_and_read(self, ui):
        listing = call(ui, "remote.list", {"server": "cl9",
                                           "path": f"{ROOT}/runs"})
        assert listing["ok"]
        names = [e["name"] for e in listing["entries"]]
        assert "good" in names and "bad" in names
        doc = call(ui, "remote.read", {"server": "cl9",
                                       "path": f"{ROOT}/runs/good/INCAR"})
        assert "SYSTEM=good" in doc["content"]

    def test_outside_root_rejected_as_json(self, ui):
        payload = call(ui, "remote.list", {"server": "cl9", "path": "/etc"})
        assert payload["ok"] is False
        assert payload["error"]["code"] == "validation_error"

    def test_jobs_and_progress(self, ui):
        jobs = call(ui, "job.list", {"server": "cl9"})
        assert jobs["ok"]
        progress = call(ui, "vasp.progress",
                        {"server": "cl9", "directory": f"{ROOT}/runs/good"})
        assert progress["ok"]
        assert progress["scientific_converged"] is True


class TestWorkflowLoop:
    def _prepare(self, ui, vasp_inputs):
        spec = {"from_dir": str(vasp_inputs), "server": "cl9",
                "remote_dir": f"{ROOT}/runs/ui-case", "ntasks": 4,
                "walltime": "02:00:00"}
        prepared = call(ui, "workflow.prepare", {"spec": spec})
        assert prepared["ok"], prepared
        return prepared

    def test_wrong_phrase_rejected(self, ui, vasp_inputs):
        prepared = self._prepare(ui, vasp_inputs)
        result = call(ui, "workflow.approve",
                      {"plan_id": prepared["plan_id"], "phrase": "approve nope"})
        assert result["ok"] is False
        assert "phrase" in result["error"]["message"]

    def test_full_prepare_approve_run(self, ui, vasp_inputs):
        prepared = self._prepare(ui, vasp_inputs)
        # human types the exact phrase in the page
        approved = call(ui, "workflow.approve", {
            "plan_id": prepared["plan_id"],
            "phrase": f"approve {prepared['plan_id']}"})
        assert approved["ok"]
        assert approved["approval_ref"]
        # seed the scientific outcome the finished job leaves behind
        remote = prepared["plan"]["remote_dir"]
        state = ui["state"]
        state.files["cl9"][f"{remote}/OSZICAR"] = (
            b"   1 F= -.10000000E+02  E0= -.10000000E+02\n")
        state.files["cl9"][f"{remote}/OUTCAR"] = b"reached required accuracy\n"
        state.files["cl9"][f"{remote}/CONTCAR"] = b"final\n"
        started = call(ui, "workflow.run", {"plan_id": prepared["plan_id"],
                                            "approval_ref": approved["approval_ref"],
                                            "poll_seconds": 0})
        assert started["ok"]
        # background thread executes; poll until terminal
        for _ in range(100):
            status = call(ui, "workflow/status",
                          {"plan_id": prepared["plan_id"]}, method="GET")
            if status.get("status") not in ("running", None):
                break
            time.sleep(0.05)
        assert status["status"] == "completed"
        assert status["scientific_converged"] is True

    def test_duplicate_run_rejected(self, ui, vasp_inputs):
        prepared = self._prepare(ui, vasp_inputs)
        remote = prepared["plan"]["remote_dir"]
        ui["state"].files["cl9"][f"{remote}/KPOINTS"] = b"collision\n"
        approved = call(ui, "workflow.approve", {
            "plan_id": prepared["plan_id"],
            "phrase": f"approve {prepared['plan_id']}"})
        first = call(ui, "workflow.run", {"plan_id": prepared["plan_id"],
                                          "approval_ref": approved["approval_ref"],
                                          "poll_seconds": 0})
        assert first["ok"]
        for _ in range(100):
            status = call(ui, "workflow/status",
                          {"plan_id": prepared["plan_id"]}, method="GET")
            if status.get("status") not in ("running", None):
                break
            time.sleep(0.05)
        assert status["status"] == "failed"  # upload collision
        # replaying the consumed approval for a fresh run is refused: the
        # worker rejects it before creating any state, so the deleted run
        # file is never recreated and the rejection shows via status
        ui["app"].engine().run_path(prepared["plan_id"]).unlink()
        replay = call(ui, "workflow.run", {"plan_id": prepared["plan_id"],
                                           "approval_ref": approved["approval_ref"],
                                           "poll_seconds": 0})
        assert replay["ok"]  # accepted for async execution...
        for _ in range(100):
            after = call(ui, "workflow/status",
                         {"plan_id": prepared["plan_id"]}, method="GET")
            if not after.get("ok"):
                break
            time.sleep(0.05)
        assert not after.get("ok")  # ...but no run state was ever created
        assert "no run state" in after["error"]["message"]


class TestChatStream:
    def test_sse_frames(self, ui):
        req = urllib.request.Request(
            f"{ui['base']}/api/agent/chat",
            data=json.dumps({"provider": "mock",
                             "message": "查看远端根目录"}).encode(),
            method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Vaspilot-Token", ui["token"])
        with urllib.request.urlopen(req, timeout=60) as response:
            body = response.read().decode("utf-8")
        frames = [json.loads(line[5:])
                  for block in body.split("\n\n") if block.strip()
                  for line in block.split("\n") if line.startswith("data:")]
        kinds = [f["type"] for f in frames]
        assert "meta" in kinds
        assert "tool" in kinds
        assert "final" in kinds
        tool = next(f for f in frames if f["type"] == "tool")
        assert tool["tool"] == "remote_pwd" and tool["ok"] is True
        final = next(f for f in frames if f.get("type") == "final"
                     and "result" in f)
        assert "远端根目录" in final["result"]["answer"]

    def test_empty_message_rejected(self, ui):
        payload = call(ui, "agent/chat", {"provider": "mock", "message": "  "})
        assert payload["ok"] is False

    def test_unknown_action(self, ui):
        with pytest.raises(urllib.error.HTTPError) as exc:
            call(ui, "no.such.action", {})
        assert exc.value.code == 404


import urllib.error  # noqa: E402  (used in exceptions above)


class TestSettingsApi:
    """The 配置 page: providers CRUD + DPAPI key vault + vlab settings."""

    def test_save_provider_with_key_roundtrip(self, ui, monkeypatch):
        monkeypatch.delenv("VASPILOT_API_KEY_WEB_X", raising=False)
        payload = {"id": "web1", "name": "Web GLM",
                   "protocol": "openai-chat-compatible",
                   "base_url": "https://api.example.com/v1",
                   "model": "glm-web", "api_key_env": "",
                   "api_key": "sk-secret-123"}
        saved = call(ui, "provider.save", payload)
        assert saved["ok"] is True
        assert saved["key_saved"] is True
        assert saved["auth_ready"] is True

        # the plaintext never lands on disk or in any API response
        raw = (ui["app"].config.settings_path).read_text(encoding="utf-8")
        assert "sk-secret-123" not in raw
        blob = json.dumps(saved) + json.dumps(
            call(ui, "settings"))
        assert "sk-secret-123" not in blob

        # a FRESH resolution (no env var) finds the key via the vault —
        # this is the exact path chat/probe uses
        from vaspilot.providers import provider_by_id
        entry, provider = provider_by_id(ui["app"].config, "web1")
        assert provider.api_key == "sk-secret-123"

        # second save WITHOUT key keeps the vault entry
        updated = call(ui, "provider.save", {**payload, "api_key": "",
                                             "model": "glm-web-2"})
        assert updated["key_saved"] is True
        _, provider2 = provider_by_id(ui["app"].config, "web1")
        assert provider2.api_key == "sk-secret-123"
        _, provider2 = provider_by_id(ui["app"].config, "web1")  # fresh again
        assert provider2.entry.model == "glm-web-2"

    def test_save_reports_missing_auth_but_still_saves(self, ui, monkeypatch):
        monkeypatch.delenv("VASPILOT_UNSET_REMOTE_Y", raising=False)
        saved = call(ui, "provider.save", {
            "id": "noserver", "name": "NoKey Cloud",
            "protocol": "openai-chat-compatible",
            "base_url": "https://cloud.example.com/v1",
            "model": "m", "api_key": ""})
        assert saved["ok"] is True
        assert saved["auth_ready"] is False
        # chat would fail with the actionable message, not 401
        from vaspilot.providers import provider_by_id
        from vaspilot.core.errors import ProviderError
        with pytest.raises(ProviderError, match="noserver"):
            provider_by_id(ui["app"].config, "noserver")

    def test_provider_delete(self, ui):
        call(ui, "provider.save", {
            "id": "tmp", "name": "T", "protocol": "openai-chat-compatible",
            "base_url": "https://x.example.com/v1", "model": "m",
            "api_key": "sk-tmp"})
        deleted = call(ui, "provider.delete", {"id": "tmp"})
        assert deleted["ok"]
        ids = [p.id for p in ui["app"].config.load_providers()]
        assert "tmp" not in ids
        assert not ui["app"].config.provider_key_saved("tmp")

    def test_vlab_save_updates_identity(self, ui, tmp_path):
        pem = tmp_path / "new.pem"
        pem.write_text("PEM\n", encoding="utf-8")
        result = call(ui, "vlab.save", {
            "identity_file": str(pem), "host": "", "user": "", "port": None})
        assert result["ok"] and result["saved"]["identity_file"] == str(pem)
        settings = call(ui, "settings")
        assert settings["vlab"]["identity_file_exists"] is True


import pytest as _pytest  # noqa: E402,F401  (pytest.raises used above)


class TestServerAdminApi:
    """添加/编辑服务器对话框背后的 API。"""

    def test_add_edit_delete_roundtrip(self, ui):
        added = call(ui, "server.add", {
            "name": "ghost5", "target": "tester@ghost5.example.com",
            "port": 22, "remote_root": "/public/home/tester",
            "persist": "8h", "scheduler": "slurm"})
        assert added["ok"]
        state = call(ui, "state")
        names = [s["name"] for s in state["servers"]]
        assert "ghost5" in names

        edited = call(ui, "server.edit", {
            "id": "ghost5", "new_name": "",
            "remote_root": "/public/home/tester/runs",
            "persist": "12h", "scheduler": "pbs"})
        assert edited["ok"]
        entry = ui["app"].client().server_entry("ghost5")
        assert entry.remote_root == "/public/home/tester/runs"
        assert entry.scheduler == "pbs"

        removed = call(ui, "provider.delete", {"id": "ghost5"})  # wrong action guard
        del removed
        # rename requires a disconnected server in the fake too
        call(ui, "server.disconnect", {"server": "ghost5"})
        renamed = call(ui, "server.edit", {
            "id": "ghost5", "new_name": "ghost6",
            "target": "tester@ghost5.example.com", "port": 22,
            "remote_root": "/public/home/tester/runs", "persist": "8h",
            "scheduler": "pbs"})
        assert renamed["ok"]
        names = [s.name for s in ui["app"].config.load_servers()]
        assert "ghost6" in names and "ghost5" not in names

    def test_rename_connected_refused(self, ui, fake_state, monkeypatch):
        fake_state.connected["cl9"] = True  # already true by default fixture
        r = call(ui, "server.edit", {
            "id": "cl9", "new_name": "renamed9",
            "target": "user@cl9", "port": 22,
            "remote_root": ROOT, "persist": "8h", "scheduler": "slurm"})
        assert r["ok"] is False
        assert "断开" in r["error"]["message"]

    def test_identity_change_needs_disconnect(self, ui):
        r = call(ui, "server.edit", {
            "id": "cl9", "new_name": "", "target": "other@host2",
            "port": 2222, "remote_root": ROOT, "persist": "8h",
            "scheduler": "slurm"})
        assert r["ok"] is False


def sse_chat(ui, body):
    req = urllib.request.Request(
        f"{ui['base']}/api/agent/chat",
        data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Vaspilot-Token", ui["token"])
    with urllib.request.urlopen(req, timeout=60) as response:
        text = response.read().decode("utf-8")
    frames = [json.loads(line[5:])
              for block in text.split("\n\n") if block.strip()
              for line in block.split("\n") if line.startswith("data:")]
    return frames


class TestChatMemory:
    def test_two_turns_share_history(self, ui):
        ui["scripted"].script.clear()
        ui["scripted"].script.extend([{"text": "第一轮：已记住 Fe。"},
                                      {"text": "第二轮：记得你说过 Fe。"}])
        created = call(ui, "chat.new", {"project": ""})
        assert created["ok"] and created["session_id"].startswith("s-")
        session_id = created["session_id"]

        frames = sse_chat(ui, {"provider": "mock", "message": "分析 Fe",
                               "session_id": session_id})
        assert any(f.get("type") == "final" for f in frames)
        second = sse_chat(ui, {"provider": "mock", "message": "继续",
                               "session_id": session_id})
        final = next(f for f in second if f.get("type") == "final"
                     and "result" in f)
        assert "第二轮" in final["result"]["answer"]

        # the provider actually received the first exchange as history
        messages = ui["scripted"].calls[1]
        roles = [m["role"] for m in messages]
        assert roles[:4] == ["system", "user", "assistant", "user"]
        assert messages[1]["content"] == "分析 Fe"
        assert messages[2]["content"] == "第一轮：已记住 Fe。"

        # persisted: chat.load returns both turns
        loaded = call(ui, "chat.load", {"session_id": session_id})
        assert len(loaded["messages"]) == 4

    def test_sessions_isolated(self, ui):
        ui["scripted"].script.clear()
        ui["scripted"].script.extend([{"text": "A 回答"}, {"text": "B 回答"}])
        first = call(ui, "chat.new", {})["session_id"]
        second = call(ui, "chat.new", {})["session_id"]
        sse_chat(ui, {"provider": "mock", "message": "会话A的问题",
                      "session_id": first})
        sse_chat(ui, {"provider": "mock", "message": "会话B的问题",
                      "session_id": second})
        messages_b = ui["scripted"].calls[1]
        assert "会话A的问题" not in json.dumps(messages_b, ensure_ascii=False)
        loaded = call(ui, "chat.load", {"session_id": second})
        assert loaded["messages"][0]["content"] == "会话B的问题"

    def test_chat_clear(self, ui):
        ui["scripted"].script.clear()
        ui["scripted"].script.extend([{"text": "回答"}])
        session_id = call(ui, "chat.new", {})["session_id"]
        sse_chat(ui, {"provider": "mock", "message": "记住我",
                      "session_id": session_id})
        cleared = call(ui, "chat.clear", {"session_id": session_id})
        assert cleared["ok"] and cleared["cleared"] is True
        missing = call(ui, "chat.load", {"session_id": session_id})
        assert missing["ok"] is False

    def test_sessions_listed_with_pending_counts(self, ui):
        listing = call(ui, "chat.sessions")
        assert listing["ok"] and "sessions" in listing


class TestProjectFlow:
    def test_project_create_then_workflow_prepare(self, ui):
        created = call(ui, "project.create", {
            "name": "ui-case", "files": {
                "INCAR": "SYSTEM = ui\nNSW = 5\nNELM = 60\n",
                "KPOINTS": "0\nGamma\n2 2 2\n0 0 0\n",
                "POSCAR": "ui\n1.0\n3 0 0\n0 3 0\n0 0 3\nNa\n1\n"
                          "direct\n0 0 0\n"}})
        assert created["ok"] is True
        assert Path(created["path"]).is_dir()

        listing = call(ui, "project.list")
        entry = next(p for p in listing["projects"]
                     if p["name"] == "ui-case")
        assert entry["complete"] is True and entry["missing"] == ["POTCAR"]

        prepared = call(ui, "workflow.prepare", {"spec": {
            "from_dir": created["path"], "server": "cl9",
            "remote_dir": f"{ROOT}/runs/ui-case",
            "skip_potcar": True}})
        assert prepared["ok"] is True
        names = [f["name"] for f in prepared["plan"]["files"]]
        assert "INCAR" in names and "run.job.sh" in names

    def test_project_run_job_sh_adopted(self, ui):
        created = call(ui, "project.create", {
            "name": "custom-script", "files": {"INCAR": "NSW=0\n",
                                               "KPOINTS": "0\n",
                                               "POSCAR": "s\n"}})
        call(ui, "project.write", {
            "name": "custom-script", "file": "run.job.sh",
            "content": "#!/bin/bash\n# custom marker\nsrun vasp_std\n"})
        prepared = call(ui, "workflow.prepare", {"spec": {
            "from_dir": created["path"], "server": "cl9",
            "remote_dir": f"{ROOT}/runs/custom", "skip_potcar": True}})
        assert prepared["ok"] is True
        assert prepared["plan"]["job_script"]["source"] == "custom"
        assert "custom marker" in prepared["plan"]["job_script_content"]
        assert any("project-authored" in n for n in
                   prepared["plan"]["risk_summary"]["notes"])

    def test_project_edit_validate_pin_delete(self, ui):
        call(ui, "project.create", {"name": "cycle",
                                    "files": {"INCAR": "NSW=0\n"}})
        written = call(ui, "project.write", {"name": "cycle",
                                             "file": "INCAR",
                                             "content": "NSW=9\n"})
        assert written["ok"] and written["size"] == 6
        doc = call(ui, "project.read", {"name": "cycle", "file": "INCAR"})
        assert doc["content"] == "NSW=9\n"
        checked = call(ui, "project.validate", {"name": "cycle"})
        assert checked["ok"] is False and checked["errors"]
        pinned = call(ui, "project.pin", {"name": "cycle", "pinned": True})
        assert pinned["ok"] and pinned["pinned"] is True
        deleted = call(ui, "project.delete", {"name": "cycle"})
        assert deleted["ok"] and "trash" in deleted["moved_to"].replace(
            "\\", "/").lower() or ".trash" in deleted["moved_to"]

    def test_project_templates_listed(self, ui):
        templates = call(ui, "project.templates")
        names = [t["name"] for t in templates["templates"]]
        assert "relax" in names and "scf" in names and "blank" in names


class TestSubmitConfirmation:
    def test_confirm_flow_executes_frozen_params(self, ui):
        app = ui["app"]
        app.config.set_agent_submit_mode("confirm")
        from vaspilot.core.hashing import text_sha256
        from vaspilot.workflow.pending import PendingSubmitStore
        directory = f"{ROOT}/runs/confirm-case"
        ui["state"].files["cl9"][f"{directory}/run.job.sh"] = b"#!/bin/bash\n"
        store = PendingSubmitStore(app.config.pending_submits_path)
        entry = store.create(server="cl9", directory=directory,
                             script="run.job.sh",
                             script_sha256=text_sha256("#!/bin/bash\n"),
                             script_content="#!/bin/bash\n", session_id="")

        listing = call(ui, "submit.pending")
        assert any(e["id"] == entry["id"] for e in listing["pending"])

        # tamper after the card was issued: sha mismatch aborts the submit
        ui["state"].files["cl9"][f"{directory}/run.job.sh"] = \
            b"#!/bin/bash\necho tampered\n"
        refused = call(ui, "submit.confirm",
                       {"id": entry["id"], "approve": True})
        assert refused["ok"] is False
        assert "修改" in refused["error"]["message"]

        # restore and approve: the submit fires exactly once
        ui["state"].files["cl9"][f"{directory}/run.job.sh"] = b"#!/bin/bash\n"
        approved = call(ui, "submit.confirm",
                        {"id": entry["id"], "approve": True})
        assert approved["ok"] is True and approved["settled"] == "approved"
        assert approved["result"]["job_id"]

        again = call(ui, "submit.confirm",
                     {"id": entry["id"], "approve": True})
        assert again["ok"] is False  # already settled

    def test_reject_flow_never_submits(self, ui):
        app = ui["app"]
        from vaspilot.workflow.pending import PendingSubmitStore
        store = PendingSubmitStore(app.config.pending_submits_path)
        entry = store.create(server="cl9", directory=f"{ROOT}/runs/rej",
                             script="run.job.sh")
        rejected = call(ui, "submit.confirm",
                        {"id": entry["id"], "approve": False})
        assert rejected["ok"] is True and rejected["settled"] == "rejected"
        assert not any(c[0] == "submit" for c in app.transport().calls)

    def test_agent_submit_mode_toggle(self, ui):
        changed = call(ui, "agent.submit_mode", {"mode": "auto"})
        assert changed["ok"] and changed["agent_submit_mode"] == "auto"
        back = call(ui, "agent.submit_mode", {"mode": "confirm"})
        assert back["agent_submit_mode"] == "confirm"
        invalid = call(ui, "agent.submit_mode", {"mode": "bypass"})
        assert invalid["ok"] is False


class TestServerMetricsApi:
    def test_metrics_with_history(self, ui):
        first = call(ui, "server.metrics", {"server": "cl9"})
        assert first["ok"] is True
        assert first["cpu"]["cores"] == 32
        assert first["gpus"][0]["name"] == "NVIDIA A100"
        assert first["queue"]["partitions"][0]["cpus_idle"] == 64
        second = call(ui, "server.metrics", {"server": "cl9"})
        assert len(second["history"]) >= 2  # trend rows accumulate
        assert (ui["app"].config.metrics_dir / "cl9.jsonl").is_file()


class TestWebsearchSettingsApi:
    def test_save_and_toggle(self, ui, monkeypatch):
        monkeypatch.delenv("VASPILOT_WEBSEARCH_KEY", raising=False)
        saved = call(ui, "websearch.save", {"provider": "bocha",
                                            "enabled": True})
        assert saved["ok"] and saved["websearch"]["provider"] == "bocha"
        assert saved["websearch"]["enabled"] is True
        assert saved["websearch"]["key_saved"] is False
        settings = call(ui, "settings")
        assert settings["websearch"]["provider"] == "bocha"
        assert settings["agent_submit_mode"] == "confirm"


class TestSkillsApi:
    def test_skill_crud(self, ui):
        written = call(ui, "skill.save", {"name": "ui-skill",
                                          "description": "UI 测试技能",
                                          "body": "步骤一\n步骤二"})
        assert written["ok"]
        doc = call(ui, "skill.read", {"name": "ui-skill"})
        assert "步骤一" in doc["content"]
        listed = call(ui, "skill.list")
        assert any(s["name"] == "ui-skill" for s in listed["skills"])
        deleted = call(ui, "skill.delete", {"name": "ui-skill"})
        assert deleted["ok"]


