#!/usr/bin/env python3
"""VASPilot gateway — restricted operations broker deployed on the Vlab host.

Invoked over SSH from the local CLI:

    vaspilot-gateway <operation> [flags...]

Every operation prints exactly one JSON document on stdout:

    {"ok": true, ...}          success
    {"ok": false, "error": {"code": "...", "message": "..."}}

Design invariants:
  - no operation accepts an arbitrary shell string from the caller; each maps
    to a fixed command shape whose identifiers are regex-validated first
  - every remote path stays inside the server's configured remote_root,
    checked lexically AND via ``realpath`` (symlink escapes fail closed)
  - removals go to a structured trash area; purge requires a double-match
  - host-key changes never auto-heal: StrictHostKeyChecking stays strict
  - secrets (passwords, TOTP) are only ever typed by the human in the
    interactive ``connect`` terminal; this script never sees or stores them
  - an append-only audit trail records every operation

Standalone by design: Python 3 stdlib only, no package imports, so a single
scp installs it on the gateway host.

A test seam exists for offline integration tests: when VASPILOT_FAKE_HPC
points to an executable helper, ``ssh`` calls are replaced by
``<helper> <server> <shell-command>`` and its stdout is used as the remote
reply. It is ignored in normal operation.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

GATEWAY_VERSION = "1.3.3"
PROTOCOL_VERSION = "2"

HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("VASPILOT_GATEWAY_CONFIG", HOME / ".config/vaspilot"))
CACHE_DIR = Path(os.environ.get("VASPILOT_GATEWAY_CACHE", HOME / ".cache/vaspilot"))
CONFIG_FILE = CONFIG_DIR / "servers.json"
AUDIT_FILE = CACHE_DIR / "gateway-audit.jsonl"
TRASH_DIR_NAME = ".vaspilot-trash"

SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}@[A-Za-z0-9][A-Za-z0-9._-]{0,253}$")
SAFE_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9._/+@=-]{1,259}$")
SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
SAFE_JOB_SCRIPT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
SAFE_JOB_ID = re.compile(r"^[0-9]{1,19}([.][A-Za-z0-9._-]{0,63})?$")
SAFE_TRASH_ID = re.compile(r"^[0-9a-zA-Z_][0-9a-zA-Z_-]{0,39}$")
SAFE_GLOB = re.compile(r"^[A-Za-z0-9*?][A-Za-z0-9*?._+-]{0,127}$")
SAFE_PERSIST = re.compile(r"^(yes|no|[0-9]+[smhdw])$", re.IGNORECASE)
STAGE_RE = re.compile(r"^/tmp/vaspilot-[0-9a-f]{8,32}$")
SCHEDULERS = ("slurm", "pbs")
TEXT_DENYLIST = {"POTCAR", "WAVECAR", "CHGCAR", "CHG", "LOCPOT", "PROCAR",
                 "PARCHG", "AECCAR0", "AECCAR1", "AECCAR2", "ELFCAR"}
MAX_READ_BYTES = 32 * 1024 * 1024

CFG: dict = {}


# ---------------------------------------------------------------- utilities
def out(payload: dict) -> int:
    """Emit one success document; ``fail()`` is the only failure path."""
    payload.setdefault("ok", True)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 0


def fail(code: str, message: str, **fields: str) -> int:
    error = {"code": code, "message": message[:500]}
    for key in ("expected", "got"):
        if key in fields:
            error[key] = str(fields[key])
    return out({"ok": False, "error": error})


def audit(operation: str, outcome: str, detail: str = "", server: str = "") -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "operation": operation[:64],
            "outcome": outcome[:32],
            "server": server[:32],
            "detail": detail[:300],
        }
        with open(AUDIT_FILE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass  # auditing must never break an operation


# ------------------------------------------------------------------ catalog
def validate_server(name: str, entry: dict) -> None:
    if not SERVER_NAME_RE.fullmatch(name or ""):
        raise ValueError(f"server name {name!r} is invalid")
    if not TARGET_RE.fullmatch(entry.get("target", "")):
        raise ValueError(f"server target {entry.get('target')!r} must be user@host")
    port = entry.get("port", 22)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("server port must be 1..65535")
    root = entry.get("remote_root", "") or ""
    if root and not SAFE_REMOTE_PATH.fullmatch(root):
        raise ValueError(f"server remote_root {root!r} is invalid")
    if root and (".." in root.split("/") or any(p == "." for p in root.split("/"))):
        raise ValueError("server remote_root must not traverse")
    persist = entry.get("persist", "") or "8h"
    if not SAFE_PERSIST.fullmatch(str(persist)):
        raise ValueError("persist must be yes/no or like 8h, 30m, 1d")
    scheduler = entry.get("scheduler", "auto")
    if scheduler not in ("auto",) + SCHEDULERS:
        raise ValueError("scheduler must be auto, slurm or pbs")
    auth_mode = entry.get("auth_mode", "interactive")
    if auth_mode not in ("interactive", "key"):
        raise ValueError("auth_mode must be interactive or key")
    if not isinstance(entry.get("auto_connect", False), bool):
        raise ValueError("auto_connect must be a boolean")


def load_config() -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        data = {"servers": {}, "default_server": ""}
    except (OSError, ValueError):
        raise ValueError("gateway catalog is unreadable; refusing to continue")
    if not isinstance(data, dict) or not isinstance(data.get("servers", {}), dict):
        raise ValueError("gateway catalog is malformed")
    for name, entry in data.get("servers", {}).items():
        validate_server(name, entry)
    return data


def save_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".servers-", dir=str(CONFIG_DIR))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_FILE)


CFG = load_config()


def resolve_server(name: str | None) -> tuple:
    chosen = name or CFG.get("default_server", "")
    entry = CFG.get("servers", {}).get(chosen)
    if entry is None:
        raise ValueError(f"unknown server: {chosen}")
    return chosen, entry


def socket_path(name: str) -> Path:
    return CACHE_DIR / f"ctl-{name}.sock"


# ---------------------------------------------------------------- transport
def _remote_runner(server: str, target: str, command: str, sock: Path,
                   timeout: int = 180) -> subprocess.CompletedProcess:
    fake = os.environ.get("VASPILOT_FAKE_HPC", "")
    if fake:
        proc = subprocess.run([sys.executable, fake, server, command],
                              capture_output=True, text=True, timeout=timeout)
        return subprocess.CompletedProcess(command, proc.returncode, proc.stdout, proc.stderr)
    import subprocess as _sp
    return _sp.run(
        ["ssh", "-p", str(CFG["servers"][server]["port"]),
         "-o", "BatchMode=yes",
         "-o", "StrictHostKeyChecking=yes",
         "-o", "UpdateHostKeys=no",
         "-o", f"ControlPath={sock}",
         target, command],
        stdin=subprocess.DEVNULL, text=True, capture_output=True,
        timeout=timeout)


def _ssh_runner_raw(server: str, argv_cmd: list[str], data: bytes | None = None,
                    timeout: int = 300) -> tuple[int, bytes]:
    """Binary-safe one-shot ssh through the mux (no shell on our side).

    Used by the stage push/pull helpers; ``data`` is piped verbatim to the
    remote command's stdin when given. Falls back to the VASPILOT_FAKE_HPC
    helper so offline tests exercise the same code paths.
    """
    fake = os.environ.get("VASPILOT_FAKE_HPC", "")
    if fake:
        proc = subprocess.run(
            [sys.executable, fake, server] + argv_cmd,
            input=data if data is not None else b"",
            capture_output=True, timeout=timeout)
        return proc.returncode, proc.stdout + (proc.stderr or b"")
    entry = CFG["servers"][server]
    import subprocess as _sp
    cmd = ["ssh", "-p", str(entry["port"]),
           "-o", "BatchMode=yes",
           "-o", "StrictHostKeyChecking=yes",
           "-o", "UpdateHostKeys=no",
           "-o", f"ControlPath={socket_path(server)}",
           entry["target"]] + argv_cmd
    proc = _sp.run(cmd, input=data, capture_output=True, timeout=timeout)
    return proc.returncode, proc.stdout + (proc.stderr or b"")


STAGE_PUSH_MARKER = "__VP_STAGE_PUSH__"
STAGE_PULL_MARKER = "__VP_STAGE_PULL__"


def stage_push(name: str, stage_path_local_to_gateway: str,
               dest_resolved: str) -> None:
    """Move the gateway-local staged file onto the HPC target path."""
    with open(_stage_fs_path(stage_path_local_to_gateway), "rb") as handle:
        payload = handle.read()
    if os.environ.get("VASPILOT_FAKE_HPC"):
        argv = [f"{STAGE_PUSH_MARKER} {shlex.quote(dest_resolved)}"]
    else:
        # a real HPC just needs a stdin-consuming writer through the mux
        argv = [f"cat > {shlex.quote(dest_resolved)}"]
    rc, out_bytes = _ssh_runner_raw(name, argv, data=payload)
    if rc != 0:
        stderr = out_bytes.decode("utf-8", "replace")
        raise GatewayError("stage_push_failed",
                           f"could not place the staged file: {stderr[:200]}")


def stage_pull(name: str, src_resolved: str,
               stage_path_local_to_gateway: str) -> None:
    """Copy a file from the HPC into a gateway-local staged file."""
    if os.environ.get("VASPILOT_FAKE_HPC"):
        argv = [f"{STAGE_PULL_MARKER} {shlex.quote(src_resolved)}"]
        rc, out_bytes = _ssh_runner_raw(name, argv)
        marker_line = b"OK "
        if rc != 0 or not out_bytes.startswith(marker_line):
            raise GatewayError("stage_pull_failed",
                               "could not fetch the remote file: "
                               + out_bytes.decode("utf-8", "replace")[:200])
        decoded = base64.b64decode(out_bytes[len(marker_line):].strip())
    else:
        rc, out_bytes = _ssh_runner_raw(
            name, [f"cat {shlex.quote(src_resolved)}"])
        if rc != 0:
            raise GatewayError("stage_pull_failed",
                               "could not fetch the remote file: "
                               + out_bytes.decode("utf-8", "replace")[:200])
        decoded = out_bytes
    local = _stage_fs_path(stage_path_local_to_gateway)
    tmp = str(local) + ".part"
    with open(tmp, "wb") as handle:
        handle.write(decoded)
    os.replace(tmp, local)


def vlab_stage_sha256(stage: str) -> str:
    """SHA-256 of a gateway-local staged file, read directly (binary)."""
    digest = hashlib.sha256()
    with open(_stage_fs_path(stage), "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_fs_path(stage: str) -> Path:
    """Filesystem path of a gateway-local stage id.

    Normal operation: the POSIX path as-is (the gateway host IS where the
    CLI scp'd the file). Test seam: VASPILOT_GATEWAY_STAGE_DIR redirects
    lookups into a fixture directory by basename.
    """
    override = os.environ.get("VASPILOT_GATEWAY_STAGE_DIR", "").strip()
    if override:
        return Path(override) / PurePosixPath(stage).name
    return Path(stage)


def connected(name: str, timeout: int = 4) -> bool:
    sock = socket_path(name)
    if os.environ.get("VASPILOT_FAKE_HPC"):
        # offline test seam: the marker file stands in for the ControlMaster
        return sock.exists() and sock.read_text(encoding="utf-8").strip() == "up"
    if not sock.exists():
        return False
    entry = CFG["servers"][name]
    try:
        result = subprocess.run(
            ["ssh", "-p", str(entry["port"]), "-o", "BatchMode=yes",
             "-o", f"ControlPath={sock}", "-O", "check", entry["target"]],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=timeout)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def require_connection(name: str, *, manual: bool = False) -> None:
    """Ensure a usable session, auto-reconnecting key-mode servers first.

    interactive servers behave exactly as before: no session -> the caller
    reports ``auth_required`` and a human types the password/TOTP. key-mode
    servers transparently rebuild their ControlMaster with the per-server
    key (subject to the reconnect backoff unless ``manual``).
    """
    if connected(name):
        record_reconnect(name, ok=True)
        return
    if ensure_session(name, manual=manual):
        return
    entry = CFG["servers"][name]
    if entry.get("auth_mode") == "key":
        state = reconnect_state(name)
        raise GatewayError(
            "disconnected",
            f"{name} key session unavailable "
            f"(state={state.get('error_code') or 'retrying'}, "
            f"retry_in={max(0, int(state.get('next_attempt', 0) - time.time()))}s)")
    raise GatewayError("disconnected",
                       f"{name} has no reusable SSH session; run "
                       f"'vaspilot server connect {name}' in a terminal")


# ------------------------------------------- per-server keys + auto reconnect
KEY_DIR = Path.home() / ".ssh" / "vaspilot"
BACKOFF_LADDER = (30, 60, 120, 300)     # seconds, capped
RECONNECT_FILE = CACHE_DIR / "reconnect-state.json"


def _key_paths(name: str) -> tuple[Path, Path]:
    if not SERVER_NAME_RE.fullmatch(name or ""):
        raise GatewayError("invalid_name", "server name is invalid")
    return KEY_DIR / name, KEY_DIR / f"{name}.pub"


def _load_reconnect() -> dict:
    try:
        data = json.loads(RECONNECT_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_reconnect(state: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".reconnect-", dir=str(CACHE_DIR))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, RECONNECT_FILE)


def reconnect_state(name: str) -> dict:
    """Read-only view for status/monitor surfaces (no secrets)."""
    return {k: v for k, v in _load_reconnect().get(name, {}).items()
            if k in ("last_attempt", "next_attempt", "failure_count",
                     "error_code", "error")}


def record_reconnect(name: str, *, ok: bool, error_code: str = "",
                     error: str = "") -> None:
    state = _load_reconnect()
    entry = state.get(name, {})
    if ok:
        if entry.get("failure_count") or entry.get("error_code"):
            entry = {"failure_count": 0, "last_attempt": int(time.time())}
        elif entry:
            return                # already clean; avoid file churn
        else:
            entry = {"failure_count": 0, "last_attempt": int(time.time())}
    else:
        count = int(entry.get("failure_count", 0)) + 1
        gap = BACKOFF_LADDER[min(count - 1, len(BACKOFF_LADDER) - 1)]
        now = int(time.time())
        entry = {"failure_count": count,
                 "last_attempt": now,
                 "next_attempt": now + gap,
                 "error_code": (error_code or "failed")[:40],
                 "error": error[:120]}
    state[name] = entry
    _save_reconnect(state)


def key_connect(name: str, *, manual: bool = False) -> tuple[bool, str]:
    """Rebuild the ControlMaster with the per-server key (BatchMode)."""
    entry = CFG["servers"][name]
    priv, _pub = _key_paths(name)
    if not priv.is_file():
        return False, "key_missing"
    sock = socket_path(name)
    if sock.exists():
        sock.unlink()                 # a stale socket is safe to remove
    if os.environ.get("VASPILOT_FAKE_HPC"):
        import json as _json
        cfg_path = os.environ.get("VASPILOT_FAKE_HPC_CONFIG", "")
        try:
            fake = _json.loads(
                Path(cfg_path).read_text(encoding="utf-8")) if cfg_path else {}
        except (OSError, ValueError):
            fake = {}
        srv = fake.get("servers", {}).get(name, {})
        if srv.get("host_key_fail"):
            record_reconnect(name, ok=False, error_code="host_key_failed",
                             error="simulated host key change")
            return False, "host_key_failed"
        if srv.get("reject_key"):
            record_reconnect(name, ok=False, error_code="key_rejected",
                             error="simulated public key rejection")
            return False, "key_rejected"
        if not srv.get("key_installed"):
            record_reconnect(name, ok=False, error_code="key_rejected",
                             error="public key not installed yet")
            return False, "key_rejected"
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        sock.write_text("up\n", encoding="utf-8")
        record_reconnect(name, ok=True)
        return True, ""
    command = [
        "ssh", "-M", "-S", str(sock),
        "-o", "ControlMaster=yes",
        "-o", "ControlPersist=yes",          # unattended survival across days
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "UpdateHostKeys=no",
        "-o", "ConnectTimeout=15",
        "-i", str(priv),
        "-p", str(entry.get("port", 22)), "-fN", entry["target"],
    ]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired):
        record_reconnect(name, ok=False, error_code="network_unreachable",
                         error="ssh invocation failed")
        return False, "network_unreachable"
    low = (result.stderr or "").lower()
    if result.returncode == 0 and connected(name):
        record_reconnect(name, ok=True)
        return True, ""
    if "host key" in low:
        record_reconnect(name, ok=False, error_code="host_key_failed",
                         error=(result.stderr or "").strip()[:120])
        return False, "host_key_failed"
    if "permission denied" in low or "publickey" in low or \
            "authentication failed" in low:
        record_reconnect(name, ok=False, error_code="key_rejected",
                         error=(result.stderr or "").strip()[:120])
        return False, "key_rejected"
    record_reconnect(name, ok=False, error_code="network_unreachable",
                     error=(result.stderr or "").strip()[:120])
    return False, "network_unreachable"


def ensure_session(name: str, *, manual: bool = False) -> bool:
    """Auto-reconnect a key-mode server; never touches interactive servers."""
    entry = CFG["servers"].get(name, {})
    if entry.get("auth_mode") != "key" or not entry.get("auto_connect"):
        return False
    if manual or not RECONNECT_FILE.exists():
        ok, _err = key_connect(name, manual=manual)
        return ok
    state = _load_reconnect().get(name, {})
    if state.get("error_code") == "host_key_failed" and not manual:
        return False                    # fingerprint changes never auto-retry
    now = time.time()
    if now < float(state.get("next_attempt", 0)):
        return False                    # waiting out the backoff window
    ok, _err = key_connect(name)
    return ok


class GatewayError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def remote(name: str, command: str) -> str:
    """Run one shell command on the HPC server over the mux; return stdout."""
    require_connection(name)
    entry = CFG["servers"][name]
    result = _remote_runner(name, entry["target"], command, socket_path(name))
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "Host key verification failed" in stderr:
            raise GatewayError("host_key_failed",
                               "host key verification failed on the HPC server; "
                               "refusing to continue (never auto-accept)")
        raise GatewayError("remote_command_failed",
                           f"remote command failed (rc={result.returncode}): "
                           f"{stderr[:300]}")
    return result.stdout or ""


# -------------------------------------------------------------- path rules
def effective_root(name: str, entry: dict) -> PurePosixPath:
    root = entry.get("remote_root") or ""
    if root:
        return PurePosixPath(root)
    # fall back to the login home, probed over the live connection and cached
    cache = CACHE_DIR / f"home-{name}"
    if cache.is_file():
        home = cache.read_text(encoding="utf-8").strip()
        if home.startswith("/"):
            return PurePosixPath(home)
    home = remote(name, "echo $HOME").strip()
    if not home.startswith("/"):
        raise GatewayError("root_unknown",
                           f"cannot determine the home directory of {name}")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(home + "\n", encoding="utf-8")
    return PurePosixPath(home)


def validated_remote_path(raw: str, entry: dict, name: str) -> str:
    if not isinstance(raw, str) or not SAFE_REMOTE_PATH.fullmatch(raw or ""):
        raise ValueError("remote path contains unsupported characters")
    path = PurePosixPath(raw)
    if ".." in path.parts or "." in path.parts:
        raise ValueError("remote path cannot contain traversal segments")
    root = effective_root(name, entry)
    if path != root and root not in path.parents:
        raise ValueError(f"remote path must remain under {root}")
    return str(path)


def require_resolved_within_root(name: str, path: str, entry: dict) -> str:
    """Reject symlink escapes: resolve with realpath before acting."""
    root = str(effective_root(name, entry))
    command = (f"realpath -m -- {shlex.quote(path)} && "
               f"realpath -m -- {shlex.quote(root)}")
    lines = [line.strip() for line in remote(name, command).splitlines()
             if line.strip()]
    if len(lines) != 2:
        raise GatewayError("resolve_failed", "could not resolve the remote path")
    resolved, resolved_root = lines
    rp, rr = PurePosixPath(resolved), PurePosixPath(resolved_root)
    if rp != rr and rr not in rp.parents:
        raise ValueError(f"remote path resolves outside {rr}")
    return resolved


def require_not_root(name: str, path: str, entry: dict) -> None:
    if PurePosixPath(path) == effective_root(name, entry):
        raise ValueError("refusing to operate on the server root itself")


def validated_stage_path(raw: str) -> str:
    """Validate the raw POSIX stage path (no Path() conversion: on Windows
    that would rewrite /tmp/... into backslash form and break the check)."""
    if not isinstance(raw, str) or not STAGE_RE.fullmatch(raw):
        raise ValueError("staging path must be /tmp/vaspilot-<hex> created by the CLI")
    return raw


def q(value: str) -> str:
    return shlex.quote(str(value))


# ---------------------------------------------------------------- scheduler
def scheduler_for(name: str, entry: dict) -> str:
    pinned = entry.get("scheduler", "auto")
    if pinned in SCHEDULERS:
        return pinned
    cache = CACHE_DIR / f"sched-{name}"
    if cache.is_file():
        cached = cache.read_text(encoding="utf-8").strip()
        if cached in SCHEDULERS:
            return cached
    detected = remote(
        name,
        "if command -v qsub >/dev/null 2>&1; then echo pbs; "
        "elif command -v sbatch >/dev/null 2>&1; then echo slurm; "
        "else echo unknown; fi").strip()
    if detected not in SCHEDULERS:
        raise GatewayError("scheduler_unknown",
                           f"cannot detect the scheduler on {name}")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(detected + "\n", encoding="utf-8")
    return detected


def normalize_state(raw: str) -> str:
    code = (raw or "").strip().upper()
    for failed in ("FAILED", "TIMEOUT", "CANCELLED", "NODE_FAIL",
                   "OUT_OF_MEMORY", "PREEMPTED", "BOOT_FAIL"):
        if code.startswith(failed[:4]):
            return failed
    if code.startswith("R"):
        return "RUNNING"
    if code.startswith("PD") or code.startswith("P") or code == "Q":
        return "PENDING"
    if code == "E":
        return "EXITING"
    if code == "H":
        return "HELD"
    if code == "S":
        return "SUSPENDED"
    if code.startswith("C") or code.startswith("F"):
        return "COMPLETED"
    return code or "UNKNOWN"


def _pbs_parse_qstat_f(raw: str) -> dict[str, dict]:
    """Parse classic `qstat -f` stanzas (PBS Pro & Torque) into
    {job_id: {name, state, elapsed, partition, nodes, completed_at,
    exit_status, limit}} — the only PBS format that reliably exposes
    walltime and the end timestamp for FINISHED jobs."""
    import time as _t

    jobs: dict[str, dict] = {}
    current: dict | None = None
    last_key: str | None = None
    for raw_line in (raw or "").splitlines():
        line = raw_line.rstrip()
        if line.startswith("Job Id:"):
            job_id = line.split(":", 1)[1].strip().split(".")[0]
            current = {"job_id": job_id}
            jobs[job_id] = current
            last_key = None
            continue
        if current is None:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if line.startswith(("    ", "\t")) and " = " in stripped \
                and not stripped.startswith(" " * 6 + "&&"):
            key, _, value = stripped.partition(" = ")
            key = key.strip().split(".")[-1]        # strip resources_used.
            current[key] = value.strip()
            last_key = key
        elif last_key and stripped and not stripped.startswith(("+++", "===")):
            current[last_key] = (current.get(last_key, "") + " "
                                 + stripped).strip()
    for job in jobs.values():
        state = normalize_state(job.pop("job_state", "UNKNOWN"))
        if "exit_status" in job and job["exit_status"].isdigit() \
                and int(job["exit_status"]) != 0 and state == "COMPLETED":
            state = "FAILED"
        job["state"] = state
        job["name"] = job.pop("Job_Name", "")
        if "walltime" in job:
            job["elapsed"] = job.pop("walltime")
        if "mtime" in job and state in ("COMPLETED", "FAILED", "EXITING"):
            try:
                job["completed_at"] = datetime.strptime(
                    job.pop("mtime"), "%a %b %d %H:%M:%S %Y"
                ).astimezone().isoformat(timespec="seconds")
            except ValueError:
                job.pop("mtime", None)
        if "stime" in job:
            try:
                job["started_at"] = datetime.strptime(
                    job.pop("stime"), "%a %b %d %H:%M:%S %Y"
                ).astimezone().isoformat(timespec="seconds")
            except ValueError:
                job.pop("stime", None)
        job.setdefault("completed_at", "")
    return jobs


# ------------------------------------------------------------- vasp parsing
def _incar_values(text: str) -> dict:
    values = {}
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if "=" in line:
            key, value = line.split("=", 1)
            key, value = key.strip().upper(), value.strip()
            if key and value:
                values[key] = value
    return values


def _int_value(values: dict, key: str, default: int) -> int:
    try:
        return int(float(values.get(key, default)))
    except (TypeError, ValueError):
        return default


def vasp_progress_payload(dir_text_files: dict) -> dict:
    """Scientific progress from bounded OSZICAR/OUTCAR/INCAR text.

    Deliberately independent from scheduler state: the caller merges the two.
    """
    oszicar = dir_text_files.get("OSZICAR", "")
    outcar = dir_text_files.get("OUTCAR", "")
    incar_text = dir_text_files.get("INCAR", "")
    values = _incar_values(incar_text)
    nelm = _int_value(values, "NELM", 60)
    nsw = _int_value(values, "NSW", 0)

    ionic, last_energy, last_e0, electronic_rows, nelm_hit = [], None, None, 0, False
    for raw_line in oszicar.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "E0=" in line or "F=" in line or "TOTEN" in line:
            step_m = re.match(r"^(\d+)", line)
            e0_m = re.search(r"E0=\s*([-+0-9.eEdD]+)", line)
            f_m = re.search(r"F=\s*([-+0-9.eEdD]+)", line)
            step = int(step_m.group(1)) if step_m else len(ionic) + 1
            if 0 < nelm <= electronic_rows:
                nelm_hit = True
            ionic.append({"step": step,
                          "e0": float(e0_m.group(1)) if e0_m else None,
                          "f": float(f_m.group(1)) if f_m else None,
                          "eddd_steps": electronic_rows})
            last_e0 = float(e0_m.group(1)) if e0_m else last_e0
            last_energy = float(f_m.group(1)) if f_m else last_energy
            electronic_rows = 0
        elif re.match(r"^\s*\d+\s+[-+0-9.eEdD]", line):
            electronic_rows += 1

    lower = outcar.lower()
    ionic_converged = "reached required accuracy" in lower
    signatures = []
    for pattern, code in (
            ("zbrent: fatal", "zbrent_fatal"),
            ("very bad news", "very_bad_news"),
            ("sub-space-matrix is not hermitian", "subspace_not_hermitian"),
            ("brmix: very serious problems", "brmix_serious"),
            ("p4_error", "mpi_abort"),
            ("the electronic self-consistency was not achieved",
             "electronic_selfconsistency_failed")):
        if pattern in lower:
            signatures.append(code)
    electronic_ok = not nelm_hit and "electronic_selfconsistency_failed" not in signatures
    ionic_ok = (len(ionic) >= 1 and electronic_ok) if nsw == 0 else ionic_converged

    return {
        "ionic_steps": len(ionic),
        "last_e0_ev": last_e0,
        "last_energy_ev": last_energy,
        "electronic_reached_nelm": nelm_hit,
        "ionic_converged": ionic_ok,
        "electronic_converged": electronic_ok,
        "scientific_converged": bool(ionic_ok and electronic_ok and not signatures),
        "error_signatures": signatures,
        "nelm": nelm,
        "nsw": nsw,
    }


def read_remote_bounded(name: str, path: str, max_bytes: int) -> str:
    """Read at most ``max_bytes`` of a remote text file (tail-biased)."""
    command = (
        f"size=$(wc -c < {q(path)} 2>/dev/null || echo 0); "
        f"if [ \"$size\" -gt {int(max_bytes)} ]; then "
        f"tail -c {int(max_bytes)} -- {q(path)}; else cat -- {q(path)} 2>/dev/null; fi")
    return remote(name, command)


# ----------------------------------------------------------- fs operations
def op_pwd(args) -> int:
    name, entry = resolve_server(args.server)
    out({"server": name, "root": str(effective_root(name, entry)),
         "pwd": remote(name, "pwd").strip()})
    audit("pwd", "ok", server=name)
    return 0


def op_list(args) -> int:
    name, entry = resolve_server(args.server)
    path = validated_remote_path(args.path or str(effective_root(name, entry)),
                                 entry, name)
    require_resolved_within_root(name, path, entry)
    # A directory view must remain non-recursive and bounded. Sorting before
    # applying a limit would enumerate a huge directory, so let `head` stop
    # the producer and request one extra entry to report truncation.
    limit = max(1, min(int(getattr(args, "limit", 500)), 2000))
    listing = remote(
        name,
        f"if [ -d {q(path)} ]; then find {q(path)} -maxdepth 1 -mindepth 1 "
        f"-printf '%y|%f|%s|%TY-%Tm-%TdT%TH:%TM:%TS\\n' | head -n {limit + 1}; "
        f"else echo NOTDIR; fi")
    entries = []
    if listing.strip() == "NOTDIR":
        return fail("not_a_directory", f"{path} is not a directory")
    for line in listing.splitlines():
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        kind, fname, size, mtime = parts
        if fname in (".", ".."):
            continue
        entries.append({"name": fname, "type": {"d": "dir", "f": "file",
                                                "l": "symlink"}.get(kind, kind),
                        "size": int(size) if size.isdigit() else 0,
                        "mtime": mtime.strip()})
    truncated = len(entries) > limit
    out({"path": path, "entries": entries[:limit], "truncated": truncated,
         "limit": limit})
    audit("list", "ok", path, name)
    return 0


def op_read(args) -> int:
    name, entry = resolve_server(args.server)
    path = validated_remote_path(args.path, entry, name)
    require_resolved_within_root(name, path, entry)
    if PurePosixPath(path).name.upper() in TEXT_DENYLIST:
        return fail("denied_file",
                    f"{PurePosixPath(path).name} may not be read as text")
    content = read_remote_bounded(name, path, MAX_READ_BYTES)
    out({"path": path, "size": len(content.encode('utf-8', 'replace')),
         "content": content})
    audit("read", "ok", path, name)
    return 0


def op_tail(args) -> int:
    name, entry = resolve_server(args.server)
    path = validated_remote_path(args.path, entry, name)
    require_resolved_within_root(name, path, entry)
    lines = max(1, min(int(args.lines), 2000))
    content = remote(name, f"tail -n {lines} -- {q(path)}")
    out({"path": path, "lines": lines, "content": content})
    audit("tail", "ok", f"{path} n={lines}", name)
    return 0


def op_find(args) -> int:
    name, entry = resolve_server(args.server)
    path = validated_remote_path(args.path, entry, name)
    require_resolved_within_root(name, path, entry)
    if not SAFE_GLOB.fullmatch(args.pattern or "*"):
        return fail("invalid_pattern", "find pattern is invalid")
    depth = max(1, min(int(args.max_depth), 8))
    limit = max(1, min(int(args.limit), 2000))
    listing = remote(
        name,
        f"find {q(path)} -maxdepth {depth} -name {q(args.pattern or '*')} -type f "
        f"-printf '%p|%s\\n' 2>/dev/null | head -n {limit}")
    files = []
    for line in listing.splitlines():
        p, _, size = line.rpartition("|")
        if p:
            files.append({"path": p, "size": int(size) if size.isdigit() else 0})
    out({"root": path, "pattern": args.pattern or "*", "files": files,
         "truncated": len(files) >= limit})
    audit("find", "ok", f"{path} {args.pattern}", name)
    return 0


def op_stat(args) -> int:
    name, entry = resolve_server(args.server)
    path = validated_remote_path(args.path, entry, name)
    require_resolved_within_root(name, path, entry)
    raw = remote(name, f"stat -c '%F|%s|%Y|%y' -- {q(path)} 2>/dev/null || echo MISSING")
    if raw.strip() == "MISSING":
        return fail("not_found", f"{path} does not exist")
    kind, size, mtime, mtime_iso = [p.strip() for p in raw.split("|", 3)]
    out({"path": path, "kind": kind, "size": int(size) if size.isdigit() else 0,
         "mtime_epoch": int(mtime) if mtime.isdigit() else 0,
         "mtime": mtime_iso})
    audit("stat", "ok", path, name)
    return 0


def op_du(args) -> int:
    name, entry = resolve_server(args.server)
    path = validated_remote_path(args.path, entry, name)
    require_resolved_within_root(name, path, entry)
    raw = remote(name, f"du -sb -- {q(path)} 2>/dev/null || du -sk -- {q(path)}")
    parts = raw.split()
    if not parts or not parts[0].isdigit():
        return fail("du_failed", f"du returned nothing for {path}")
    size = int(parts[0])
    human = remote(name, f"du -sh -- {q(path)} 2>/dev/null").strip() or raw.strip()
    out({"path": path, "bytes": size, "size_human": human})
    audit("du", "ok", path, name)
    return 0


MAX_WRITE_BYTES = 64 * 1024 * 1024


def _sha256_local(path: str) -> str:
    """Streaming sha256 of a Vlab-local file (the staged upload)."""
    import hashlib
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def op_write(args) -> int:
    """Structured text save: SHA-256 conflict check + same-directory temp
    file + atomic rename. Never a free-form shell write."""
    name, entry = resolve_server(args.server)
    path = validated_remote_path(args.path, entry, name)
    path = require_resolved_within_root(name, path, entry)
    require_not_root(name, path, entry)
    stage = validated_stage_path(args.stage)
    expected = (getattr(args, "expected_sha", "") or "").strip().lower()
    if expected and not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("expected sha256 must be empty or 64 hex chars")
    if os.path.basename(path).upper() in TEXT_DENYLIST:
        raise GatewayError("text_denylist",
                           f"{os.path.basename(path)} is not writable as text")
    stage_fs = _stage_fs_path(stage)
    if not stage_fs.is_file():
        return fail("stage_missing", f"staged content missing: {stage}")
    if stage_fs.stat().st_size > MAX_WRITE_BYTES:
        return fail("too_large",
                    f"content exceeds the {MAX_WRITE_BYTES // (1024*1024)} MiB write cap")
    new_sha = _sha256_local(str(stage_fs))
    new_size = stage_fs.stat().st_size

    def _cleanup_stage() -> None:
        try:
            stage_fs.unlink()
        except OSError:
            pass

    exists = remote(name, f"[ -e {q(path)} ] && echo yes || echo no").strip()
    cur_sha = ""
    if exists == "yes":
        raw = remote(
            name,
            f"sha256sum -- {q(path)} 2>/dev/null || true").strip()
        cur_sha = raw.split()[0] if raw and raw.split() else ""
        if cur_sha == new_sha and cur_sha == expected:
            audit("write", "unchanged", path, name)
            _cleanup_stage()
            mtime = remote(name, f"stat -c %Y -- {q(path)}").strip()
            out({"path": path, "sha256": cur_sha, "size": new_size,
                 "mtime_epoch": int(mtime) if mtime.isdigit() else 0})
            return 0
    if (expected or "") != cur_sha:
        audit("write", "conflict", path, name)
        _cleanup_stage()
        return fail("remote_changed",
                    "远端文件已被其他操作修改，请比较后再保存（已拒绝覆盖）")

    tmp = f"{path}.vaspilot-write-{uuid.uuid4().hex[:12]}"
    try:
        # stream the staged content onto the server through the mux
        stage_push(name, stage, tmp)
        tmp_sha = remote(
            name, f"sha256sum -- {q(tmp)} 2>/dev/null || true").strip()
        if not tmp_sha or tmp_sha.split()[0] != new_sha:
            _cleanup_stage()
            remote(name, f"rm -f -- {q(tmp)} 2>/dev/null")
            return fail("verify_failed",
                        "temporary file verification failed")
        if exists == "yes":
            remote(name, f"chmod --reference={q(path)} -- {q(tmp)}")
        remote(name, f"mv -f -- {q(tmp)} {q(path)}")
    finally:
        remote(name, f"rm -f -- {q(tmp)} 2>/dev/null")
    _cleanup_stage()
    mtime = remote(name, f"stat -c %Y -- {q(path)}").strip()
    out({"path": path, "sha256": new_sha, "size": new_size,
         "mtime_epoch": int(mtime) if mtime.isdigit() else 0})
    audit("write", "ok", path, name)
    return 0


def op_mkdir(args) -> int:
    name, entry = resolve_server(args.server)
    path = validated_remote_path(args.path, entry, name)
    resolved = require_resolved_within_root(name, path, entry)
    remote(name, f"mkdir -p -- {q(resolved)}")
    out({"path": resolved, "created": True})
    audit("mkdir", "ok", resolved, name)
    return 0


def op_copy(args) -> int:
    name, entry = resolve_server(args.server)
    src = validated_remote_path(args.path, entry, name)
    dst = validated_remote_path(args.destination, entry, name)
    require_resolved_within_root(name, src, entry)
    dst_r = require_resolved_within_root(name, dst, entry)
    remote(name, f"cp -a -- {q(src)} {q(dst_r)}")
    out({"copied": src, "to": dst_r})
    audit("copy", "ok", f"{src} -> {dst_r}", name)
    return 0


def op_move(args) -> int:
    name, entry = resolve_server(args.server)
    src = validated_remote_path(args.path, entry, name)
    dst = validated_remote_path(args.destination, entry, name)
    require_resolved_within_root(name, src, entry)
    dst_r = require_resolved_within_root(name, dst, entry)
    remote(name, f"mv -- {q(src)} {q(dst_r)}")
    out({"moved": src, "to": dst_r})
    audit("move", "ok", f"{src} -> {dst_r}", name)
    return 0


# -------------------------------------------------------------------- trash
def trash_root(name: str, entry: dict) -> str:
    return str(effective_root(name, entry) / TRASH_DIR_NAME)


def op_remove(args) -> int:
    """Quarantine a remote path under the server root (never destroys)."""
    name, entry = resolve_server(args.server)
    path = validated_remote_path(args.path, entry, name)
    resolved = require_resolved_within_root(name, path, entry)
    require_not_root(name, resolved, entry)
    if PurePosixPath(resolved).as_posix().startswith(trash_root(name, entry)):
        return fail("already_trashed",
                    "path is already inside the trash area")
    trash_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + \
        hashlib.sha256(os.urandom(8)).hexdigest()[:8]
    root = trash_root(name, entry)
    entry_dir = f"{root}/{trash_id}"
    meta = {
        "trash_id": trash_id,
        "state": "active",
        "original_path": resolved,
        "trashed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "server": name,
        "approval_ref": (args.approval_ref or "")[:120],
    }
    payload = f"{entry_dir}/payload"
    remote(
        name,
        f"mkdir -p -- {q(entry_dir)} && "
        f"mv -- {q(resolved)} {q(payload)} && "
        f"printf '%s' {q(json.dumps(meta, ensure_ascii=False))} > {q(entry_dir + '/metadata.json')}")
    out({"trash_id": trash_id, "moved": resolved, "trash_path": payload})
    audit("remove", "ok", f"{resolved} -> {trash_id}", name)
    return 0


def op_trash_list(args) -> int:
    name, entry = resolve_server(args.server)
    root = trash_root(name, entry)
    raw = remote(
        name,
        f"if [ -d {q(root)} ]; then "
        f"for m in {q(root)}/*/metadata.json; do [ -f \"$m\" ] && cat -- \"$m\" && echo; done; "
        f"else true; fi")
    items = []
    decoder = json.JSONDecoder()
    for chunk in raw.split("\n"):
        chunk = chunk.strip()
        if not chunk.startswith("{"):
            continue
        try:
            value, _ = decoder.raw_decode(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("trash_id"):
            items.append(value)
    out({"trash": items})
    audit("trash-list", "ok", f"{len(items)} entries", name)
    return 0


def op_restore(args) -> int:
    name, entry = resolve_server(args.server)
    if not SAFE_TRASH_ID.fullmatch(args.trash_id or ""):
        return fail("invalid_trash_id", "trash id is invalid")
    root = trash_root(name, entry)
    meta_path = f"{root}/{args.trash_id}/metadata.json"
    raw = remote(name, f"cat -- {q(meta_path)} 2>/dev/null || echo MISSING")
    if raw.strip() == "MISSING":
        return fail("not_found", f"trash entry {args.trash_id} does not exist")
    try:
        meta = json.loads(raw.strip().splitlines()[0])
    except (ValueError, IndexError):
        return fail("corrupt_metadata", "trash metadata is unreadable")
    if meta.get("state") == "restored":
        return fail("already_restored",
                    f"trash entry {args.trash_id} was already restored")
    original = str(meta.get("original_path", ""))
    try:
        validated = validated_remote_path(original, entry, name)
    except ValueError as exc:
        return fail("invalid_original", f"original path is now invalid: {exc}")
    payload = f"{root}/{args.trash_id}/payload"
    reply = remote(
        name,
        f"if [ -e {q(validated)} ]; then echo EXISTS; "
        f"elif [ -e {q(payload)} ]; then "
        f"mv -- {q(payload)} {q(validated)} && "
        f"printf '%s' {q(json.dumps({'state': 'restored', 'restored_to': validated}, ensure_ascii=False))} "
        f"> {q(meta_path)} && echo RESTORED; else echo MISSING_PAYLOAD; fi").strip()
    if "EXISTS" in reply:
        return fail("target_exists",
                    f"refusing to restore onto existing path {validated}")
    if "MISSING_PAYLOAD" in reply:
        return fail("payload_missing",
                    f"trash entry {args.trash_id} has no payload to restore")
    if "RESTORED" not in reply:
        return fail("restore_failed", "restore command did not complete")
    out({"restored": validated, "trash_id": args.trash_id})
    audit("restore", "ok", f"{args.trash_id} -> {validated}", name)
    return 0


def op_purge(args) -> int:
    """Irreversibly destroy one trash entry. Requires a double-matched id."""
    name, entry = resolve_server(args.server)
    if not SAFE_TRASH_ID.fullmatch(args.trash_id or ""):
        return fail("invalid_trash_id", "trash id is invalid")
    if args.confirm_trash_id != args.trash_id:
        return fail("confirm_mismatch",
                    "purge requires -ConfirmTrashId exactly matching -TrashId")
    root = trash_root(name, entry)
    target = f"{root}/{args.trash_id}"
    exists = remote(name, f"test -d {q(target)} && echo YES || echo NO").strip()
    if exists != "YES":
        return fail("not_found", f"trash entry {args.trash_id} does not exist")
    remote(name, f"rm -rf -- {q(target)}")
    out({"purged": args.trash_id})
    audit("purge", "ok", args.trash_id, name)
    return 0


# --------------------------------------------------------------------- jobs
def op_jobs(args) -> int:
    name, entry = resolve_server(args.server)
    scheduler = scheduler_for(name, entry)
    if scheduler == "pbs":
        raw = remote(name, "qstat -f 2>/dev/null || qstat -fx 2>/dev/null")
        mapped = _pbs_parse_qstat_f(raw)
        active_states = ("RUNNING", "PENDING", "EXITING", "HELD",
                         "SUSPENDED")
        jobs = [{"job_id": j["job_id"], "state": j["state"],
                 "elapsed": j.get("elapsed", ""),
                 "limit": j.get("walltime", j.get("limit", "")),
                 "partition": j.get("queue", ""),
                 "name": j.get("name", ""),
                 "nodes": j.get("exec_host", "")[:80]}
                for j in mapped.values() if j["state"] in active_states]
    else:
        command = ('squeue -u "$(id -un)" -h -o '
                   '"%i|%T|%M|%L|%P|%j|%N"')
        raw = remote(name, command)
        jobs = []
        for line in raw.splitlines():
            fields = [f.strip() for f in line.split("|")]
            if len(fields) >= 2 and fields[0][:1].isdigit():
                jobs.append({"job_id": fields[0].split(".")[0],
                             "state": normalize_state(fields[1]),
                             "elapsed": fields[2] if len(fields) > 2 else "",
                             "limit": fields[3] if len(fields) > 3 else "",
                             "partition": fields[4] if len(fields) > 4 else "",
                             "name": fields[5] if len(fields) > 5 else "",
                             "nodes": fields[6] if len(fields) > 6 else ""})
    out({"scheduler": scheduler, "jobs": jobs})
    audit("jobs", "ok", f"{len(jobs)} jobs", name)
    return 0


def op_recent(args) -> int:
    name, entry = resolve_server(args.server)
    scheduler = scheduler_for(name, entry)
    if scheduler == "pbs":
        raw = remote(name, "qstat -f 2>/dev/null || qstat -fx 2>/dev/null")
        mapped = _pbs_parse_qstat_f(raw)
        jobs = [{"job_id": j["job_id"], "name": j.get("name", ""),
                 "partition": j.get("queue", ""), "state": j["state"],
                 "elapsed": j.get("elapsed", ""),
                 "completed_at": j.get("completed_at", ""),
                 "started_at": j.get("started_at", ""),
                 "nodes": j.get("exec_host", "")[:80]}
                for j in mapped.values()]
    else:
        try:
            raw = remote(
                name,
                'sacct -u "$(id -un)" --starttime today -X -P '
                '-o JobID,JobName%24,Partition,State,Elapsed,ExitCode')
        except GatewayError as exc:
            # slurmdbd down (e.g. "Connection refused" on port 7031) must
            # not break the history view: squeue-driven active jobs and
            # the UI's local ledger keep working without accounting
            out({"scheduler": scheduler, "jobs": [],
                 "warning": f"sacct unavailable: {str(exc)[:180]}"})
            audit("recent", "failed", "sacct unavailable", name)
            return 0
        jobs = []
        for line in raw.splitlines():
            fields = [f.strip() for f in line.split("|")]
            if len(fields) >= 4 and fields[0][:1].isdigit():
                jobs.append({"job_id": fields[0].split(".")[0],
                             "name": fields[1], "partition": fields[2],
                             "state": normalize_state(fields[3]),
                             "elapsed": fields[4] if len(fields) > 4 else "",
                             "exit_code": fields[5] if len(fields) > 5 else ""})
    out({"scheduler": scheduler, "jobs": jobs})
    audit("recent", "ok", f"{len(jobs)} jobs", name)
    return 0


def op_submit(args) -> int:
    name, entry = resolve_server(args.server)
    directory = validated_remote_path(args.directory, entry, name)
    resolved = require_resolved_within_root(name, directory, entry)
    if not SAFE_JOB_SCRIPT.fullmatch(args.script or "") or args.script in (".", ".."):
        return fail("invalid_script", "job script must be a simple filename")
    scheduler = scheduler_for(name, entry)
    if scheduler == "pbs":
        command = f"cd -- {q(resolved)} && qsub -- {q(args.script)}"
    else:
        command = f"cd -- {q(resolved)} && sbatch --parsable -- {q(args.script)}"
    raw = remote(name, command).strip()
    match = re.search(r"(\d{1,19})", raw)
    if not match:
        return fail("submit_no_job_id", f"scheduler produced no job id: {raw[:200]}")
    out({"job_id": match.group(1), "scheduler": scheduler, "directory": resolved,
         "script": args.script, "raw": raw[:200],
         "approval_ref": (args.approval_ref or "")[:120]})
    audit("submit", "ok", f"{resolved}/{args.script} job={match.group(1)}", name)
    return 0


def op_cancel(args) -> int:
    name, entry = resolve_server(args.server)
    if not SAFE_JOB_ID.fullmatch(args.job_id or ""):
        return fail("invalid_job_id", "job id is invalid")
    if args.confirm_job_id != args.job_id:
        return fail("confirm_mismatch",
                    "cancel requires a confirmation job id that exactly matches")
    scheduler = scheduler_for(name, entry)
    if scheduler == "pbs":
        remote(name, f"qdel -- {q(args.job_id)}")
    else:
        remote(name, f"scancel -- {q(args.job_id)}")
    out({"cancelled": args.job_id, "scheduler": scheduler})
    audit("cancel", "ok", args.job_id, name)
    return 0


def op_job_state(args) -> int:
    """Normalized lifecycle state for one job (scheduler view ONLY)."""
    name, entry = resolve_server(args.server)
    if not SAFE_JOB_ID.fullmatch(args.job_id or ""):
        return fail("invalid_job_id", "job id is invalid")
    scheduler = scheduler_for(name, entry)
    if scheduler == "slurm":
        raw = remote(
            name,
            f"out=$(squeue -h -j {q(args.job_id)} -o '%i|%T' 2>/dev/null); "
            f"if [ -n \"$out\" ]; then echo \"$out\"; "
            f"else sacct -j {q(args.job_id)} -n -X -o JobID%40,State 2>/dev/null; fi")
        state = "UNKNOWN"
        for line in raw.splitlines():
            fields = [f.strip() for f in line.split("|")]
            if len(fields) >= 2 and fields[0].split(".")[0] == args.job_id.split(".")[0]:
                state = normalize_state(fields[1])
                break
    else:
        raw = remote(
            name,
            f"out=$(qstat -f {q(args.job_id)} 2>/dev/null | grep -i 'job_state' "
            f"| head -n 1); "
            f"if [ -n \"$out\" ]; then echo \"$out\"; "
            f"else qstat -x -f {q(args.job_id)} 2>/dev/null | grep -i 'job_state' "
            f"| head -n 1; fi")
        match = re.search(r"job_state\s*[:=]\s*(\w+)", raw, re.IGNORECASE)
        state = normalize_state(match.group(1)) if match else "COMPLETED"
    out({"job_id": args.job_id, "scheduler": scheduler, "state": state})
    audit("job-state", "ok", f"{args.job_id}={state}", name)
    return 0


# --------------------------------------------------------------------- vasp
def op_vasp_validate(args) -> int:
    name, entry = resolve_server(args.server)
    directory = validated_remote_path(args.directory, entry, name)
    resolved = require_resolved_within_root(name, directory, entry)
    listing = remote(
        name,
        f"for f in INCAR KPOINTS POSCAR POTCAR; do "
        f"if [ -f {q(resolved)}/$f ]; then printf '%s ' $f; fi; done; echo")
    present = set(listing.split())
    errors = []
    for required in ("INCAR", "KPOINTS", "POSCAR"):
        if required not in present:
            errors.append(f"missing required input {required}")
    incar = read_remote_bounded(name, f"{resolved}/INCAR", 65536) \
        if "INCAR" in present else ""
    if "INCAR" in present and not incar.strip():
        errors.append("INCAR is empty")
    out({"directory": resolved, "present": sorted(present), "errors": errors,
         "warnings": [], "incar": _incar_values(incar) if incar else {}})
    audit("vasp-validate", "ok" if not errors else "failed", resolved, name)
    return 0


def op_vasp_progress(args) -> int:
    name, entry = resolve_server(args.server)
    directory = validated_remote_path(args.directory, entry, name)
    resolved = require_resolved_within_root(name, directory, entry)
    texts = {}
    listing = remote(
        name,
        f"for f in INCAR OSZICAR OUTCAR CONTCAR DOSCAR EIGENVAL vasprun.xml; do "
        f"if [ -f {q(resolved)}/$f ]; then printf '%s ' $f; fi; done; echo")
    present = set(listing.split())
    if "INCAR" in present:
        texts["INCAR"] = read_remote_bounded(name, f"{resolved}/INCAR", 65536)
    if "OSZICAR" in present:
        texts["OSZICAR"] = read_remote_bounded(name, f"{resolved}/OSZICAR", 262144)
    if "OUTCAR" in present:
        texts["OUTCAR"] = read_remote_bounded(name, f"{resolved}/OUTCAR", 262144)
    progress = vasp_progress_payload(texts)
    progress["directory"] = resolved
    progress["files_present"] = sorted(present)
    out(progress)
    audit("vasp-progress", "ok", resolved, name)
    return 0


# --------------------------------------------------------------- diagnostic
DIAGNOSTICS = {
    "hostname": "hostname -f 2>/dev/null || hostname",
    "system": "echo cores=$(nproc); grep MemTotal /proc/meminfo 2>/dev/null | head -n 1; "
              "df -h --output=source,size,avail,pct,target . 2>/dev/null | tail -n +1; uname -srmo",
    "python": "python3 -V 2>&1; command -v python3",
    "disk": "df -h . 2>/dev/null | tail -n +1",
    "quota": "quota -s 2>/dev/null || echo 'quota: unavailable'",
    "partitions": "sinfo -h -o '%P %a %l %D %t %N' 2>/dev/null || "
                  "qstat -Q 2>/dev/null || echo 'partitions: unavailable'",
    "queues": "sinfo -h -o '%P %a %l %D %t %N' 2>/dev/null || "
              "qstat -Q 2>/dev/null || echo 'queues: unavailable'",
    "modules": "module avail 2>&1 | head -n 40 || echo 'modules: unavailable'",
    "scheduler": "if command -v sbatch >/dev/null 2>&1; then "
                 "echo slurm; sbatch --version 2>/dev/null | head -n 1; "
                 "elif command -v qsub >/dev/null 2>&1; then echo pbs; "
                 "qsub --version 2>/dev/null | head -n 1; else echo unknown; fi",
}


def op_diagnostic(args) -> int:
    name, entry = resolve_server(args.server)
    if args.diagnostic not in DIAGNOSTICS:
        return fail("invalid_diagnostic",
                    "diagnostic must be one of " + ", ".join(sorted(DIAGNOSTICS)))
    output = remote(name, DIAGNOSTICS[args.diagnostic])
    out({"server": name, "diagnostic": args.diagnostic,
         "output": output[:20000]})
    audit("diagnostic", "ok", args.diagnostic, name)
    return 0


# --------------------------------------------------------- exec (audit-only)
def op_exec(args) -> int:
    """Run one caller-supplied shell command on the HPC login node.

    Explicit operator policy: no interception, full audit. Unlike every
    other op there is no path confinement and no fixed command shape —
    the audit row is the control plane.
    """
    name, entry = resolve_server(args.server)
    require_connection(name)
    command_tokens = list(args.command or [])
    if command_tokens and command_tokens[0] == "--":
        command_tokens = command_tokens[1:]
    command = " ".join(command_tokens).strip()
    if not command:
        return fail("empty_command", "exec requires a command")
    if len(command) > 8000:
        return fail("command_too_long", "exec command exceeds 8000 chars")
    timeout = max(1, min(int(args.timeout or 120), 600))
    try:
        result = _remote_runner(name, entry["target"], command,
                                socket_path(name), timeout=timeout)
    except subprocess.TimeoutExpired:
        audit("exec", "timeout", command[:300], name)
        return fail("timeout", f"remote command exceeded {timeout}s")
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    audit("exec", "ok" if result.returncode == 0 else "fail",
          command[:300], name)
    out({
        "server": name, "rc": result.returncode,
        "stdout": stdout[:262144], "stderr": stderr[:65536],
        "truncated": len(stdout) > 262144 or len(stderr) > 65536,
        "command": command[:500],
    })
    return 0


# ---------------------------------------------------------------- metrics
METRICS_SCRIPT = r"""
echo __VP_CPU1__; head -n 1 /proc/stat 2>/dev/null
sleep 0.25
echo __VP_CPU2__; head -n 1 /proc/stat 2>/dev/null
echo __VP_LOAD__; cat /proc/loadavg 2>/dev/null
echo __VP_NPROC__; nproc 2>/dev/null
echo __VP_MEM__; grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo 2>/dev/null
echo __VP_DF__; df -P -k 2>/dev/null | sed -n '1,18p'
echo __VP_GPU__; if command -v nvidia-smi >/dev/null 2>&1; then nvidia-smi --query-gpu=index,gpu_uuid,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw --format=csv,noheader,nounits 2>&1 || echo _ERR_; else echo _MISSING_; fi
echo __VP_GPUPROC__; if command -v nvidia-smi >/dev/null 2>&1; then nvidia-smi --query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory --format=csv,noheader,nounits 2>/dev/null | while IFS=, read -r G P N M; do U=$(ps --no-headers -o user= -p "$(echo \"$P\" | tr -d ' ')" 2>/dev/null | tr -d ' '); printf '%s , %s , %s , %s , %s\n' \"$G\" \"$P\" \"$N\" \"$M\" \"$U\"; done; fi
echo __VP_SCHED__; if command -v sinfo >/dev/null 2>&1; then echo slurm; sinfo -h -o '%R|%a|%D|%C' 2>/dev/null; elif command -v qstat >/dev/null 2>&1; then echo pbs; qstat -q 2>/dev/null | sed -n '1,20p'; fi
echo __VP_DONE__
"""


def op_metrics(args) -> int:
    """Collect raw node-resource sections in one SSH round trip.

    The remote script is fixed; the LOCAL client parses the tagged sections
    (RackTop-style). Read-only by construction. A final heartbeat section
    touches <root>/.vp-monitor/hb when the offline collector directory
    exists, so the daemon only self-samples while no live poller is around.
    """
    name, entry = resolve_server(args.server)
    require_connection(name)
    hb = (f"mon={q(str(effective_root(name, entry)) + '/.vp-monitor')}; "
          f'[ -d "$mon" ] && touch "$mon/hb" 2>/dev/null; true')
    raw = remote(name, METRICS_SCRIPT + "\necho __VP_HB__\n" + hb + "\n")
    sections: dict = {}
    current = None
    for line in raw.splitlines():
        marker = re.fullmatch(r"__VP_([A-Z0-9_]+)__", line.strip())
        if marker:
            # the wire name is GPUPROC but the documented/parsed section
            # is "gpu_proc" -- normalize instead of leaking the mismatch
            current = marker.group(1).lower().replace("gpuproc", "gpu_proc")
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    sections = {key: "\n".join(lines).strip()
                for key, lines in sections.items()}
    audit("metrics", "ok", "", name)
    out({"server": name, "collected_at":
         datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "sections": sections})
    return 0


# ------------------------------------------------------------- data motion
def op_upload(args) -> int:
    """Move a gateway-local staged file into place after SHA-256 check.

    The CLI scps to the GATEWAY host's /tmp stage; the hash is therefore
    computed HERE (direct read), then the bytes are pushed onto the HPC
    through the mux. Mixing the two hosts would corrupt verification.
    """
    name, entry = resolve_server(args.server)
    stage = validated_stage_path(args.stage)
    target = validated_remote_path(args.path, entry, name)
    resolved = require_resolved_within_root(name, target, entry)
    expected = (args.sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return fail("invalid_sha256", "upload requires the local SHA-256")
    try:
        actual = vlab_stage_sha256(stage)
    except OSError:
        return fail("stage_missing",
                    f"staged file {stage} not found on the gateway host")
    if actual != expected:
        _stage_fs_path(stage).unlink(missing_ok=True)
        return fail("sha_mismatch",
                    f"staged file hash {actual[:12]}… != expected "
                    f"{expected[:12]}…",
                    expected=expected, got=actual)
    existing = remote(
        name,
        f"if [ -e {q(resolved)} ]; then sha256sum -- {q(resolved)} "
        f"2>/dev/null | cut -d' ' -f1; fi").strip()
    if existing == expected:
        _stage_fs_path(stage).unlink(missing_ok=True)
        out({"path": resolved, "sha256": expected, "status": "identical"})
        return 0
    if existing:
        return fail("target_exists",
                    f"{resolved} already exists with different content; "
                    f"remove it to the trash first")
    parent = str(PurePosixPath(resolved).parent)
    remote(name, f"mkdir -p -- {q(parent)}")
    stage_push(name, stage, resolved)
    _stage_fs_path(stage).unlink(missing_ok=True)
    out({"path": resolved, "sha256": expected, "status": "uploaded"})
    audit("upload", "ok", f"{resolved} sha={expected[:12]}", name)
    return 0


def op_download(args) -> int:
    """Copy a remote file to a gateway-local staged /tmp file + report sha."""
    name, entry = resolve_server(args.server)
    path = validated_remote_path(args.path, entry, name)
    resolved = require_resolved_within_root(name, path, entry)
    stage = validated_stage_path(args.stage)
    exists = remote(name, f"test -f {q(resolved)} && echo YES || echo NO").strip()
    if exists != "YES":
        return fail("not_found", f"{resolved} is not a regular file")
    try:
        stage_pull(name, resolved, stage)
    except GatewayError as exc:
        return fail(exc.code, exc.message)
    actual = vlab_stage_sha256(stage)
    size = _stage_fs_path(stage).stat().st_size
    out({"path": resolved, "sha256": actual, "size": size})
    audit("download", "ok", f"{resolved} sha={actual[:12]}", name)
    return 0


def op_transfer(args) -> int:
    """Server-to-server copy through the gateway host (bounded)."""
    from_name, from_entry = resolve_server(args.from_server)
    to_name, to_entry = resolve_server(args.to_server)
    src = validated_remote_path(args.from_path, from_entry, from_name)
    dst = validated_remote_path(args.to_path, to_entry, to_name)
    src_r = require_resolved_within_root(from_name, src, from_entry)
    dst_r = require_resolved_within_root(to_name, dst, to_entry)
    size_raw = remote(from_name, f"du -sb -- {q(src_r)} | cut -f1").strip()
    size = int(size_raw) if size_raw.isdigit() else 0
    if size > 8 * 1024 * 1024 * 1024:
        return fail("too_large", "transfer is capped at 8 GiB")
    remote(to_name, f"mkdir -p -- {q(str(PurePosixPath(dst_r).parent))}")
    # tar stream from source to destination over the two mux sessions
    if os.environ.get("VASPILOT_FAKE_HPC"):
        # test seam: perform as two bounded copies via a gateway-local temp file
        handle = tempfile.NamedTemporaryFile(delete=False, prefix="vaspilot-xfer-")
        stage = handle.name
        handle.write(remote(from_name, f"cat -- {q(src_r)}").encode("utf-8", "replace"))
        handle.close()
        remote(to_name, f"cp -- {q(stage)} {q(dst_r)} && rm -f -- {q(stage)}")
        os.unlink(stage)
    else:
        pull = subprocess.Popen(
            ["ssh", "-p", str(from_entry["port"]), "-o", "BatchMode=yes",
             "-o", f"ControlPath={socket_path(from_name)}", from_entry["target"],
             f"tar -C {q(str(PurePosixPath(src_r).parent))} -cf - -- {q(PurePosixPath(src_r).name)}"],
            stdout=subprocess.PIPE)
        push = subprocess.Popen(
            ["ssh", "-p", str(to_entry["port"]), "-o", "BatchMode=yes",
             "-o", f"ControlPath={socket_path(to_name)}", to_entry["target"],
             f"tar -C {q(str(PurePosixPath(dst_r).parent))} -xf -"],
            stdin=pull.stdout)
        pull.stdout.close()
        rc = push.wait()
        pull.wait()
        if rc != 0:
            return fail("transfer_failed", "tar stream transfer failed")
    out({"from": src_r, "to": dst_r, "size": size})
    audit("transfer", "ok", f"{src_r} -> {dst_r}", f"{from_name}->{to_name}")
    return 0


# ------------------------------------------------------------ server catalog
def op_servers(args) -> int:
    servers = []
    state = _load_reconnect()
    now = time.time()
    for name, entry in sorted(CFG.get("servers", {}).items()):
        s = state.get(name, {})
        servers.append({
            "name": name,
            "target": entry.get("target", ""),
            "port": entry.get("port", 22),
            "remote_root": entry.get("remote_root", ""),
            "persist": entry.get("persist", ""),
            "scheduler": entry.get("scheduler", "auto"),
            "auth_mode": entry.get("auth_mode", "interactive"),
            "auto_connect": bool(entry.get("auto_connect", False)),
            "connected": connected(name),
            "reconnect_state": (
                "online" if connected(name) else
                ("host_key_failed" if s.get("error_code") == "host_key_failed"
                 else ("waiting_backoff"
                       if now < float(s.get("next_attempt", 0))
                       else ("retrying" if s.get("failure_count") else "-")))),
            "failure_count": int(s.get("failure_count", 0)),
            "retry_in": max(0, int(float(s.get("next_attempt", 0)) - now)),
            "last_connect_error": str(s.get("error", "")
                                      or s.get("error_code", ""))[:120],
        })
    out({"servers": servers, "default": CFG.get("default_server", ""),
         "gateway_version": GATEWAY_VERSION, "protocol": PROTOCOL_VERSION})
    return 0


def op_server_add(args) -> int:
    if not SERVER_NAME_RE.fullmatch(args.name or ""):
        return fail("invalid_name", "server name is invalid")
    entry = {
        "target": args.target or "",
        "port": int(args.port or 22),
        "remote_root": args.root or "",
        "persist": args.persist or "8h",
        "scheduler": args.scheduler or "auto",
        "auth_mode": getattr(args, "auth_mode", "") or "interactive",
        "auto_connect": bool(getattr(args, "auto_connect", False)),
    }
    try:
        validate_server(args.name, entry)
    except ValueError as exc:
        return fail("invalid_server", str(exc))
    servers = dict(CFG.get("servers", {}))
    servers[args.name] = entry
    CFG["servers"] = servers
    if not CFG.get("default_server"):
        CFG["default_server"] = args.name
    save_config(CFG)
    audit("server-add", "ok", args.name)
    out({"added": args.name, "entry": entry})
    return 0


def op_server_remove(args) -> int:
    if not SERVER_NAME_RE.fullmatch(args.name or ""):
        return fail("invalid_name", "server name is invalid")
    servers = dict(CFG.get("servers", {}))
    if args.name not in servers:
        return fail("not_found", f"server {args.name} is not registered")
    del servers[args.name]
    CFG["servers"] = servers
    if CFG.get("default_server") == args.name:
        CFG["default_server"] = next(iter(sorted(servers)), "")
    save_config(CFG)
    audit("server-remove", "ok", args.name)
    out({"removed": args.name})
    return 0


def op_server_set_default(args) -> int:
    if args.name not in CFG.get("servers", {}):
        return fail("not_found", f"server {args.name} is not registered")
    CFG["default_server"] = args.name
    save_config(CFG)
    out({"default": args.name})
    return 0


def op_server_edit(args) -> int:
    servers = dict(CFG.get("servers", {}))
    if args.name not in servers:
        return fail("not_found", f"server {args.name} is not registered")
    entry = dict(servers[args.name])
    changed = False
    if args.target:
        entry["target"] = args.target
        changed = True
    if args.port:
        entry["port"] = int(args.port)
        changed = True
    if args.root is not None and args.root != "":
        entry["remote_root"] = args.root
        changed = True
    if args.persist:
        entry["persist"] = args.persist
        changed = True
    if args.scheduler:
        entry["scheduler"] = args.scheduler
        changed = True
    if getattr(args, "auth_mode", None):
        entry["auth_mode"] = args.auth_mode
        changed = True
    if getattr(args, "auto_connect", None) is not None:
        entry["auto_connect"] = bool(args.auto_connect)
        changed = True
    if not changed:
        return fail("no_changes", "server-edit needs at least one field")
    try:
        validate_server(args.name, entry)
    except ValueError as exc:
        return fail("invalid_server", str(exc))
    servers[args.name] = entry
    CFG["servers"] = servers
    save_config(CFG)
    audit("server-edit", "ok", args.name)
    out({"edited": args.name, "entry": entry})
    return 0


# ------------------------------------------------------------- session ops
def op_status(args) -> int:
    name, entry = resolve_server(args.server)
    state = reconnect_state(name)
    now = time.time()
    out({"server": name, "connected": connected(name),
         "socket": str(socket_path(name)),
         "auth_mode": entry.get("auth_mode", "interactive"),
         "auto_connect": bool(entry.get("auto_connect", False)),
         "reconnect_state": (
             "online" if connected(name) else
             ("host_key_failed" if state.get("error_code") == "host_key_failed"
              else ("waiting_backoff"
                    if now < float(state.get("next_attempt", 0))
                    else ("retrying" if state.get("failure_count")
                          else "-")))),
         "retry_in": max(0, int(float(state.get("next_attempt", 0)) - now)),
         "failure_count": int(state.get("failure_count", 0)),
         "last_connect_error": str(state.get("error", ""))[:120]})
    return 0


def op_connect(args) -> int:
    """Establish the ControlMaster session.

    interactive servers spawn the prompting master (password + TOTP in the
    caller's terminal). key-mode servers reconnect with their dedicated key
    via ``ensure_session``; ``--manual`` bypasses the reconnect backoff.
    """
    name, entry = resolve_server(args.server)
    manual = bool(getattr(args, "manual", False))
    if connected(name):
        record_reconnect(name, ok=True)
        out({"server": name, "connected": True, "already": True})
        return 0
    if entry.get("auth_mode") == "key" and entry.get("auto_connect"):
        ok, err = ensure_session(name, manual=manual)
        if ok:
            audit("connect", "ok", "key", name)
            out({"server": name, "connected": True, "via": "key"})
            return 0
        return fail(err or "connect_failed",
                    f"key reconnect failed for {name}: {err}")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sock = socket_path(name)
    if sock.exists():
        sock.unlink()
    if os.environ.get("VASPILOT_FAKE_HPC"):
        sock.write_text("up\n", encoding="utf-8")
        audit("connect", "ok", server=name)
        out({"server": name, "connected": True})
        return 0
    command = [
        "ssh", "-M", "-S", str(sock),
        "-o", "ControlMaster=yes",
        "-o", f"ControlPersist={entry.get('persist') or '8h'}",
        # heartbeat keepalives: NAT/firewall devices silently drop idle
        # TCP sessions, which would defeat ControlPersist=yes
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=4",
        "-o", "StrictHostKeyChecking=ask",
        "-o", "UpdateHostKeys=no",
        "-o", "NumberOfPasswordPrompts=3",
        "-p", str(entry.get("port", 22)), "-fN", entry["target"],
    ]
    result = subprocess.run(command)
    ok_now = result.returncode == 0 and connected(name)
    audit("connect", "ok" if ok_now else "failed", server=name)
    if not ok_now:
        return fail("connect_failed",
                    f"could not establish the multiplexed session for {name}")
    out({"server": name, "connected": True})
    return 0


def op_disconnect(args) -> int:
    name, entry = resolve_server(args.server)
    if not connected(name):
        out({"server": name, "connected": False, "already": True})
        return 0
    if os.environ.get("VASPILOT_FAKE_HPC"):
        socket_path(name).unlink(missing_ok=True)
        out({"server": name, "connected": False})
        audit("disconnect", "ok", server=name)
        return 0
    result = subprocess.run(
        ["ssh", "-p", str(entry["port"]), "-o", "BatchMode=yes",
         "-o", f"ControlPath={socket_path(name)}", "-O", "exit", entry["target"]],
        stdin=subprocess.DEVNULL, capture_output=True, text=True)
    out({"server": name, "connected": connected(name)})
    audit("disconnect", "ok" if result.returncode == 0 else "failed", server=name)
    return 0


# ------------------------------------------------------- per-server key mgmt
def _pub_prefix(pub: Path) -> str:
    """`<type> <b64>` head of the public key — the precise, comment-free
    identity used for authorized_keys line matching."""
    parts = pub.read_text(encoding="utf-8").strip().split()
    if len(parts) < 2:
        raise GatewayError("key_invalid", "public key file is malformed")
    return f"{parts[0]} {parts[1]}"


def op_key_generate(args) -> int:
    """Create the per-server Ed25519 pair on this gateway host (Vlab)."""
    name = args.name
    if not SERVER_NAME_RE.fullmatch(name or ""):
        return fail("invalid_name", "server name is invalid")
    priv, pub = _key_paths(name)
    if priv.exists() or pub.exists():
        return fail("key_exists",
                    "keys already exist; use key-disable/key-revoke instead "
                    "of overwriting (refusing to clobber)")
    KEY_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(KEY_DIR, 0o700)
    if os.environ.get("VASPILOT_FAKE_HPC"):
        priv.write_text("FAKE-PRIVATE\n")
        pub.write_text(f"ssh-ed25519 FAKEPUB vaspilot:{name}\n")
    else:
        result = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
             "-C", f"vaspilot:{name}", "-f", str(priv)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if result.returncode != 0:
            return fail("keygen_failed",
                        (result.stderr or "ssh-keygen failed").strip()[:200])
    os.chmod(priv, 0o600)
    audit("key.generate", "ok", name)
    out({"server": name, "generated": True,
         "key_material_present": True})
    return 0


def op_key_status(args) -> int:
    name = args.name
    if not SERVER_NAME_RE.fullmatch(name or ""):
        return fail("invalid_name", "server name is invalid")
    priv, pub = _key_paths(name)
    entry = CFG["servers"].get(name, {})
    state = reconnect_state(name)
    out({
        "server": name,
        "auth_mode": entry.get("auth_mode", "interactive"),
        "auto_connect": bool(entry.get("auto_connect", False)),
        "key_material_present": priv.is_file() and pub.is_file(),
        "batch_login_verified": bool(state.get("batch_login_verified")),
        "last_verified": str(state.get("last_verified", "")),
        "reconnect_state": ("online" if connected(name) else
                            state.get("error_code", "-")),
        "error": str(state.get("error", ""))[:160],
    })
    return 0


def op_key_install(args) -> int:
    """Append the public key to the HPC authorized_keys over the LIVE mux,
    then prove it with a BatchMode login using ONLY the new key."""
    name, entry = resolve_server(args.name)
    priv, pub = _key_paths(name)
    if not (priv.is_file() and pub.is_file()):
        return fail("key_missing",
                    "generate the per-server key first (key-generate)")
    pubfull = pub.read_text(encoding="utf-8").strip()
    quoted = q(pubfull)
    remote(name,
           f"mkdir -p ~/.ssh && chmod 700 ~/.ssh; "
           f"touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys; "
           f"grep -qxF {quoted} ~/.ssh/authorized_keys || "
           f"printf '%s\\n' {quoted} >> ~/.ssh/authorized_keys")
    if os.environ.get("VASPILOT_FAKE_HPC"):
        # fake mode: reconnect simulation keys off this config flag
        import json as _json
        cfg_path = os.environ.get("VASPILOT_FAKE_HPC_CONFIG")
        try:
            fake = _json.loads(Path(cfg_path).read_text(encoding="utf-8")) \
                if cfg_path else {}
        except (OSError, ValueError):
            fake = {}
        fake.setdefault("servers", {}).setdefault(name, {})[
            "key_installed"] = True
        if cfg_path:
            Path(cfg_path).write_text(_json.dumps(fake), encoding="utf-8")
    else:
        verify = [
            "ssh", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
            "-o", "ConnectTimeout=15", "-i", str(priv),
            "-p", str(entry.get("port", 22)), entry["target"],
            "echo VP_KEY_OK",
        ]
        result = subprocess.run(verify, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=45)
        if result.returncode != 0 or "VP_KEY_OK" not in (result.stdout or ""):
            low = (result.stderr or "").lower()
            code = ("host_key_failed" if "host key" in low else
                    "key_rejected" if ("permission denied" in low or
                                       "publickey" in low) else
                    "network_unreachable")
            audit("key.install", "failed", code, name)
            return fail("key_verify_failed",
                        f"BatchMode login with the new key failed: {code}; "
                        "staying in interactive mode")
    entry["auth_mode"] = "key"
    entry["auto_connect"] = True
    servers = dict(CFG.get("servers", {}))
    servers[name] = {**servers.get(name, {}), **entry}
    CFG["servers"] = servers
    save_config(CFG)
    record_reconnect(name, ok=True)
    state = _load_reconnect()
    state.setdefault(name, {})
    state[name]["batch_login_verified"] = True
    state[name]["last_verified"] = datetime.now(
        timezone.utc).isoformat(timespec="seconds")
    _save_reconnect(state)
    audit("key.install", "ok", name)
    out({"server": name, "auth_mode": "key", "auto_connect": True,
         "batch_login_verified": True})
    return 0


def op_key_disable(args) -> int:
    """Stop using the key (back to interactive) WITHOUT deleting anything."""
    name = args.name
    if name not in CFG.get("servers", {}):
        return fail("not_found", f"server {name} is not registered")
    entry = dict(CFG["servers"][name])
    entry["auth_mode"] = "interactive"
    entry["auto_connect"] = False
    servers = dict(CFG.get("servers", {}))
    servers[name] = entry
    CFG["servers"] = servers
    save_config(CFG)
    audit("key.disable", "ok", name)
    out({"server": name, "auth_mode": "interactive", "auto_connect": False,
         "key_material_present":
             all(p.is_file() for p in _key_paths(name))})
    return 0


def op_key_revoke(args) -> int:
    """Remove the exact `vaspilot:<server>` public key from the HPC and
    delete the gateway-side pair. Requires the typed server name twice and
    a live session; anything ambiguous aborts without touching the file."""
    name, entry = resolve_server(args.name)
    confirm = getattr(args, "confirm_server", "")
    if confirm != name:
        return fail("confirm_mismatch",
                    f"revoke requires --confirm-server to exactly repeat "
                    f"the server name {name}")
    priv, pub = _key_paths(name)
    if not pub.is_file():
        return fail("key_missing", "no gateway-side public key to revoke")
    prefix = _pub_prefix(pub)
    pubfull = pub.read_text(encoding="utf-8").strip()
    check = remote(name, f"grep -cF {q(pubfull)} ~/.ssh/authorized_keys || true")
    removed = int(check.strip() or 0)
    if removed > 1:
        return fail("ambiguous", f"{removed} identical lines found; "
                                 "refusing to edit authorized_keys blindly")
    if removed == 1:
        remote(name,
               f"awk 'index($0, prefix) != 1' prefix={q(prefix)} "
               f"~/.ssh/authorized_keys > ~/.ssh/authorized_keys.vptmp && "
               f"mv ~/.ssh/authorized_keys.vptmp ~/.ssh/authorized_keys && "
               f"chmod 600 ~/.ssh/authorized_keys")
    priv.unlink(missing_ok=True)
    pub.unlink(missing_ok=True)
    if name in CFG.get("servers", {}):
        entry = dict(CFG["servers"][name])
        entry["auth_mode"] = "interactive"
        entry["auto_connect"] = False
        servers = dict(CFG.get("servers", {}))
        servers[name] = entry
        CFG["servers"] = servers
        save_config(CFG)
    audit("key.revoke", "ok", f"{name} removed={removed}")
    out({"server": name, "revoked": True, "lines_removed": removed,
         "auth_mode": "interactive"})
    return 0


def op_whoami(args) -> int:
    name, entry = resolve_server(args.server)
    raw = remote(name, 'echo "$(id -un)@$(hostname -s):$(pwd)"').strip()
    out({"server": name, "identity": raw, "root": str(effective_root(name, entry))})
    return 0


def op_version(args) -> int:
    out({"gateway_version": GATEWAY_VERSION, "protocol": PROTOCOL_VERSION,
         "python": sys.version.split()[0]})
    return 0


# -------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vaspilot-gateway")
    sub = parser.add_subparsers(dest="operation", required=True)

    def add(name, func, *, server=True):
        child = sub.add_parser(name)
        if server:
            child.add_argument("--server")
        child.set_defaults(func=func)
        return child

    add("version", op_version, server=False)
    add("servers", op_servers, server=False)

    p = add("server-add", op_server_add, server=False)
    p.add_argument("name")
    p.add_argument("--target", required=True)
    p.add_argument("--port", type=int, default=22)
    p.add_argument("--root", default="")
    p.add_argument("--persist", default="8h")
    p.add_argument("--scheduler", default="auto", choices=["auto", "slurm", "pbs"])
    p.add_argument("--auth-mode", default="interactive",
                   choices=["interactive", "key"])
    p.add_argument("--auto-connect", action="store_true")

    p = add("server-remove", op_server_remove, server=False)
    p.add_argument("name")
    p = add("server-set-default", op_server_set_default, server=False)
    p.add_argument("name")
    p = add("server-edit", op_server_edit, server=False)
    p.add_argument("name")
    p.add_argument("--target")
    p.add_argument("--port", type=int)
    p.add_argument("--root")
    p.add_argument("--persist")
    p.add_argument("--scheduler", choices=["auto", "slurm", "pbs"])
    p.add_argument("--auth-mode", choices=["interactive", "key"])
    p.add_argument("--auto-connect", dest="auto_connect",
                   action="store_true", default=None)

    add("status", op_status)
    p = add("connect", op_connect)
    p.add_argument("--manual", action="store_true",
                   help="bypass the reconnect backoff for this attempt")
    add("disconnect", op_disconnect)
    add("whoami", op_whoami)

    # ---------------------------------------------- per-server key lifecycle
    p = add("key-generate", op_key_generate, server=False)
    p.add_argument("name")
    p = add("key-install", op_key_install)
    p.add_argument("name")
    p = add("key-status", op_key_status, server=False)
    p.add_argument("name")
    p = add("key-disable", op_key_disable, server=False)
    p.add_argument("name")
    p = add("key-revoke", op_key_revoke)
    p.add_argument("name")
    p.add_argument("--confirm-server", required=True)

    p = add("pwd", op_pwd)
    p = add("list", op_list)
    p.add_argument("path", nargs="?")
    p.add_argument("--limit", type=int, default=500)
    p = add("read", op_read)
    p.add_argument("path")
    p = add("tail", op_tail)
    p.add_argument("path")
    p.add_argument("--lines", type=int, default=80)
    p = add("find", op_find)
    p.add_argument("path")
    p.add_argument("--pattern", default="*")
    p.add_argument("--max-depth", type=int, default=2)
    p.add_argument("--limit", type=int, default=200)
    p = add("stat", op_stat)
    p.add_argument("path")
    p = add("du", op_du)
    p.add_argument("path")
    p = add("write", op_write)
    p.add_argument("stage")
    p.add_argument("path")
    p.add_argument("--expected-sha", default="")
    p = add("mkdir", op_mkdir)
    p.add_argument("path")
    p = add("copy", op_copy)
    p.add_argument("path")
    p.add_argument("--destination", required=True)
    p = add("move", op_move)
    p.add_argument("path")
    p.add_argument("--destination", required=True)
    p = add("remove", op_remove)
    p.add_argument("path")
    p.add_argument("--approval-ref", dest="approval_ref", default="")
    p = add("trash-list", op_trash_list)
    p = add("restore", op_restore)
    p.add_argument("trash_id")
    p = add("purge", op_purge)
    p.add_argument("trash_id")
    p.add_argument("--confirm-trash-id", dest="confirm_trash_id", required=True)

    p = add("jobs", op_jobs)
    p = add("recent", op_recent)
    p = add("submit", op_submit)
    p.add_argument("directory")
    p.add_argument("script")
    p.add_argument("--approval-ref", dest="approval_ref", default="")
    p = add("cancel", op_cancel)
    p.add_argument("job_id")
    p.add_argument("--confirm-job-id", dest="confirm_job_id", required=True)
    p = add("job-state", op_job_state)
    p.add_argument("job_id")

    p = add("vasp-validate", op_vasp_validate)
    p.add_argument("directory")
    p = add("vasp-progress", op_vasp_progress)
    p.add_argument("directory")

    p = add("diagnostic", op_diagnostic)
    p.add_argument("diagnostic", choices=sorted(DIAGNOSTICS))

    p = add("exec", op_exec)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("command", nargs=argparse.REMAINDER)
    add("metrics", op_metrics)

    p = add("upload", op_upload)
    p.add_argument("stage")
    p.add_argument("path")
    p.add_argument("sha256")
    p = add("download", op_download)
    p.add_argument("path")
    p.add_argument("stage")
    p = add("transfer", op_transfer)
    p.add_argument("--from-server", dest="from_server", required=True)
    p.add_argument("--from-path", dest="from_path", required=True)
    p.add_argument("--to-server", dest="to_server", required=True)
    p.add_argument("--to-path", dest="to_path", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except GatewayError as exc:
        return fail(exc.code, exc.message)
    except ValueError as exc:
        return fail("invalid_argument", str(exc))
    except subprocess.TimeoutExpired:
        return fail("timeout", "the remote operation timed out")
    except FileNotFoundError as exc:
        return fail("ssh_missing", f"ssh is unavailable: {exc}")


if __name__ == "__main__":
    sys.exit(main())
