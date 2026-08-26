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
    yield {"base": base, "token": token, "app": app, "state": fake_state}
    httpd.shutdown()
    httpd.server_close()


class ScriptedProvider:
    protocol = "openai-chat-compatible"

    def __init__(self, script):
        self.script = list(script)
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

    def test_page_served_with_valid_token(self, ui):
        with urllib.request.urlopen(f"{ui['base']}/t/{ui['token']}",
                                    timeout=10) as response:
            html = response.read().decode("utf-8")
        assert "VASPilot" in html and "chatinput" in html


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
