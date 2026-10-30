import json

import pytest

from rsci.core import secretbox
from rsci.core.config import Config
from rsci.core.migration import migrate


pytestmark = pytest.mark.skipif(not secretbox.available(), reason="Requires Windows DPAPI")


def test_migration_preserves_data_and_reencrypts_credentials(tmp_path):
    source, target = tmp_path / "old", tmp_path / "new"
    source.mkdir()
    target.mkdir()
    settings = {"providers": [{"id": "model"}], "provider_keys": {
        "model": secretbox.protect_b64("sample-key", entropy=b"VaspLoom-provider-key/v1")},
        "server_credentials": {"cluster": {
            "password": secretbox.protect_b64("sample-password", entropy=b"VaspLoom-server-credential/v1:cluster")}}}
    raw = json.dumps(settings)
    (source / "settings.json").write_text(raw, encoding="utf-8")
    (source / "servers.json").write_text('{"servers": []}', encoding="utf-8")
    (source / "ui.json").write_text('"old-session"', encoding="utf-8")
    (target / "ui.json").write_text('"current-session"', encoding="utf-8")
    result = migrate(source, target, "VaspLoom")
    assert result["reencrypted_credentials"] == 2
    assert Config(target).get_provider_key("model") == "sample-key"
    assert Config(target)._decrypt_server_credential("cluster", "password") == "sample-password"
    assert (source / "settings.json").read_text(encoding="utf-8") == raw
    assert (target / "ui.json").read_text(encoding="utf-8") == '"current-session"'
    with pytest.raises(ValueError, match="refusing to overwrite"):
        migrate(source, target, "VaspLoom")


def test_vaspilot_provider_key_migrates_to_rsci(tmp_path):
    source, target = tmp_path / "vaspilot", tmp_path / "rsci"
    source.mkdir()
    settings = {"provider_keys": {
        "model": secretbox.protect_b64(
            "sample-key", entropy=b"VASPilot-provider-key/v1")}}
    (source / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    result = migrate(source, target, "VASPilot")

    assert result["reencrypted_credentials"] == 1
    assert Config(target).get_provider_key("model") == "sample-key"


def test_wrong_entropy_leaves_destination_unconfigured(tmp_path):
    source, target = tmp_path / "old", tmp_path / "new"
    source.mkdir()
    (source / "settings.json").write_text(json.dumps({"provider_keys": {
        "model": secretbox.protect_b64("sample-key", entropy=b"DifferentApp")}}), encoding="utf-8")
    with pytest.raises(OSError):
        migrate(source, target, "OldApp")
    assert not target.exists()
