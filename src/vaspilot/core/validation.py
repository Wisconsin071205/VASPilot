"""Strict validation of every externally supplied identifier and path.

These functions are the single gatekeeper used by the CLI, the agent tool
registry and the MCP server. They reject:

  - path traversal (``..``, absolute-local tricks, home shorthands)
  - shell metacharacters smuggled into filenames, patterns or job scripts
  - server names that are not simple identifiers
  - job/trash ids that do not match the scheduler/trash formats

Nothing here ever falls back to "best effort": invalid input raises
:class:`ValidationError` and the caller fails closed.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from .errors import ValidationError

SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
JOB_ID_RE = re.compile(r"^[0-9]{1,19}([.][A-Za-z0-9._-]{0,63})?$")
# gateway trash ids: 20260826T235959Z-ab12cd34 (also tolerate UUIDs)
TRASH_ID_RE = re.compile(
    r"^([0-9]{8}[Tt][0-9]{6}[Zz]-[0-9a-f]{8}"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")
PLAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
GLOB_RE = re.compile(r"^[A-Za-z0-9*?][A-Za-z0-9*?.._+-]{0,127}$")
# Characters that would break single-quoted POSIX shells or Windows arguments.
SHELL_META = set(" \t\r\n\"'`$;&|<>\\(){}[]!*?~^#%")

def valid_server_name(value: object) -> str:
    if not isinstance(value, str) or not SERVER_NAME_RE.fullmatch(value):
        raise ValidationError(
            f"server name {value!r} must match {SERVER_NAME_RE.pattern}")
    return value


def valid_job_id(value: object) -> str:
    if not isinstance(value, str) or not JOB_ID_RE.fullmatch(value):
        raise ValidationError(
            f"job id {value!r} must be a scheduler numeric id")
    return value


def valid_trash_id(value: object) -> str:
    if not isinstance(value, str) or not TRASH_ID_RE.fullmatch(value):
        raise ValidationError("trash id must be a gateway-issued identifier")
    return value


def valid_filename(value: object) -> str:
    if not isinstance(value, str) or not SAFE_FILENAME_RE.fullmatch(value):
        raise ValidationError(
            f"filename {value!r} contains forbidden characters")
    if value in {".", ".."}:
        raise ValidationError("filename may not be '.' or '..'")
    return value


def valid_glob(value: object) -> str:
    if not isinstance(value, str) or not GLOB_RE.fullmatch(value):
        raise ValidationError(
            f"pattern {value!r} contains forbidden characters")
    return value


def _posix_no_traversal(path: PurePosixPath) -> None:
    if ".." in path.parts or "." in path.parts:
        raise ValidationError(f"remote path {str(path)!r} must not traverse")


def remote_path(value: object, *, remote_root: str) -> str:
    """Validate one absolute remote POSIX path confined under ``remote_root``.

    ``remote_root`` itself is the confinement boundary declared per server.
    Relative paths are rejected; the caller may pass root-relative names
    through :func:`join_remote` instead.
    """
    if not isinstance(value, str) or not value:
        raise ValidationError("remote path is required")
    if "\x00" in value or any(ord(ch) < 32 for ch in value):
        raise ValidationError("remote path contains control characters")
    # reject "." / ".." in the RAW string before PurePosixPath normalizes
    # them away ("/root/." must not silently become the root itself)
    segments = [seg for seg in value.split("/") if seg]
    if any(seg in (".", "..") for seg in segments):
        raise ValidationError(f"remote path {value!r} must not traverse")
    raw = PurePosixPath(value)
    if not raw.is_absolute():
        raise ValidationError(f"remote path {value!r} must be absolute")
    _posix_no_traversal(raw)
    root = PurePosixPath(remote_root)
    if not root.is_absolute():
        raise ValidationError(f"server remote_root {remote_root!r} must be absolute")
    _posix_no_traversal(root)
    if raw != root and root not in raw.parents:
        raise ValidationError(
            f"remote path {value!r} is outside the server root {remote_root!r}")
    return raw.as_posix()


def join_remote(remote_root: str, *parts: str) -> str:
    """Join validated root-relative parts onto ``remote_root``."""
    combined = PurePosixPath(remote_root)
    for part in parts:
        name = valid_filename(part)
        combined = combined / name
    return remote_path(combined.as_posix(), remote_root=remote_root)


def safe_relative_remote(value: object) -> str:
    """A single path segment or a bounded relative path (no traversal)."""
    if not isinstance(value, str) or not value:
        raise ValidationError("relative remote path is required")
    if value.startswith("/") or "\x00" in value:
        raise ValidationError("relative remote path must not be absolute")
    raw = PurePosixPath(value)
    _posix_no_traversal(raw)
    for part in raw.parts:
        valid_filename(part)
    return raw.as_posix()


def local_project_path(value: object, *, project_root: str | Path) -> Path:
    """Resolve ``value`` inside ``project_root``; traversal and symlinks fail."""
    if not isinstance(value, str) or not value:
        raise ValidationError("local path is required")
    root = Path(project_root).expanduser().resolve(strict=False)
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValidationError(
            f"local path {value!r} escapes the project root") from exc
    if "\x00" in value:
        raise ValidationError("local path contains a null byte")
    return resolved


def scheduler_kind(value: object) -> str:
    if value not in ("slurm", "pbs"):
        raise ValidationError("scheduler must be 'slurm' or 'pbs'")
    return str(value)


def no_shell_meta(value: object, *, label: str) -> str:
    """Reject strings containing characters that could escape shell quoting."""
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a string")
    bad = sorted(set(value) & SHELL_META)
    if bad:
        raise ValidationError(
            f"{label} contains forbidden shell metacharacters: {''.join(bad)}")
    return value


def confirm_match(value: object, expected: str, *, label: str) -> str:
    """Double-match confirmation for destructive operations (cancel/purge)."""
    if not isinstance(value, str) or value != expected:
        raise ValidationError(
            f"{label} confirmation must repeat the exact identifier")
    return value
