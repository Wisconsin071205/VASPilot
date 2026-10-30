"""WebDAV client helpers, encrypted settings and the sync state machine."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from rsci.agents.memory import ConversationStore
from rsci.core import secretbox
from rsci.core.config import Config, ProviderEntry, ServerEntry
from rsci.sync.engine import SyncEngine, is_allowed_rel
from rsci.sync.webdav import RemoteEntry, normalize_url, validate_remote_dir
from rsci.core.errors import ValidationError


class FakeWebDAV:
    """In-memory WebDAV store shared by multiple SyncEngine instances."""

    def __init__(self, files: dict[str, bytes] | None = None):
        self.files = dict(files or {})
        self.dirs = {""}
        self.etags = {path: "e0" for path in self.files}
        self.etag_seq = 0
        self.calls: list[tuple] = []

    def _stat(self, rel: str) -> RemoteEntry | None:
        rel = rel.strip("/")
        children = [p for p in self.files if p == rel or p.startswith(rel + "/")]
        is_dir = rel in self.dirs or any(p.startswith(rel + "/") for p in self.files)
        if rel and rel not in self.files and not is_dir:
            return None
        data = self.files.get(rel) if rel in self.files else b""
        return RemoteEntry(rel=rel, href="/" + rel, is_dir=is_dir,
                           size=len(data), mtime=time.time(),
                           etag=self.etags.get(rel, "e0"))

    def ensure_dir(self, rel: str) -> None:
        self.dirs.add(rel.strip("/"))

    def stat(self, rel: str) -> RemoteEntry | None:
        return self._stat(rel)

    def list_tree(self, rel: str = "") -> dict[str, RemoteEntry]:
        rel = rel.strip("/")
        out = {}
        for path in self.files:
            if path.startswith(rel + "/"):
                out[path] = self._stat(path)
        for path in self.dirs:
            if path.startswith(rel + "/") and path != rel:
                out[path] = self._stat(path)
        return out

    def get(self, rel: str) -> bytes:
        return self.files[rel.strip("/")]

    def put(self, rel: str, data: bytes) -> None:
        rel = rel.strip("/")
        self.etag_seq += 1
        self.files[rel] = bytes(data)
        self.etags[rel] = f"e{self.etag_seq}"
        # create implied parent dirs
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            self.dirs.add("/".join(parts[:i]))

    def delete(self, rel: str) -> None:
        rel = rel.strip("/")
        self.files.pop(rel, None)
        self.etags.pop(rel, None)


def make_engine(config: Config, remote: FakeWebDAV) -> SyncEngine:
    return SyncEngine(
        config,
        client_factory=lambda url, user, password: remote)


def configure(config: Config, *, password: str = "dav-password") -> None:
    config.set_webdav(
        url="https://pan.ustc.edu.cn/seafdav",
        username="test-user@auth.local",
        password=password,
        enabled=True,
        auto_sync=False,
        remote_dir="RSCI-sync",
        interval_minutes=15)


def test_normalize_and_remote_dir_validation():
    assert normalize_url("pan.ustc.edu.cn/seafdav/") == \
        "https://pan.ustc.edu.cn/seafdav"
    assert normalize_url("https://pan.ustc.edu.cn/seafdav") == \
        "https://pan.ustc.edu.cn/seafdav"
    with pytest.raises(ValidationError):
        normalize_url("ftp://example.com/dav")
    with pytest.raises(ValidationError):
        normalize_url("https://user:pass@example.com/dav")
    assert validate_remote_dir("") == "RSCI-sync"
    assert validate_remote_dir("/RSCI-sync/backup/") == "RSCI-sync/backup"
    with pytest.raises(ValidationError):
        validate_remote_dir("../outside")
    with pytest.raises(ValidationError):
        validate_remote_dir("a//b")


def test_webdav_password_is_encrypted_and_survives_username_change(config_home):
    config = Config(config_home)
    configure(config)
    raw = config.settings_path.read_text(encoding="utf-8")
    assert "dav-password" not in raw
    assert config.webdav()["password_saved"] is True
    assert config.webdav_password() == "dav-password"

    # Changing only the username must re-encrypt the preserved password under
    # the new entropy label rather than leaving an undecryptable blob.
    config.set_webdav(username="another@example.com")
    assert config.webdav_password() == "dav-password"
    assert config.webdav()["username"] == "another@example.com"

    config.clear_webdav_password()
    assert config.webdav()["password_saved"] is False
    assert config.webdav_password() == ""


def test_initial_sync_uploads_only_portable_data(config_home):
    config = Config(config_home)
    settings = config.load_settings()
    settings.update({
        "providers": [{"id": "mock", "name": "Mock", "protocol": "openai-chat-compatible",
                       "base_url": "https://api.example.com/v1", "model": "m",
                       "api_key_env": ""}],
        "server_credentials": {"cl9": {"password": "encrypted-marker"}},
        "agent_submit_mode": "confirm",
    })
    config.save_settings(settings)
    config.set_provider_key("mock", "sk-plain-marker")
    config.upsert_server(ServerEntry(name="cl9", target="user@cl9",
                                     remote_root="/scratch/me", scheduler="slurm"))
    configure(config)
    store = ConversationStore(config.chat_dir)
    session = store.create_session(title="同步测试")
    store.append(session["session_id"], "user", "hello webdav")

    remote = FakeWebDAV()
    result = make_engine(config, remote).sync()
    assert result["ok"], result
    assert "RSCI-sync/settings.json" in remote.files
    assert "RSCI-sync/servers.json" in remote.files
    assert f"RSCI-sync/chat/{session['session_id']}.json" in remote.files
    remote_settings = json.loads(remote.files["RSCI-sync/settings.json"])
    assert "webdav" not in remote_settings
    assert "provider_keys" not in remote_settings
    assert "server_credentials" not in remote_settings
    assert "sk-plain-marker" not in remote.files["RSCI-sync/settings.json"].decode()
    assert "dav-password" not in remote.files["RSCI-sync/settings.json"].decode()
    assert remote.files["RSCI-sync/servers.json"]
    # Local secret material is still intact and usable.
    assert config.get_provider_key("mock") == "sk-plain-marker"


def test_second_device_pulls_config_and_chat_without_losing_local_secrets(config_home, tmp_path):
    first = Config(config_home)
    settings = first.load_settings()
    settings.update({"providers": [{"id": "mock", "name": "Mock",
                                    "protocol": "openai-chat-compatible",
                                    "base_url": "https://api.example.com/v1",
                                    "model": "m", "api_key_env": ""}],
                     "agent_submit_mode": "auto"})
    first.save_settings(settings)
    first.upsert_server(ServerEntry(name="cl9", target="user@cl9",
                                    remote_root="/scratch/me", scheduler="slurm"))
    configure(first)
    store = ConversationStore(first.chat_dir)
    session = store.create_session(title="跨设备")
    store.append(session["session_id"], "user", "from first device")
    remote = FakeWebDAV()
    assert make_engine(first, remote).sync()["ok"]

    second_home = tmp_path / "rsci-second"
    second_home.mkdir()
    second = Config(second_home)
    configure(second, password="second-password")
    second.set_provider_key("mock", "second-local-key")
    result = make_engine(second, remote).sync()
    assert result["ok"], result
    assert second.load_providers()[0].id == "mock"
    assert second.agent_submit_mode() == "auto"
    assert [s.name for s in second.load_servers()] == ["cl9"]
    pulled = ConversationStore(second.chat_dir).load(session["session_id"])
    assert pulled and pulled["messages"][0]["content"] == "from first device"
    # The second device's own password and API key survive the pull.
    assert second.webdav_password() == "second-password"
    assert second.get_provider_key("mock") == "second-local-key"


def test_concurrent_chat_edits_merge_and_remote_converges(config_home, tmp_path):
    first = Config(config_home)
    configure(first)
    store1 = ConversationStore(first.chat_dir)
    session = store1.create_session(title="冲突合并")
    store1.append(session["session_id"], "user", "original")
    remote = FakeWebDAV()
    assert make_engine(first, remote).sync()["ok"]

    second_home = tmp_path / "rsci-second"
    second_home.mkdir()
    second = Config(second_home)
    configure(second)
    assert make_engine(second, remote).sync()["ok"]

    store1.append(session["session_id"], "assistant", "answer on device one")
    make_engine(first, remote).sync()
    store2 = ConversationStore(second.chat_dir)
    store2.append(session["session_id"], "assistant", "answer on device two")
    result = make_engine(second, remote).sync()
    assert result["ok"], result
    assert result["conflicts"]
    merged = json.loads(remote.files[f"RSCI-sync/chat/{session['session_id']}.json"])
    contents = [row["content"] for row in merged["messages"]]
    assert "answer on device one" in contents
    assert "answer on device two" in contents
    local_merged = store2.load(session["session_id"])
    assert local_merged is not None
    local_contents = [row["content"] for row in local_merged["messages"]]
    assert "answer on device one" in local_contents
    assert "answer on device two" in local_contents


def test_chat_deletion_propagates_both_ways(config_home, tmp_path):
    first = Config(config_home)
    configure(first)
    store1 = ConversationStore(first.chat_dir)
    session = store1.create_session(title="删除传播")
    store1.append(session["session_id"], "user", "delete me")
    remote = FakeWebDAV()
    assert make_engine(first, remote).sync()["ok"]

    second_home = tmp_path / "rsci-second"
    second_home.mkdir()
    second = Config(second_home)
    configure(second)
    assert make_engine(second, remote).sync()["ok"]

    store1.delete(session["session_id"])
    result = make_engine(first, remote).sync()
    assert result["ok"], result
    assert f"RSCI-sync/chat/{session['session_id']}.json" not in remote.files
    assert f"chat/{session['session_id']}.json" in result["deleted_remote"]
    second_result = make_engine(second, remote).sync()
    assert second_result["ok"], second_result
    assert ConversationStore(second.chat_dir).load(session["session_id"]) is None


def test_untrusted_remote_path_is_not_written(config_home):
    config = Config(config_home)
    configure(config)
    remote = FakeWebDAV({
        "RSCI-sync/approval.key": b"not allowed",
        "RSCI-sync/../escape.json": b"bad",
    })
    result = make_engine(config, remote).sync()
    assert not (config.home / "approval.key").exists()
    assert not (config.home.parent / "escape.json").exists()
    assert is_allowed_rel("settings.json")
    assert is_allowed_rel("chat/s-abc.json")
    assert not is_allowed_rel("approval.key")
    assert not is_allowed_rel("../settings.json")


def test_remote_deletion_of_provider_and_server_propagates(config_home, tmp_path):
    first = Config(config_home)
    for pid in ("one", "two"):
        first.add_provider(ProviderEntry(
            id=pid, name=pid, protocol="openai-chat-compatible",
            base_url=f"https://{pid}.example.com/v1", model="m"))
    first.upsert_server(ServerEntry(name="keep", target="user@keep",
                                    remote_root="/r/keep", scheduler="slurm"))
    first.upsert_server(ServerEntry(name="drop", target="user@drop",
                                    remote_root="/r/drop", scheduler="slurm"))
    configure(first)
    remote = FakeWebDAV()
    assert make_engine(first, remote).sync()["ok"]

    second_home = tmp_path / "rsci-second"
    second_home.mkdir()
    second = Config(second_home)
    configure(second)
    assert make_engine(second, remote).sync()["ok"]
    assert {p.id for p in second.load_providers()} == {"one", "two"}
    assert {s.name for s in second.load_servers()} == {"keep", "drop"}

    first.remove_provider("two")
    first.remove_server("drop")
    assert make_engine(first, remote).sync()["ok"]
    result = make_engine(second, remote).sync()
    assert result["ok"], result
    assert {p.id for p in second.load_providers()} == {"one"}
    assert {s.name for s in second.load_servers()} == {"keep"}
    # Reserved local secrets are not wiped by an authoritative remote pull.
    assert second.webdav()["password_saved"] is True
    assert second.webdav_password()
