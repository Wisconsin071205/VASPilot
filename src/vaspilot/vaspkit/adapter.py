"""The only module that knows what a VASPKIT command line looks like.

None of this has been checked against a real installation yet. That is what
the probe is for: ``doctor_script`` runs on the server, ``parse_doctor``
turns its output into a profile, and no campaign may start on a server whose
profile is not ``ready``. Everything the probe cannot settle -- the 101
template codes, the 102 menu answers -- lives in the tables below, so a
correction after the first real run touches one place.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from pathlib import Path
from typing import Any

from ..core.errors import ValidationError

MODES = ("stdin", "task")

# Task 101 template per stage. VASPKIT's 101 menu has no band or DOS entry,
# so those stages start from the static template and get their settings
# written on top (workflow/campaign.py).
INCAR_TEMPLATE = {"relax": "LR", "static": "ST", "band": "ST", "dos": "ST"}
KPOINTS_GAMMA = "2"  # 102 menu: 1 = Monkhorst-Pack, 2 = Gamma-centred

GEN_OK = "__VP_GEN_OK__"
GEN_MISSING = "__VP_GEN_MISSING__"

_COMMAND_RE = re.compile(r"^[A-Za-z0-9_./+~-]{1,200}$")
_SECTION_RE = re.compile(r"^__VP_VK_([A-Z]+)__$")


def valid_command(command: str) -> str:
    """A VASPKIT executable: a bare name or a path, nothing a shell expands."""
    command = str(command or "").strip()
    if not _COMMAND_RE.fullmatch(command):
        raise ValidationError(
            f"vaspkit command {command!r} must be a plain executable name or path")
    return command


def invoke(command: str, mode: str, task: int,
           answers: tuple[str, ...] = ()) -> str:
    """One VASPKIT call as a shell fragment.

    ``stdin`` feeds the task number through the top-level menu, the way the
    program is used interactively; ``task`` names it with ``-task`` and feeds
    only the follow-up prompts.
    """
    command = valid_command(command)
    if mode == "stdin":
        feed = (str(task),) + tuple(answers)
        tail = command
    elif mode == "task":
        feed = tuple(answers)
        tail = f"{command} -task {int(task)}"
    else:
        raise ValidationError(f"vaspkit mode must be one of {MODES}")
    if not feed:
        return f"{tail} </dev/null"
    return "printf '%s\\n' " + " ".join(shlex.quote(item) for item in feed) \
        + f" | {tail}"


def remote_command(script: str) -> str:
    """Ship a multi-line script as one shell word.

    The gateway hands its command to the remote login shell as a single
    argument; base64 keeps newlines and quotes intact on the way, and
    ``bash -lc`` loads the login environment where ``module``-installed
    VASPKIT usually lives. The script keeps its own stdin free: every VASPKIT
    call has an explicit pipe or ``</dev/null``.
    """
    import base64
    encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return f'bash -lc "$(echo {encoded} | base64 -d)"'


# --------------------------------------------------------------------- probe
_PROBE_POSCAR = ("Si\n1.0\n0 2.715 2.715\n2.715 0 2.715\n2.715 2.715 0\n"
                 "Si\n2\nDirect\n0 0 0\n0.25 0.25 0.25\n")


def doctor_script(command_hint: str = "") -> str:
    """A read-mostly probe: it only writes inside its own mktemp directory."""
    hint = shlex.quote(valid_command(command_hint)) + " " if command_hint else ""
    candidates = hint + 'vaspkit "$HOME/vaspkit/bin/vaspkit" /opt/vaspkit/bin/vaspkit'
    poscar = shlex.quote(_PROBE_POSCAR)
    return "\n".join([
        "vk=''",
        f"for c in {candidates}; do",
        '  p=$(command -v "$c" 2>/dev/null) && { vk="$p"; break; }',
        "done",
        "echo __VP_VK_CMD__",
        'echo "$vk"',
        "echo __VP_VK_VERSION__",
        '[ -n "$vk" ] && timeout 20 "$vk" -v </dev/null 2>&1 | head -5',
        "echo __VP_VK_POT__",
        "for key in PBE_PATH GGA_PATH LDA_PATH; do",
        '  v=$(sed -n "s/^[[:space:]]*$key[[:space:]]*=[[:space:]]*'
        '\\([^[:space:]#]*\\).*/\\1/p" "$HOME/.vaspkit" 2>/dev/null | head -1)',
        '  v="${v/#\\~/$HOME}"',
        '  if [ -n "$v" ] && [ -d "$v" ]; then',
        '    n=$(ls -- "$v" | wc -l)',
        '    [ -d "$v/Si" ] && si=yes || si=no',
        '    echo "$key|$v|dir|$n|$si"',
        "  else",
        '    echo "$key|$v|missing|0|no"',
        "  fi",
        "done",
        "echo __VP_VK_TRY__",
        'if [ -n "$vk" ]; then',
        "  for mode in stdin task; do",
        "    t=$(mktemp -d)",
        f"    printf '%s' {poscar} > \"$t/POSCAR\"",
        '    if [ "$mode" = stdin ]; then',
        "      (cd \"$t\" && printf '%s\\n' 103 | timeout 60 \"$vk\" >log 2>&1)",
        "    else",
        '      (cd "$t" && timeout 60 "$vk" -task 103 </dev/null >log 2>&1)',
        "    fi",
        '    if [ -s "$t/POTCAR" ]; then echo "$mode|ok"; '
        'else echo "$mode|fail"; fi',
        '    rm -rf -- "$t"',
        "  done",
        "fi",
        "echo __VP_VK_END__",
        "",
    ])


def parse_doctor(stdout: str) -> dict[str, Any]:
    """Turn the probe's tagged output into a server profile."""
    sections: dict[str, list[str]] = {}
    current = ""
    for line in str(stdout or "").splitlines():
        match = _SECTION_RE.match(line.strip())
        if match:
            current = match.group(1)
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line.rstrip())

    command = next((line.strip() for line in sections.get("CMD", [])
                    if line.strip()), "")
    version = " ".join(line.strip() for line in sections.get("VERSION", [])
                       if line.strip())[:200]

    potcar_paths: dict[str, str] = {}
    potcar_checks: list[dict[str, Any]] = []
    for line in sections.get("POT", []):
        parts = line.split("|")
        if len(parts) != 5:
            continue
        key, path, kind, count, si = parts
        label = key.replace("_PATH", "")
        entry = {"key": key, "path": path, "exists": kind == "dir",
                 "entries": int(count) if count.isdigit() else 0,
                 "has_si": si == "yes"}
        potcar_checks.append(entry)
        if entry["exists"]:
            potcar_paths[label] = path

    modes: dict[str, bool] = {}
    for line in sections.get("TRY", []):
        name, _, outcome = line.partition("|")
        if name in MODES:
            modes[name] = outcome.strip() == "ok"
    mode = next((name for name in MODES if modes.get(name)), "")

    problems: list[str] = []
    if "END" not in sections:
        problems.append("the probe output was cut off before it finished")
    if not command:
        problems.append("no vaspkit executable was found on PATH or in the "
                        "usual install prefixes")
    if "PBE" not in potcar_paths:
        problems.append("~/.vaspkit has no readable PBE_PATH, so task 103 "
                        "cannot assemble a POTCAR")
    if command and not mode:
        problems.append("task 103 produced no POTCAR in either calling mode; "
                        "read the probe output before trusting this server")
    return {"command": command, "version": version,
            "potcar_paths": potcar_paths, "potcar_checks": potcar_checks,
            "modes": modes, "mode": mode,
            "ready": not problems, "problems": problems}


# ---------------------------------------------------------------- generation
def generation_script(*, stage: str, profile: dict[str, Any], stage_dir: str,
                      poscar_src: str, chgcar_src: str, kspacing: float,
                      kpoints_task: int, check_primitive: bool = False) -> str:
    """Shell that turns one empty stage directory into VASP inputs.

    ``poscar_src`` empty means POSCAR was already uploaded. The script ends
    with ``GEN_OK`` only when INCAR, KPOINTS and POTCAR all exist and are
    non-empty; the VASPKIT logs stay in the directory for a human to read.
    """
    if stage not in INCAR_TEMPLATE:
        raise ValidationError(f"unknown stage {stage!r}")
    if not profile.get("ready"):
        raise ValidationError("this server's VASPKIT profile is not ready; "
                              "run vaspkit_doctor first")
    command = valid_command(str(profile.get("command", "")))
    mode = str(profile.get("mode", ""))
    for path in (stage_dir, poscar_src, chgcar_src):
        if path and (not path.startswith("/") or ".." in path.split("/")):
            raise ValidationError(f"{path!r} is not an absolute clean path")
    if kpoints_task not in (102, 303):
        raise ValidationError("kpoints_task must be 102 or 303")

    lines = [f"cd -- {shlex.quote(stage_dir)} || exit 2"]
    if poscar_src:
        lines.append(f"cp -- {shlex.quote(poscar_src)} POSCAR || exit 2")
    if chgcar_src:
        lines.append(f"cp -- {shlex.quote(chgcar_src)} CHGCAR || exit 2")
    lines.append(invoke(command, mode, 103) + " > vaspkit-103.log 2>&1")
    lines.append(invoke(command, mode, 101, (INCAR_TEMPLATE[stage],))
                 + " > vaspkit-101.log 2>&1")
    if kpoints_task == 303 or check_primitive:
        # 303 writes the high-symmetry path (KPATH.in) and the standard
        # primitive cell it assumes (PRIMCELL.vasp)
        lines.append(invoke(command, mode, 303) + " > vaspkit-303.log 2>&1")
    if kpoints_task == 303:
        lines.append("cp -- KPATH.in KPOINTS || exit 2")
    else:
        lines.append(invoke(command, mode, 102,
                            (KPOINTS_GAMMA, f"{float(kspacing):.3f}"))
                     + " > vaspkit-102.log 2>&1")
    lines += [
        "for f in INCAR KPOINTS POTCAR; do",
        f'  [ -s "$f" ] || {{ echo "{GEN_MISSING} $f"; exit 3; }}',
        "done",
        f"echo {GEN_OK}",
        "",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ profiles
class ProfileStore:
    """Per-server probe results, kept apart from the gateway's server mirror
    so a catalog refresh never wipes them."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, server: str) -> dict[str, Any]:
        profile = self._load().get(server)
        return profile if isinstance(profile, dict) else {}

    def put(self, server: str, profile: dict[str, Any]) -> None:
        data = self._load()
        data[server] = profile
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".vaspkit.", dir=str(self.path.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, self.path)
