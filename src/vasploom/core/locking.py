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
            handle.seek(0)
            if handle.read(1) != b"\0":
                handle.seek(0)
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
