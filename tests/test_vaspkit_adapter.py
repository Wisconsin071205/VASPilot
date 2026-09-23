"""VASPKIT command construction and the probe's verdict."""

from __future__ import annotations

import base64
import re

import pytest

from vaspilot.core.errors import ValidationError
from vaspilot.vaspkit.adapter import (GEN_OK, ProfileStore, doctor_script,
                                      generation_script, invoke, parse_doctor,
                                      remote_command, valid_command)

READY = {"command": "/opt/vaspkit/bin/vaspkit", "mode": "stdin", "ready": True}
STAGE_DIR = "/hpc/home/tester/vaspilot-root/campaigns/abc/02-static"


def probe_output(*, command="/opt/vaspkit/bin/vaspkit", pbe=True,
                 stdin="ok", task="ok", finished=True):
    lines = ["__VP_VK_CMD__", command, "__VP_VK_VERSION__", "VASPKIT 1.4.1",
             "__VP_VK_POT__"]
    lines.append("PBE_PATH|/data/pot/PBE|dir|320|yes" if pbe
                 else "PBE_PATH||missing|0|no")
    lines += ["GGA_PATH||missing|0|no", "LDA_PATH|/data/pot/LDA|dir|300|yes",
              "__VP_VK_TRY__"]
    if command:
        lines += [f"stdin|{stdin}", f"task|{task}"]
    if finished:
        lines.append("__VP_VK_END__")
    return "\n".join(lines) + "\n"


class TestCommand:
    @pytest.mark.parametrize("command", ["vaspkit", "/opt/vaspkit/bin/vaspkit",
                                         "~/bin/vaspkit.1.4"])
    def test_plain_commands_pass(self, command):
        assert valid_command(command) == command

    @pytest.mark.parametrize("command", ["", "vaspkit; rm -rf ~", "$(id)",
                                         "vaspkit -task 1", "a|b"])
    def test_anything_a_shell_expands_is_refused(self, command):
        with pytest.raises(ValidationError, match="plain executable"):
            valid_command(command)

    def test_stdin_mode_feeds_the_task_through_the_menu(self):
        assert invoke("vaspkit", "stdin", 102, ("2", "0.030")) == \
            "printf '%s\\n' 102 2 0.030 | vaspkit"

    def test_task_mode_names_the_task_and_feeds_only_answers(self):
        assert invoke("vaspkit", "task", 102, ("2", "0.030")) == \
            "printf '%s\\n' 2 0.030 | vaspkit -task 102"

    def test_task_mode_without_answers_closes_stdin(self):
        assert invoke("vaspkit", "task", 103) == "vaspkit -task 103 </dev/null"

    def test_unknown_mode_is_refused(self):
        with pytest.raises(ValidationError, match="mode"):
            invoke("vaspkit", "gui", 103)

    def test_remote_command_carries_the_script_intact(self):
        script = "echo 'a b'\nprintf \"%s\\n\" x\n"
        wrapped = remote_command(script)
        encoded = re.search(r"echo (\S+) \| base64 -d", wrapped).group(1)
        assert base64.b64decode(encoded).decode() == script
        assert wrapped.startswith('bash -lc "$(')


class TestDoctor:
    def test_script_only_writes_inside_mktemp(self):
        script = doctor_script()
        assert "mktemp -d" in script
        assert 'rm -rf -- "$t"' in script
        assert script.rstrip().endswith("echo __VP_VK_END__")

    def test_command_hint_is_tried_first(self):
        script = doctor_script("/apps/vk/bin/vaspkit")
        assert "for c in /apps/vk/bin/vaspkit vaspkit" in script

    def test_bad_hint_is_refused(self):
        with pytest.raises(ValidationError):
            doctor_script("vaspkit; id")

    def test_healthy_server_is_ready(self):
        profile = parse_doctor(probe_output())
        assert profile["ready"] is True
        assert profile["problems"] == []
        assert profile["command"] == "/opt/vaspkit/bin/vaspkit"
        assert profile["version"] == "VASPKIT 1.4.1"
        assert profile["potcar_paths"] == {"PBE": "/data/pot/PBE",
                                           "LDA": "/data/pot/LDA"}

    def test_stdin_mode_is_preferred_when_both_work(self):
        assert parse_doctor(probe_output())["mode"] == "stdin"

    def test_task_mode_is_used_when_only_it_works(self):
        profile = parse_doctor(probe_output(stdin="fail"))
        assert profile["mode"] == "task"
        assert profile["ready"] is True

    def test_no_executable_is_not_ready(self):
        profile = parse_doctor(probe_output(command=""))
        assert profile["ready"] is False
        assert any("no vaspkit executable" in p for p in profile["problems"])

    def test_missing_pbe_library_is_not_ready(self):
        profile = parse_doctor(probe_output(pbe=False))
        assert profile["ready"] is False
        assert any("PBE_PATH" in p for p in profile["problems"])

    def test_no_working_mode_is_not_ready(self):
        profile = parse_doctor(probe_output(stdin="fail", task="fail"))
        assert profile["mode"] == ""
        assert profile["ready"] is False

    def test_truncated_output_is_not_ready(self):
        profile = parse_doctor(probe_output(finished=False))
        assert profile["ready"] is False
        assert any("cut off" in p for p in profile["problems"])


class TestGeneration:
    def script(self, **over):
        args = {"stage": "static", "profile": READY, "stage_dir": STAGE_DIR,
                "poscar_src": STAGE_DIR.replace("02-static", "01-relax/CONTCAR"),
                "chgcar_src": "", "kspacing": 0.03, "kpoints_task": 102}
        args.update(over)
        return generation_script(**args)

    def test_profile_must_be_ready(self):
        with pytest.raises(ValidationError, match="vaspkit_doctor"):
            self.script(profile={**READY, "ready": False})

    def test_static_stage(self):
        script = self.script()
        assert script.startswith(f"cd -- {STAGE_DIR} || exit 2")
        assert "01-relax/CONTCAR POSCAR || exit 2" in script
        assert "printf '%s\\n' 101 ST | /opt/vaspkit/bin/vaspkit" in script
        assert "printf '%s\\n' 102 2 0.030 |" in script
        assert "303" not in script
        assert script.rstrip().endswith(f"echo {GEN_OK}")

    def test_potcar_is_generated_before_the_incar(self):
        script = self.script()
        assert script.index("103") < script.index("101 ST")

    def test_uploaded_poscar_is_not_copied(self):
        script = self.script(stage="relax", poscar_src="")
        assert "POSCAR ||" not in script
        assert "101 LR" in script

    def test_band_stage_uses_the_high_symmetry_path(self):
        script = self.script(stage="band", kpoints_task=303,
                             chgcar_src=STAGE_DIR + "/CHGCAR")
        assert "02-static/CHGCAR CHGCAR || exit 2" in script
        assert "printf '%s\\n' 303 | /opt/vaspkit/bin/vaspkit " \
            "> vaspkit-303.log" in script
        assert "cp -- KPATH.in KPOINTS || exit 2" in script
        assert "102" not in script

    def test_primitive_check_adds_303_without_replacing_kpoints(self):
        script = self.script(stage="relax", poscar_src="", check_primitive=True)
        assert "vaspkit-303.log" in script
        assert "KPATH.in KPOINTS" not in script
        assert script.index("303") < script.index("102")

    def test_missing_outputs_fail_the_script(self):
        assert '[ -s "$f" ] || { echo "__VP_GEN_MISSING__ $f"; exit 3; }' \
            in self.script()

    @pytest.mark.parametrize("field", ["stage_dir", "poscar_src", "chgcar_src"])
    def test_relative_or_traversing_paths_are_refused(self, field):
        with pytest.raises(ValidationError, match="absolute"):
            self.script(**{field: "../elsewhere"})


class TestProfileStore:
    def test_round_trip(self, tmp_path):
        store = ProfileStore(tmp_path / "vaspkit.json")
        assert store.get("cl9") == {}
        store.put("cl9", READY)
        store.put("pbs1", {"ready": False})
        assert store.get("cl9") == READY
        assert ProfileStore(tmp_path / "vaspkit.json").get("pbs1") == \
            {"ready": False}
