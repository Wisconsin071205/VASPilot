#!/usr/bin/env python3
"""Fake HPC server for gateway integration tests.

Invoked as:  python fake_hpc.py <server> <shell-command>

It emulates exactly the fixed command shapes the VASPilot gateway builds
(single-quoted POSIX arguments), operating on a REAL local directory that
stands in for the server filesystem. The mapping comes from the JSON config
file named by VASPILOT_FAKE_HPC_CONFIG:

    {"servers": {"cl9": {"root": "/hpc/.../root", "real": "C:/tmp/cl9fs",
                          "scheduler": "slurm"}},
     "stage_dir": "C:/tmp/stage", "next_job_id": 1000,
     "jobs": {"1001": "RUNNING"}}

Exit code 0 + stdout mimics the remote shell; exit 1 + stderr means failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

QUOTED = re.compile(r"'((?:[^']|'\\'')*)'")


def unquote(token: str) -> str:
    return token.replace("'\\''", "'")


def quoted_args(command: str) -> list[str]:
    return [unquote(m) for m in QUOTED.findall(command)]


def tokens(command: str) -> list[str]:
    """Shell-ish tokenizer: single-quoted segments (with '\\'' escapes) and
    bare whitespace-separated words, like the gateway actually emits."""
    out: list[str] = []
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == "'":
            j = i + 1
            buf: list[str] = []
            while j < n:
                if command.startswith("'\\''", j):
                    buf.append("'")
                    j += 4
                    continue
                if command[j] == "'":
                    break
                buf.append(command[j])
                j += 1
            out.append("".join(buf))
            i = j + 1
        elif ch.isspace():
            i += 1
        else:
            j = i
            while j < n and not command[j].isspace():
                j += 1
            out.append(command[i:j])
            i = j
    return out


def path_tokens(command: str) -> list[str]:
    """Absolute-path arguments in order (paths are bare or quoted; command
    words, flags and quoted format strings never start with '/')."""
    return [t for t in tokens(command) if t.startswith("/")]


class FakeHpc:
    def __init__(self, server: str) -> None:
        config_path = os.environ["VASPILOT_FAKE_HPC_CONFIG"]
        with open(config_path, "r", encoding="utf-8") as handle:
            self.config = json.load(handle)
        self.server = server
        entry = self.config["servers"][server]
        self.root = entry["root"].rstrip("/")
        self.real = Path(entry["real"])
        self.stage_dir = Path(self.config["stage_dir"])
        self.scheduler = entry.get("scheduler", "slurm")
        self.real.mkdir(parents=True, exist_ok=True)
        self.stage_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = Path(self.config["stage_dir"]) / "hpc-state.json"
        if self.state_path.is_file():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        else:
            self.state = {"next_job_id": self.config.get("next_job_id", 1000),
                          "jobs": self.config.get("jobs", {})}

    # -- path mapping ---------------------------------------------------------
    def to_real(self, posix_path: str) -> Path:
        if posix_path.startswith("/tmp/vaspilot-"):
            return self.stage_dir / posix_path[len("/tmp/"):]
        rel = posix_path[len(self.root):]
        return self.real / rel.lstrip("/")

    def to_posix(self, real_path: Path) -> str:
        try:
            rel = real_path.resolve().relative_to(self.stage_dir.resolve())
            return "/tmp/" + rel.as_posix()
        except ValueError:
            pass
        rel = real_path.resolve().relative_to(self.real.resolve())
        return self.root + ("/" + rel.as_posix() if str(rel) != "." else "")

    def _save(self) -> None:
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")

    # -- dispatch ---------------------------------------------------------------
    def execute(self, command: str) -> tuple[int, str]:
        paths = path_tokens(command)
        try:
            # metrics collector script (tagged sections, RackTop style)
            if "__VP_CPU1__" in command:
                return 0, (
                    "__VP_CPU1__\n"
                    "cpu  100 20 30 1000 10 0 0 0\n"
                    "__VP_CPU2__\n"
                    "cpu  200 40 60 1100 10 0 0 0\n"
                    "__VP_LOAD__\n"
                    "0.52 0.41 0.35 1/512 12345\n"
                    "__VP_NPROC__\n"
                    "32\n"
                    "__VP_MEM__\n"
                    "MemTotal:       16384000 kB\n"
                    "MemAvailable:    8192000 kB\n"
                    "SwapTotal:       2097152 kB\n"
                    "SwapFree:        2097152 kB\n"
                    "__VP_DF__\n"
                    "Filesystem     1024-blocks      Used Available Capacity "
                    "Mounted on\n"
                    "tmpfs            8192000        0   8192000       0% "
                    "/dev/shm\n"
                    "/dev/sda1      500000000 200000000 300000000     40% /\n"
                    "__VP_GPU__\n"
                    "0, GPU-fake-a100-0, NVIDIA A100, 5, 1200, 32510, "
                    "45, 70.5\n"
                    "__VP_GPUPROC__\n"
                    "GPU-fake-a100-0, 12345, python, 1200, wuhong\n"
                    "__VP_SCHED__\n"
                    "slurm\ncpu|up|4|160/64/0/224\n"
                    "__VP_HB__\n\n"
                    "__VP_DONE__\n")
            if "qstat -f" in command:
                return 0, (
                    "Job Id: 5001.admin\n"
                    "    Job_Name = relax_case1\n"
                    "    job_state = R\n"
                    "    queue = work\n"
                    "    resources_used.walltime = 00:12:34\n"
                    "    stime = Fri Aug 29 10:00:00 2026\n"
                    "Job Id: 5000.admin\n"
                    "    Job_Name = bader_run\n"
                    "    job_state = F\n"
                    "    queue = work\n"
                    "    resources_used.walltime = 01:02:03\n"
                    "    resources_used.exit_status = 0\n"
                    "    mtime = Fri Aug 29 11:02:03 2026\n")
            # arbitrary exec passthrough (audit-only remote shell)
            if command.startswith("echo "):
                return 0, command[5:].strip() + "\n"
            if "exit 3" in command:
                return 3, "intentional failure\n"
            if command == "echo $HOME":
                return 0, self.root + "\n"
            if command.startswith("realpath -m --"):
                lines = [str(PurePosixPath(p)) for p in paths[:2]]
                return 0, "\n".join(lines) + "\n"
            # compound shapes are matched BEFORE their plain prefixes
            if "payload" in command and "metadata.json" in command:
                return self._special(command, paths)
            if "EXISTS" in command and "RESTORED" in command:
                return self._special(command, paths)
            if "cp --" in command and "rm -f" in command:
                return self._special(command, paths)
            if "find" in command and "-printf '%y|" in command:
                return self._list_dir(paths[0])
            if command.startswith("size=$(wc -c"):
                return self._read_file(paths[0])
            if command.startswith("tail -n"):
                return self._tail(int(re.search(r"tail -n (\d+)", command).group(1)),
                                  paths[0])
            if "find" in command and "-name" in command:
                return self._find(command, paths)
            if command.startswith("stat -c '%F"):
                return self._stat(paths[0])
            if command.startswith("du -sb") or command.startswith("du -sk"):
                return self._du(paths[0])
            if command.startswith("du -sh"):
                return 0, "1.0K\t" + paths[0] + "\n"
            if command.startswith("mkdir -p --"):
                Path(self.to_real(paths[0])).mkdir(parents=True, exist_ok=True)
                return 0, ""
            if command.startswith("cp -a --"):
                shutil.copytree(self.to_real(paths[0]), self.to_real(paths[1]),
                                dirs_exist_ok=True) \
                    if self.to_real(paths[0]).is_dir() else \
                    shutil.copy2(self.to_real(paths[0]), self.to_real(paths[1]))
                return 0, ""
            if command.startswith("mv --"):
                source, dest = self.to_real(paths[0]), self.to_real(paths[1])
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(dest))
                return 0, ""
            if command.startswith("if [ -d"):
                return self._trash_list(paths[0] if paths else "")
            if "sha256=$(sha256sum" in command:
                data = self.to_real(paths[0]).read_bytes()
                return 0, hashlib.sha256(data).hexdigest() + "\n"
            if command.startswith("if [ -e") and "sha256sum" in command:
                target = self.to_real(paths[0])
                if target.exists():
                    return 0, hashlib.sha256(target.read_bytes()).hexdigest() + "\n"
                return 0, ""
            if command.startswith("test -f") or command.startswith("test -d") \
                    or command.startswith("test -e"):
                return self._test(command)
            if command.startswith("cat -- "):
                target = self.to_real(paths[0])
                if target.is_file():
                    return 0, target.read_text(encoding="utf-8",
                                               errors="replace")
                return 0, "MISSING\n"
            if command.startswith("cp --"):
                shutil.copy2(self.to_real(paths[0]), self.to_real(paths[1]))
                return 0, ""
            if command.startswith("sha256sum --"):
                return 0, hashlib.sha256(
                    self.to_real(paths[0]).read_bytes()).hexdigest() + "\n"
            if command.startswith("stat -c %s"):
                return 0, str(self.to_real(paths[0]).stat().st_size) + "\n"
            if command.startswith("rm -rf --"):
                target = self.to_real(paths[0])
                shutil.rmtree(target, ignore_errors=True)
                return 0, ""
            if "command -v qsub" in command:
                return 0, self.scheduler + "\n"
            if command.startswith("squeue -u"):
                rows = [f"{jid}|{state}|0:00|8:00:00|cpu|job|n1"
                        for jid, state in self.state["jobs"].items()
                        if state in ("RUNNING", "PENDING")]
                return 0, ("\n".join(rows) + "\n") if rows else ""
            if command.startswith("sacct -u"):
                rows = [f"{jid}|job{jid}|cpu|{state}|00:01:00|0:0"
                        for jid, state in self.state["jobs"].items()]
                return 0, ("\n".join(rows) + "\n") if rows else ""
            if "sbatch" in command:
                self.state["next_job_id"] += 1
                job_id = str(self.state["next_job_id"])
                self.state["jobs"][job_id] = "RUNNING"
                self._save()
                return 0, job_id + "\n"
            if "scancel" in command or "qdel" in command:
                match = re.search(r"(?:scancel|qdel) -- '?([^' ]+)'?", command)
                job_id = match.group(1)
                if job_id in self.state["jobs"]:
                    self.state["jobs"][job_id] = "CANCELLED"
                    self._save()
                return 0, ""
            if command.startswith("out=$(squeue"):
                job_id = re.search(r"-j '?([^' ]+)'?", command).group(1)
                state = self.state["jobs"].get(job_id, "COMPLETED")
                return 0, f"{job_id}|{state}\n"
            if "for f in" in command and "printf" in command:
                match = re.search(r"-f (\S+?)/\$f", command)
                directory = self.to_real(match.group(1) if match else paths[0])
                names = re.findall(r"\b(INCAR|KPOINTS|POSCAR|POTCAR|OSZICAR|"
                                   r"OUTCAR|CONTCAR|DOSCAR|EIGENVAL|vasprun.xml)\b",
                                   command)
                present = [n for n in dict.fromkeys(names)
                           if (directory / n).is_file()]
                return 0, " ".join(present) + (" \n" if present else "\n")
            if command.startswith("echo \"$(id -un)"):
                return 0, f"tester@fakehost:{self.root}\n"
            if "nproc" in command:
                return 0, "cores=8\nMemTotal: 1000 kB\nsystem fake\n"
            # diagnostics and everything else: canned harmless output
            return 0, "fake diagnostic output\n"
        except FileNotFoundError as exc:
            return 1, str(exc) + "\n"
        except OSError as exc:
            return 1, str(exc) + "\n"

    # -- compound handlers ---------------------------------------------------------
    def _special(self, command: str, paths: list[str]) -> tuple[int, str]:
        # remove: mkdir trash entry, mv path -> payload, write metadata
        # quoted order: [entry_dir, original_path, payload, metadata_json]
        if "payload" in command and "metadata.json" in command \
                and command.startswith("mkdir"):
            entry_dir = self.to_real(paths[0])
            entry_dir.mkdir(parents=True, exist_ok=True)
            payload = self.to_real(paths[2])
            payload.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(self.to_real(paths[1])), str(payload))
            (entry_dir / "metadata.json").write_text(quoted_args(command)[-1], encoding="utf-8")
            return 0, ""
        # restore
        if "EXISTS" in command and "RESTORED" in command:
            target = self.to_real(paths[0])
            if target.exists():
                return 0, "EXISTS\n"
            payload = self.to_real(paths[1])
            if payload.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(payload), str(target))
                (payload.parent / "metadata.json").write_text(
                    quoted_args(command)[-1], encoding="utf-8")
                return 0, "RESTORED\n"
            return 0, "MISSING_PAYLOAD\n"
        # upload finalize: mkdir parent && cp stage target && rm stage
        if "cp --" in command and "rm -f" in command:
            parent = self.to_real(paths[0])
            parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.to_real(paths[1]), self.to_real(paths[2]))
            stage = self.to_real(paths[3])
            if stage.is_file():
                stage.unlink()
            return 0, ""
        return 1, "unhandled compound command\n"

    def _test(self, command: str) -> tuple[int, str]:
        kind = "d" if " -d " in command else ("f" if " -f " in command else "e")
        target = self.to_real(path_tokens(command)[0])
        if kind == "d":
            return (0, "YES\n") if target.is_dir() else (0, "NO\n")
        if kind == "f":
            return (0, "YES\n") if target.is_file() else (0, "NO\n")
        return (0, "YES\n") if target.exists() else (0, "NO\n")

    def _list_dir(self, posix_path: str) -> tuple[int, str]:
        real = self.to_real(posix_path)
        if not real.is_dir():
            return 0, "NOTDIR\n"
        rows = []
        for child in sorted(real.iterdir()):
            kind = "d" if child.is_dir() else "f"
            size = 0 if child.is_dir() else child.stat().st_size
            rows.append(f"{kind}|{child.name}|{size}|2026-01-01T00:00:00")
        return 0, "\n".join(sorted(rows)) + ("\n" if rows else "")

    def _read_file(self, posix_path: str) -> tuple[int, str]:
        real = self.to_real(posix_path)
        if not real.is_file():
            return 1, f"cat: {posix_path}: No such file\n"
        data = real.read_bytes()
        cap = 2 * 1024 * 1024
        if len(data) > cap:
            return 0, data[-cap:].decode("utf-8", "replace")
        return 0, data.decode("utf-8", "replace")

    def _tail(self, lines: int, posix_path: str) -> tuple[int, str]:
        real = self.to_real(posix_path)
        if not real.is_file():
            return 1, f"tail: {posix_path}: No such file\n"
        content = real.read_text(encoding="utf-8", errors="replace")
        return 0, "\n".join(content.splitlines()[-lines:]) + "\n"

    def _find(self, command: str, paths: list[str]) -> tuple[int, str]:
        base = self.to_real(paths[0])
        depth_match = re.search(r"-maxdepth (\d+)", command)
        depth = int(depth_match.group(1)) if depth_match else 2
        pattern = re.search(r"-name (\S+)", command)
        import fnmatch
        pat = pattern.group(1).strip("'\"") if pattern else "*"
        rows = []
        for path in sorted(base.rglob("*")):
            rel_depth = len(path.relative_to(base).parts)
            if rel_depth > depth or not path.is_file():
                continue
            if fnmatch.fnmatch(path.name, pat):
                rows.append(f"{self.to_posix(path)}|{path.stat().st_size}")
        limit = int(re.search(r"head -n (\d+)", command).group(1)) \
            if "head -n" in command else 200
        return 0, "\n".join(rows[:limit]) + ("\n" if rows else "")

    def _stat(self, posix_path: str) -> tuple[int, str]:
        real = self.to_real(posix_path)
        if not real.exists():
            return 0, "MISSING\n"
        kind = "directory" if real.is_dir() else "regular file"
        return 0, f"{kind}|{real.stat().st_size if real.is_file() else 0}|" \
                  f"0|2026-01-01 00:00:00\n"

    def _du(self, posix_path: str) -> tuple[int, str]:
        real = self.to_real(posix_path)
        total = 0
        if real.is_file():
            total = real.stat().st_size
        else:
            for path in real.rglob("*"):
                if path.is_file():
                    total += path.stat().st_size
        return 0, f"{total}\t{posix_path}\n"

    def _trash_list(self, root: str) -> tuple[int, str]:
        trash_root = self.to_real(root or self.root + "/.vaspilot-trash")
        out = []
        if trash_root.is_dir():
            for meta in sorted(trash_root.glob("*/metadata.json")):
                out.append(meta.read_text(encoding="utf-8").strip() + "\n")
        return 0, "".join(out)


def main() -> int:
    server, command = sys.argv[1], sys.argv[2]
    # binary stage push/pull used by the gateway's data-motion helpers:
    # push takes the payload on stdin; pull emits "OK <base64>" on stdout
    if command.startswith("__VP_STAGE_PUSH__ "):
        hpc = FakeHpc(server)
        dest = hpc.to_real(path_tokens(command)[0])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(sys.stdin.buffer.read())
        return 0
    if command.startswith("__VP_STAGE_PULL__ "):
        hpc = FakeHpc(server)
        src = hpc.to_real(path_tokens(command)[0])
        if not src.is_file():
            sys.stderr.write(f"cat: {src}: No such file\n")
            return 1
        sys.stdout.buffer.write(b"OK " + __import__("base64").b64encode(
            src.read_bytes()))
        return 0
    code, out = FakeHpc(server).execute(command)
    sys.stdout.write(out)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
