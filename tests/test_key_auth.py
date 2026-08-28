"""Key-based server auth (auth_mode=key) and auto-reconnect behavior.

Layers covered here: local config migration/serialization and the
GatewayClient surface against the FakeTransport.  Gateway-level lifecycle
(generate -> install -> crash -> auto-reconnect) lives in
test_gateway_sim.py against the REAL gateway script.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import ROOT


# ----------------------------------------------------------------- config
def test_legacy_entry_migrates_to_interactive():
    from vaspilot.core.config import ServerEntry
    entry = ServerEntry.from_dict({
        "name": "cl9", "target": "u@h", "port": 22,
        "remote_root": ROOT, "persist": "8h", "scheduler": "pbs",
    })  # no auth fields at all
    assert entry.auth_mode == "interactive"
    assert entry.auto_connect is False


def test_key_mode_round_trip():
    from vaspilot.core.config import ServerEntry
    entry = ServerEntry(name="cl9", target="u@h", port=22,
                        remote_root=ROOT, persist="8h", scheduler="pbs",
                        auth_mode="key", auto_connect=True)
    data = entry.to_dict()
    assert data["auth_mode"] == "key" and data["auto_connect"] is True
    again = ServerEntry.from_dict(data)
    assert again.auth_mode == "key" and again.auto_connect is True


def test_bad_auth_mode_falls_back():
    from vaspilot.core.config import ServerEntry
    entry = ServerEntry(name="x", target="u@x", auth_mode="password123")
    assert entry.auth_mode == "interactive"


# ------------------------------------------------------ name-injection guard
def test_key_paths_reject_injected_names():
    from vaspilot.gateway import vaspilot_gateway as gw
    for bad in ("../evil", "a/b", ".", "..", "x y"):
        with pytest.raises(Exception):
            gw._key_paths(bad)
    priv, pub = gw._key_paths("cl9")
    assert priv.parent.name == "vaspilot" and pub.suffix == ".pub"


# ------------------------------------------------------- client-level flows
def test_key_setup_full_flow(app_with_fake):
    app, _transport = app_with_fake
    client = app.client()
    result = client.key_setup("cl9")
    assert result["ok"] is True
    assert result["auth_mode"] == "key"
    assert result["auto_connect"] is True
    assert result["batch_login_verified"] is True
    entry = client.server_entry("cl9")
    assert entry.auth_mode == "key" and entry.auto_connect is True


def test_key_status_never_leaks_material(app_with_fake):
    app, transport = app_with_fake
    client = app.client()
    client.key_generate("cl9")
    payload = client.key_status("cl9")
    blob = json.dumps(payload, ensure_ascii=False)
    assert "vaspilot/" not in blob          # key directory name
    assert "PRIVATE" not in blob
    assert "FAKEPUB" not in blob
    assert payload["key_material_present"] is True


def test_key_install_requires_generated_key(app_with_fake):
    app, transport = app_with_fake
    client = app.client()
    result = client.key_install("cl9")
    assert result["ok"] is False
    assert result["error"]["code"] == "key_missing"


def test_key_install_rejected_surfaces_code(app_with_fake):
    app, transport = app_with_fake
    client = app.client()
    client.key_generate("cl9")
    transport.state.key_reject = True
    result = client.key_install("cl9")
    assert result["ok"] is False
    assert result["error"]["code"] == "key_verify_failed"


def test_key_revoke_needs_exact_confirm(app_with_fake):
    app, transport = app_with_fake
    client = app.client()
    client.key_generate("cl9")
    result = client.key_revoke("cl9", confirm_server="other")
    assert result["ok"] is False
    assert result["error"]["code"] == "confirm_mismatch"


def test_interactive_connect_never_autofills(app_with_fake):
    app, transport = app_with_fake
    client = app.client()
    from vaspilot.core.errors import AuthRequiredError
    with pytest.raises(AuthRequiredError):
        client.connect("cl9")               # interactive server


def test_ensure_session_key_mode_reconnects(app_with_fake):
    app, transport = app_with_fake
    client = app.client()
    client.key_generate("cl9")
    client.key_install("cl9")               # flips cl9 to key/auto
    transport.state.connected["cl9"] = False   # session lost
    result = client.ensure_session("cl9")
    assert result["ok"] is True and result["connected"] is True
