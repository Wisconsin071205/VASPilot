"""Local project store: scaffold, whitelist, validation, POTCAR copy, trash."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from vaspilot.core.config import Config
from vaspilot.core.errors import ValidationError
from vaspilot.workflow.projects import ProjectStore, TEMPLATES


@pytest.fixture()
def store(config_home):
    config = Config(config_home)
    return ProjectStore(projects_dir=config.projects_dir,
                        index_path=config.projects_index_path)


class TestCreate:
    def test_create_with_template(self, store):
        result = store.create("Fe-relax", {
            "INCAR": TEMPLATES["relax"]["INCAR"],
            "KPOINTS": TEMPLATES["relax"]["KPOINTS"],
            "POSCAR": TEMPLATES["relax"]["POSCAR"]})
        assert result["name"] == "Fe-relax"
        assert Path(result["path"]).is_dir()
        assert {w["name"] for w in result["files"]} == \
            {"INCAR", "KPOINTS", "POSCAR"}
        assert (Path(result["path"]) / "INCAR").read_text(
            encoding="utf-8").startswith("SYSTEM = relax")

    def test_duplicate_rejected(self, store):
        store.create("dup", {"INCAR": "NSW=0\n"})
        with pytest.raises(ValidationError):
            store.create("dup", {"INCAR": "NSW=1\n"})

    def test_traversal_name_rejected(self, store):
        for bad in ("../escape", "..", "a/b", ".hidden", "x" * 65):
            with pytest.raises(ValidationError):
                store.create(bad, {"INCAR": "NSW=0\n"})

    def test_non_whitelisted_file_rejected(self, store):
        with pytest.raises(ValidationError):
            store.create("evil", {"evil.sh": "rm -rf /\n"})

    def test_potcar_text_rejected(self, store):
        with pytest.raises(ValidationError):
            store.create("p", {"POTCAR": "TITEL = stolen\n"})

    def test_potcar_copied_byte_for_byte(self, store, tmp_path):
        source = tmp_path / "POTCAR.Fe"
        payload = b"  TITEL  = PAW Fe 08Apr2002\n   ENMAX =  302.0\n" * 40
        source.write_bytes(payload)
        result = store.create("with-potcar",
                              {"INCAR": "NSW=0\n"}, potcar_path=source)
        target = Path(result["path"]) / "POTCAR"
        assert target.read_bytes() == payload          # byte-for-byte
        assert result["potcar"]["size"] == len(payload)

    def test_missing_potcar_source_rejected(self, store, tmp_path):
        with pytest.raises(ValidationError):
            store.create("bad", {"INCAR": "NSW=0\n"},
                         potcar_path=tmp_path / "nope")


class TestListPinDelete:
    def test_list_completeness(self, store):
        store.create("full", {"INCAR": "NSW=0\n", "KPOINTS": "k\n",
                              "POSCAR": "p\n"})
        store.create("partial", {"INCAR": "NSW=0\n"})
        listing = {p["name"]: p for p in store.list()}
        assert listing["full"]["complete"] is True
        assert listing["full"]["missing"] == ["POTCAR"]
        assert listing["partial"]["complete"] is False
        assert "POSCAR" in listing["partial"]["missing"]

    def test_pin_orders_first(self, store):
        store.create("aaa", {"INCAR": "NSW=0\n"})
        store.create("zzz", {"INCAR": "NSW=0\n"})
        store.pin("zzz", True)
        assert store.list()[0]["name"] == "zzz"
        assert store.list()[0]["pinned"] is True

    def test_delete_moves_to_trash(self, store):
        result = store.create("gone", {"INCAR": "NSW=0\n"})
        deleted = store.delete("gone")
        assert Path(deleted["moved_to"]).is_dir()
        assert not Path(result["path"]).exists()
        assert store.list() == []


class TestFileAccess:
    def test_read_write_whitelist(self, store):
        store.create("rw", {"INCAR": "NSW=0\n"})
        written = store.write_file("rw", "INCAR", "NSW=10\n")
        assert written["size"] == 7
        assert store.read_file("rw", "INCAR")["content"] == "NSW=10\n"
        store.write_file("rw", "run.job.sh", "#!/bin/bash\nsrun vasp_std\n")
        assert store.read_file("rw", "run.job.sh")["content"].startswith("#!")

    def test_write_outside_whitelist_rejected(self, store):
        store.create("lock", {"INCAR": "NSW=0\n"})
        for name in ("evil.sh", "CONTCAR", "OSZICAR", "sub/INCAR"):
            with pytest.raises(ValidationError):
                store.write_file("lock", name, "x")

    def test_potcar_metadata_only(self, store, tmp_path):
        source = tmp_path / "POTCAR.Na"
        source.write_bytes(b"  TITEL  = PAW Na_sv 08Apr2002\n"
                           b"   ENMAX =  302.0\n   LEXCH = PE\n")
        store.create("meta", {"INCAR": "NSW=0\n"}, potcar_path=source)
        doc = store.read_file("meta", "POTCAR")
        assert "content" not in doc                 # never the text body
        assert doc["titel"] == "PAW Na_sv 08Apr2002"
        assert doc["enmax_ev"] == pytest.approx(302.0)
        assert len(doc["sha256"]) == 64
        with pytest.raises(ValidationError):
            store.write_file("meta", "POTCAR", "must fail")

    def test_copy_potcar_after_creation(self, store, tmp_path):
        store.create("late", {"INCAR": "NSW=0\n"})
        source = tmp_path / "POTCAR.Cl"
        source.write_bytes(b"  TITEL  = PAW Cl 08Apr2002\n")
        info = store.copy_potcar("late", source)
        assert info["size"] == source.stat().st_size
        listing = {p["name"]: p for p in store.list()}
        assert "POTCAR" not in listing["late"]["missing"]


class TestValidate:
    def test_validate_uses_vasp_rules(self, store):
        store.create("check", {"INCAR": "NSW=0\nIBRION=0\nALGO=All\n",
                               "KPOINTS": "auto\n0\nGamma\n2 2 2\n0 0 0\n",
                               "POSCAR": "s\n1.0\n3 0 0\n0 3 0\n0 0 3\n"
                                         "Na\n0\ndirect\n0 0 0\n"})
        result = store.validate("check")
        assert result["ok"] is False
        assert any("POSCAR" in e for e in result["errors"])

    def test_validate_relax_template_passes(self, store):
        template = {k: v for k, v in TEMPLATES["relax"].items()
                    if k != "description"}
        store.create("ok", template)
        result = store.validate("ok")
        assert result["ok"] is True
        assert result["errors"] == []
