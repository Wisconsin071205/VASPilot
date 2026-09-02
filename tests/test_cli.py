"""CLI end-to-end over the fake transport: JSON output + exit codes."""

from __future__ import annotations

import io
import json
import sys

import pytest

from vaspilot.cli.main import main


def run_cli(argv, monkeypatch, stdin=None):
    captured_out = io.StringIO()
    captured_err = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured_out)
    monkeypatch.setattr(sys, "stderr", captured_err)
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    try:
        code = main(argv)
    except SystemExit as exc:  # argparse usage errors
        code = int(exc.code or 0)
    text = captured_out.getvalue()
    document = None
    try:
        document = json.loads(text)  # single pretty-printed document
    except json.JSONDecodeError:
        lines = [ln for ln in text.splitlines() if ln.strip().startswith("{")]
        if len(lines) == 1:
            try:
                document = json.loads(lines[0])
            except json.JSONDecodeError:
                document = None
        elif lines:
            document = []
            for ln in lines:
                try:
                    document.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
    return code, document, text, captured_err.getvalue()


@pytest.fixture()
def cli(config_home, fake_state, monkeypatch):
    from vaspilot.cli.main import App
    from vaspilot.core.config import Config, ServerEntry
    from tests.conftest import FakeTransport

    config = Config(config_home)
    config.upsert_server(ServerEntry(name="cl9", target="user@cl9", port=22,
                                     remote_root="/hpc/home/tester/vaspilot-root",
                                     persist="8h", scheduler="slurm"))
    config.set_default_server("cl9")
    transport = FakeTransport(fake_state)

    def fake_transport_method(self):
        return transport

    monkeypatch.setattr(App, "transport", fake_transport_method)
    return transport


class TestServerCommands:
    def test_list(self, cli, monkeypatch):
        code, document, _, _ = run_cli(["server", "list"], monkeypatch)
        assert code == 0
        assert document["ok"] is True
        names = {s["name"] for s in document["servers"]}
        assert "cl9" in names

    def test_status_auth_required(self, cli, fake_state, monkeypatch):
        fake_state.connected["cl9"] = False
        code, document, _, _ = run_cli(["server", "status", "cl9"], monkeypatch)
        assert code == 3
        assert document["error"]["code"] == "auth_required"
        # the error must tell the user to authenticate themselves
        assert "server connect" in document["error"]["message"]

    def test_status_connected(self, cli, monkeypatch):
        code, document, _, _ = run_cli(["server", "status"], monkeypatch)
        assert code == 0
        assert document["connected"] is True


class TestRemoteCommands:
    def test_list_confines_paths(self, cli, monkeypatch):
        code, document, _, _ = run_cli(
            ["remote", "list", "/etc"], monkeypatch)
        assert code == 5
        assert document["error"]["code"] == "validation_error"

    def test_read_refuses_potcar(self, cli, monkeypatch):
        path = "/hpc/home/tester/vaspilot-root/run/POTCAR"
        code, document, _, _ = run_cli(["remote", "read", path], monkeypatch)
        assert code == 5
        assert "POTCAR" in document["error"]["message"]

    def test_upload_download_roundtrip(self, cli, fake_state, tmp_path,
                                       monkeypatch):
        local = tmp_path / "INCAR"
        local.write_text("SYSTEM = x\n", encoding="utf-8")
        code, document, _, _ = run_cli(
            ["remote", "upload", str(local),
             "/hpc/home/tester/vaspilot-root/run/INCAR"], monkeypatch)
        assert code == 0
        assert document["sha256"]
        target = tmp_path / "downloaded"
        code, document, _, _ = run_cli(
            ["remote", "download",
             "/hpc/home/tester/vaspilot-root/run/INCAR", str(target)],
            monkeypatch)
        assert code == 0
        assert target.read_text(encoding="utf-8") == "SYSTEM = x\n"
        # downloads never overwrite
        code, document, _, _ = run_cli(
            ["remote", "download",
             "/hpc/home/tester/vaspilot-root/run/INCAR", str(target)],
            monkeypatch)
        assert code == 5

    def test_purge_requires_double_match(self, cli, fake_state, monkeypatch):
        code, document, _, _ = run_cli(
            ["remote", "trash", "/hpc/home/tester/vaspilot-root/runs/bad"],
            monkeypatch)
        assert code == 0
        trash_id = document["trash_id"]
        # without the confirm the purge refuses
        code, document, _, _ = run_cli(
            ["remote", "purge", trash_id, "--confirm-trash-id", "nope"],
            monkeypatch)
        assert code == 5
        code, document, _, _ = run_cli(
            ["remote", "purge", trash_id, "--confirm-trash-id", trash_id],
            monkeypatch)
        assert code == 0
        assert document["purged"] == trash_id


class TestWorkspaceCommands:
    def test_open_uses_vlab_workspace_gateway_not_remote_ssh(self, cli, monkeypatch):
        path = "/hpc/home/tester/vaspilot-root/runs/relax"
        code, document, _, _ = run_cli(
            ["workspace", "open", "--server", "cl9", "--path", path], monkeypatch)
        assert code == 0
        assert document["workspace_id"] == "ws-1234abcd"
        assert document["mode"] == "read-write"
        assert ["workspace", "open", "--server", "cl9", "--path", path,
                "--mode", "full"] in cli.calls

    def test_status_and_close_are_structured(self, cli, monkeypatch):
        run_cli(["workspace", "open", "--server", "cl9", "--path",
                 "/hpc/home/tester/vaspilot-root/runs/relax"], monkeypatch)
        code, document, _, _ = run_cli(
            ["workspace", "status", "--workspace", "ws-1234abcd"], monkeypatch)
        assert code == 0
        assert document["status"] == "open"
        code, document, _, _ = run_cli(
            ["workspace", "close", "--workspace", "ws-1234abcd"], monkeypatch)
        assert code == 0
        assert document["closed"] is True


class TestJobCommands:
    def test_progress_reports_science_only(self, cli, monkeypatch):
        code, document, _, _ = run_cli(
            ["job", "progress", "/hpc/home/tester/vaspilot-root/runs/good"],
            monkeypatch)
        assert code == 0
        assert document["scientific_converged"] is True

    def test_cancel_requires_double_match(self, cli, monkeypatch):
        code, document, _, _ = run_cli(
            ["job", "cancel", "123", "--confirm-job-id", "124"], monkeypatch)
        assert code == 5
        code, document, _, _ = run_cli(
            ["job", "cancel", "123", "--confirm-job-id", "123"], monkeypatch)
        assert code == 0


class TestProviderCommands:
    def test_add_list_remove_default(self, config_home, monkeypatch):
        code, document, _, _ = run_cli(
            ["agent", "provider", "add", "--id", "ds", "--name", "DeepSeek",
             "--protocol", "openai-chat-compatible",
             "--base-url", "https://api.example.com/v1",
             "--model", "deepseek-chat",
             "--api-key-env", "DEEPSEEK_API_KEY"], monkeypatch)
        assert code == 0
        # only the env-var NAME may appear; never a key field or value
        assert '"api_key"' not in json.dumps(document)
        assert document["added"]["api_key_env"] == "DEEPSEEK_API_KEY"
        code, document, _, _ = run_cli(["agent", "provider", "list"], monkeypatch)
        assert [p["id"] for p in document["providers"]] == ["ds"]
        assert document["default"] == "ds"
        code, document, _, _ = run_cli(
            ["agent", "provider", "set-default", "ds"], monkeypatch)
        assert code == 0
        code, document, _, _ = run_cli(
            ["agent", "provider", "remove", "ds"], monkeypatch)
        assert code == 0

    def test_provider_add_requires_model(self, config_home, monkeypatch):
        code, document, _, _ = run_cli(
            ["agent", "provider", "add", "--id", "x", "--name", "X",
             "--protocol", "openai-chat-compatible",
             "--base-url", "https://x.example", "--model", ""], monkeypatch)
        assert code == 5


class TestMonitorCommands:
    def test_snapshot(self, cli, monkeypatch):
        code, document, _, _ = run_cli(["monitor", "snapshot"], monkeypatch)
        assert code == 0
        assert document["ok"] is True
        assert document["total"] >= 1
        entry = next(e for e in document["servers"] if e["server"] == "cl9")
        assert entry["connected"] is True

    def test_watch_rejects_tiny_interval(self, cli, monkeypatch):
        code, document, _, _ = run_cli(
            ["monitor", "watch", "--interval", "1"], monkeypatch)
        assert code == 5


class TestUsage:
    def test_unknown_group_exits_usage(self, monkeypatch):
        code, document, _, _ = run_cli(["nope"], monkeypatch)
        assert code == 2
