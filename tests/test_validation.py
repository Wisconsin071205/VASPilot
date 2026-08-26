"""Security unit tests: traversal, injection, identifiers, double-confirm."""

from __future__ import annotations

import pytest

from vaspilot.core.errors import ValidationError
from vaspilot.core.validation import (confirm_match, join_remote,
                                      local_project_path, no_shell_meta,
                                      remote_path, valid_filename, valid_glob,
                                      valid_job_id, valid_server_name,
                                      valid_trash_id)

ROOT = "/hpc/home/tester/vaspilot-root"


class TestRemotePath:
    def test_rejects_traversal(self):
        for bad in (f"{ROOT}/../etc/passwd", f"{ROOT}/sub/../../x",
                    f"{ROOT}/.", f"{ROOT}/.."):
            with pytest.raises(ValidationError):
                remote_path(bad, remote_root=ROOT)

    def test_rejects_relative_and_control_chars(self):
        with pytest.raises(ValidationError):
            remote_path("relative/path", remote_root=ROOT)
        with pytest.raises(ValidationError):
            remote_path(f"{ROOT}/x\x00y", remote_root=ROOT)

    def test_rejects_outside_root(self):
        for bad in ("/etc/passwd", "/hpc/home/tester", "/hpc/home/other/x"):
            with pytest.raises(ValidationError):
                remote_path(bad, remote_root=ROOT)

    def test_accepts_root_itself_and_children(self):
        assert remote_path(ROOT, remote_root=ROOT) == ROOT
        assert remote_path(f"{ROOT}/a/b", remote_root=ROOT) == f"{ROOT}/a/b"

    def test_join_remote_validates_segments(self):
        assert join_remote(ROOT, "runs", "case1") == f"{ROOT}/runs/case1"
        with pytest.raises(ValidationError):
            join_remote(ROOT, "..")


class TestShellInjection:
    @pytest.mark.parametrize("payload", [
        "INCAR; rm -rf /", "file`id`", "x$(whoami)", "a && b", "n|c",
        "p>o", "q<'s>", "space name", "tab\tname",
    ])
    def test_filenames_reject_metacharacters(self, payload):
        with pytest.raises(ValidationError):
            valid_filename(payload)

    @pytest.mark.parametrize("payload", [
        "*.sh; cat /etc/shadow", "$(id)", "`id`", "a b", "x;y",
    ])
    def test_globs_reject_metacharacters(self, payload):
        with pytest.raises(ValidationError):
            valid_glob(payload)

    def test_no_shell_meta_rejects(self):
        with pytest.raises(ValidationError):
            no_shell_meta("value; rm", label="test")
        assert no_shell_meta("clean-value_1.2", label="test") == "clean-value_1.2"

    def test_valid_glob_accepts_reasonable_patterns(self):
        assert valid_glob("POSCAR*") == "POSCAR*"
        assert valid_glob("OUTCAR") == "OUTCAR"


class TestIdentifiers:
    def test_server_names(self):
        assert valid_server_name("cl9") == "cl9"
        for bad in ("", "a b", "../etc", "x;rm", "9" * 40 + "toolong"):
            with pytest.raises(ValidationError):
                valid_server_name(bad)

    def test_job_ids(self):
        assert valid_job_id("12345") == "12345"
        assert valid_job_id("12345.server") == "12345.server"
        for bad in ("abc", "12;rm", "", "1/../2"):
            with pytest.raises(ValidationError):
                valid_job_id(bad)

    def test_trash_ids(self):
        good = "20260101T000000Z-deadbeef"
        assert valid_trash_id(good) == good
        with pytest.raises(ValidationError):
            valid_trash_id("../etc")

    def test_double_confirm(self):
        assert confirm_match("42", "42", label="cancel") == "42"
        with pytest.raises(ValidationError):
            confirm_match("41", "42", label="cancel")


class TestLocalProjectPath:
    def test_confines_to_project_root(self, tmp_path):
        root = tmp_path / "project"
        (root / "sub").mkdir(parents=True)
        inside = local_project_path("sub/file", project_root=root)
        assert str(inside).startswith(str(root.resolve()))
        with pytest.raises(ValidationError):
            local_project_path("../outside", project_root=root)
        with pytest.raises(ValidationError):
            local_project_path("sub/../../escape", project_root=root)

    def test_absolute_inside_root_accepted(self, tmp_path):
        root = tmp_path / "project"
        root.mkdir()
        inside = local_project_path(str(root / "file.txt"), project_root=root)
        assert str(inside).startswith(str(root.resolve()))
