"""DPAPI secret box + provider key vault round trips."""

from __future__ import annotations

import pytest

from vaspilot.core import secretbox

pytestmark = pytest.mark.skipif(
    not secretbox.available(), reason="DPAPI requires Windows")


def test_protect_unprotect_roundtrip():
    plaintext = "sk-test-世界-key".encode("utf-8")
    blob = secretbox.protect(plaintext)
    assert blob != plaintext
    assert secretbox.unprotect(blob) == plaintext


def test_wrong_entropy_fails():
    blob = secretbox.protect(b"secret", entropy=b"one")
    with pytest.raises(OSError):
        secretbox.unprotect(blob, entropy=b"two")


def test_b64_helpers():
    encoded = secretbox.protect_b64("k&=!x")
    assert secretbox.unprotect_b64(encoded) == "k&=!x"
    # the base64 alphabet can contain any letter; what matters is that the
    # plaintext itself is nowhere in the stored form
    assert "k&=!x" not in encoded
    import base64
    assert b"k&=!x" not in base64.b64decode(encoded)


class TestProviderKeyVault:
    def test_store_read_delete(self, config_home):
        from vaspilot.core.config import Config
        config = Config(config_home)
        config.set_provider_key("glm", "sk-live-abc")
        assert config.provider_key_saved("glm") is True
        assert config.get_provider_key("glm") == "sk-live-abc"
        config.remove_provider_key("glm")
        assert config.provider_key_saved("glm") is False
        assert config.get_provider_key("glm") == ""

    def test_key_never_appears_in_settings_json(self, config_home):
        from vaspilot.core.config import Config
        config = Config(config_home)
        config.set_provider_key("glm", "SK-PLAINTEXT-MARKER")
        raw = (config_home / "settings.json").read_text(encoding="utf-8")
        assert "SK-PLAINTEXT-MARKER" not in raw
        # the DPAPI blob is there instead
        assert "provider_keys" in raw
