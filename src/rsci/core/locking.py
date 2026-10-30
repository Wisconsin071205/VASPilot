"""Small cross-process advisory locks for local state files.

The workflow approval ledger is a read/modify/write resource.  Atomic JSON
replacement protects readers from half-written files, but it does not prevent
two processes from consuming the same approval at the same time.  This module
keeps that critical section serialized on Windows and POSIX without adding a
third-party dependency.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_HELD_LOCKS = threading.local()


@contextmanager
def reentrant_file_lock(path: str | Path) -> Iterator[None]:
    """Nested store operations share one OS lock, including across instances."""
    key = str(Path(path).expanduser().resolve())
    held = getattr(_HELD_LOCKS, "paths", set())
    if key in held:
        yield
        return
    with exclusive_file_lock(path):
        _HELD_LOCKS.paths = held | {key}
        try:
            yield
        finally:
            _HELD_LOCKS.paths = held


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path.expanduser().resolve())
    with _LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def exclusive_file_lock(path: str | Path) -> Iterator[None]:
    """Hold an advisory lock represented by ``path``.

    The lock file is intentionally retained; it is only a coordination
    sentinel and contains a single byte.  OS locks are released automatically
    if the process exits unexpectedly.
    """
    lock_path = Path(path).expanduser()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock(lock_path):
        with open(lock_path, "a+b") as handle:
            # Windows byte locks are mandatory for reads too. Inspecting the
            # sentinel byte before acquiring its lock races a live owner and
            # raises EACCES instead of waiting. fstat never touches that byte.
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def try_file_lock(path: str | Path) -> Iterator[bool]:
    """Try once, without waiting. A live owner is never displaced by a PID check.

    Unlike the blocking lock this is also suitable for startup discovery and
    status requests. Keep the sentinel inode in place even after releasing it.
    """
    lock_path = Path(path).expanduser()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    local = _thread_lock(lock_path)
    if not local.acquire(blocking=False):
        yield False
        return
    try:
        with open(lock_path, "a+b") as handle:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            acquired = False
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                import errno
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
            try:
                yield acquired
            finally:
                if acquired:
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        local.release()
