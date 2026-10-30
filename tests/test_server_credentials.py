"""Encrypted server credentials and periodic retention checks."""

from __future__ import annotations

import json

import pytest

from rsci.core import secretbox
from rsci.core.credentials import normalize_totp_seed, totp_code

pytestmark = pytest.mark.skipif(
    not secretbox.available(), reason="server credential vault uses Windows DPAPI")


def test_totp_normalization_and_rfc_vector():
    assert normalize_totp_seed("JBSW Y3DP-EHPK3PXP") == "JBSWY3DPEHPK3PXP"
    assert totp_code("JBSWY3DPEHPK3PXP", at=0) == "282760"


def test_server_credentials_are_encrypted_and_need_confirmation(config_home):
    from rsci.core.config import Config, ServerEntry

    config = Config(config_home)
    config.upsert_server(ServerEntry("cl12", "user@cl12", route="direct"))
    saved = config.save_server_credentials(
        "cl12", password="P@ssword!", totp_seed="JBSWY3DPEHPK3PXP",
        confirm_interval_days=3)
    assert saved["saved"] is True and saved["confirmation_due"] is False
    raw = json.dumps(config.load_settings(), ensure_ascii=False)
    assert "P@ssword!" not in raw and "JBSWY3DPEHPK3PXP" not in raw
    assert config.get_server_credentials("cl12")["password"] == "P@ssword!"

    data = config.load_settings()
    data["server_credentials"]["cl12"]["confirmed_at"] = "2000-01-01T00:00:00+00:00"
    config.save_settings(data)
    assert config.server_credentials_status("cl12")["confirmation_due"] is True
    assert config.get_server_credentials("cl12") == {}
    config.confirm_server_credentials("cl12")
    assert config.server_credentials_status("cl12")["confirmation_due"] is False


def test_vlab_route_auto_reconnect_uses_local_vault_only(app_with_fake):
    app, transport = app_with_fake
    password = "Vlab-target-password!"
    seed = "JBSWY3DPEHPK3PXP"
    app.config.save_server_credentials(
        "cl9", password=password, totp_seed=seed,
        confirm_interval_days=3)
    transport.state.connected["cl9"] = False

    status = app.client().status("cl9")

    assert status["connected"] is True
    assert transport.credential_connects == [{
        "server": "cl9", "password_present": True, "totp_present": True}]
    assert password not in json.dumps(transport.calls)
    assert seed not in json.dumps(transport.calls)
    audit_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in app.config.audit_dir.glob("audit-*.jsonl"))
    assert password not in audit_text and seed not in audit_text
