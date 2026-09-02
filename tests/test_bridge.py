"""VS Code bridge server-side surface: structured write, stat, remove,
trash semantics and the ui.json discovery file."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from tests.conftest import ROOT
from tests.test_ui import call, ui  # noqa: F401 (fixture re-export)


def _write(ui, content, expected="", path=None):
    return call(ui, "remote.write", {
        "server": "cl9",
        "path": path or f"{ROOT}/runs/bridge-note.txt",
        "content": content,
        "expected_sha256": expected,
    })


class TestBridgeWrite:
    def test_new_file_roundtrip(self, ui):
        doc = _write(ui, "第一版内容\n")
        assert doc["ok"] is True
        sha = hashlib.sha256("第一版内容\n".encode("utf-8")).hexdigest()
        assert doc["sha256"] == sha
        back = call(ui, "remote.read", {"server": "cl9",
                                        "path": f"{ROOT}/runs/bridge-note.txt"})
        assert back["content"] == "第一版内容\n"

    def test_conflict_rejected_and_file_kept(self, ui):
        first = _write(ui, "版本 A\n")
        assert first["ok"] is True
        stale = hashlib.sha256(b"stale baseline").hexdigest()
        doc = _write(ui, "版本 B\n", expected=stale)
        assert doc["ok"] is False and doc["error"]["code"] == "remote_changed"
        back = call(ui, "remote.read", {"server": "cl9",
                                        "path": f"{ROOT}/runs/bridge-note.txt"})
        assert back["content"] == "版本 A\n"          # original kept

    def test_clean_update_with_baseline(self, ui):
        first = _write(ui, "base\n")
        doc = _write(ui, "update\n", expected=first["sha256"])
        assert doc["ok"] is True
        assert doc["sha256"] == hashlib.sha256(b"update\n").hexdigest()

    def test_denylist_refused(self, ui):
        doc = _write(ui, "x", path=f"{ROOT}/runs/CHGCAR")
        assert doc["ok"] is False
        assert doc["error"]["code"] == "text_denylist"


class TestBridgeStatRemove:
    def test_list_is_limited_and_remote_find_is_available(self, ui):
        state = ui["state"]
        for index in range(3):
            state.files["cl9"][f"{ROOT}/runs/limited-{index}.txt"] = b"x"
        listing = call(ui, "remote.list", {"server": "cl9",
                                             "path": f"{ROOT}/runs",
                                             "limit": 2})
        assert len(listing["entries"]) == 2
        assert listing["truncated"] is True
        found = call(ui, "remote.find", {"server": "cl9",
                                          "path": f"{ROOT}/runs",
                                          "pattern": "*.txt",
                                          "max_depth": 2,
                                          "limit": 10})
        assert found["ok"] is True
        assert found["root"] == f"{ROOT}/runs"

    def test_stat_reports_size_and_kind(self, ui):
        doc = call(ui, "remote.stat", {"server": "cl9",
                                       "path": f"{ROOT}/runs/good/INCAR"})
        assert doc["ok"] is True
        assert doc["size"] > 0
        assert "file" in doc.get("kind", "")

    def test_remove_goes_to_trash(self, ui):
        _write(ui, "to delete\n", path=f"{ROOT}/runs/trash-me.txt")
        doc = call(ui, "remote.remove", {"server": "cl9",
                                         "path": f"{ROOT}/runs/trash-me.txt"})
        assert doc["ok"] is True
        listing = call(ui, "remote.list", {"server": "cl9",
                                           "path": f"{ROOT}/runs"})
        assert "trash-me.txt" not in [e["name"] for e in listing["entries"]]
        trash = call(ui, "remote.trash.list", {"server": "cl9"})
        assert any(m["original_path"].endswith("trash-me.txt")
                   for m in trash["trash"])


class TestDiscoveryFile:
    def test_ui_json_written_on_serve(self, ui):
        """The bridge extension discovers the console through ui.json."""
        home = Path(os.environ["VASPILOT_HOME"])
        doc = json.loads((home / "ui.json").read_text(encoding="utf-8"))
        assert doc["url"].startswith("http://127.0.0.1:")
        assert doc["token"] == ui["token"]
