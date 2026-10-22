from pathlib import Path

import pytest

from rsci.core.errors import RemoteError, ValidationError
from tests.conftest import ROOT


def test_interrupted_download_can_retry_same_path(app_with_fake, tmp_path, monkeypatch):
    app, transport = app_with_fake
    remote = f"{ROOT}/OUTCAR"
    transport.state.files["cl9"][remote] = b"complete output"
    target = tmp_path / "OUTCAR"
    original = transport.scp_from_stage

    def interrupted(stage, local, **kwargs):
        Path(local).write_bytes(b"partial")
        raise RemoteError("connection interrupted")

    monkeypatch.setattr(transport, "scp_from_stage", interrupted)
    with pytest.raises(RemoteError):
        app.client().download(remote, target, server="cl9")
    assert not target.exists()
    assert not list(tmp_path.glob(".OUTCAR.*.part"))
    monkeypatch.setattr(transport, "scp_from_stage", original)
    app.client().download(remote, target, server="cl9")
    assert target.read_bytes() == b"complete output"


def test_bad_download_hash_never_publishes_file(app_with_fake, tmp_path, monkeypatch):
    app, transport = app_with_fake
    remote = f"{ROOT}/OUTCAR"
    transport.state.files["cl9"][remote] = b"correct data"
    monkeypatch.setattr(transport, "scp_from_stage",
                        lambda stage, local, **kwargs: Path(local).write_bytes(b"corrupt data"))
    with pytest.raises(RemoteError, match="SHA-256"):
        app.client().download(remote, tmp_path / "OUTCAR", server="cl9")
    assert not (tmp_path / "OUTCAR").exists()
    assert not list(tmp_path.glob("*.part"))


def test_target_created_during_download_is_never_overwritten(app_with_fake, tmp_path, monkeypatch):
    app, transport = app_with_fake
    remote = f"{ROOT}/OUTCAR"
    transport.state.files["cl9"][remote] = b"downloaded data"
    target = tmp_path / "OUTCAR"
    original = transport.scp_from_stage

    def competing_writer(stage, local, **kwargs):
        original(stage, local, **kwargs)
        target.write_bytes(b"another writer's data")

    monkeypatch.setattr(transport, "scp_from_stage", competing_writer)
    with pytest.raises(ValidationError, match="never overwrite"):
        app.client().download(remote, target, server="cl9")
    assert target.read_bytes() == b"another writer's data"
    assert not list(tmp_path.glob("*.part"))
