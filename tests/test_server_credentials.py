"""Encrypted direct-server credentials and periodic retention checks."""

from __future__ import annotations

import json

import pytest

from vaspilot.core import secretbox
from vaspilot.core.credentials import normalize_totp_seed, totp_code

pytestmark = pytest.mark.skipif(
    not secretbox.available(), reason="server credential vault uses Windows DPAPI")


def test_totp_normalization_and_rfc_vector():
    assert normalize_totp_seed("JBSW Y3DP-EHPK3PXP") == "JBSWY3DPEHPK3PXP"
    assert totp_code("JBSWY3DPEHPK3PXP", at=0) == "282760"


def test_server_credentials_are_encrypted_and_need_confirmation(config_home):
    from vaspilot.core.config import Config, ServerEntry

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
