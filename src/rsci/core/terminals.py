"""Per-conversation persistent remote-terminal state.

The agent drives each HPC login node like a person sitting at the
terminal: the working directory established by one command is still in
effect for the next. Only cwd is carried across commands — environment
stacking (`module load`, `conda activate`) stays explicit inline chaining,
because VASP job environments belong inside the submitted scripts anyway.

State lives per chat session id, capped LRU so long-running consoles do
not accumulate.
"""

from __future__ import annotations

from collections import OrderedDict

MARKER = "@@VP_CWD@@"
MAX_SESSIONS = 64


class TerminalHub:
    """cwd memory for one chat session: {session-scoped server key: path}."""

    def __init__(self) -> None:
        self._cwd: dict[str, str] = {}

    def get(self, key: str) -> str:
        return self._cwd.get(key, "")

    def set(self, key: str, path: str) -> None:
        path = str(path or "").strip()
        if path.startswith("/"):
            self._cwd[key] = path

    def reset(self, key: str) -> None:
        self._cwd.pop(key, None)


_HUBS: "OrderedDict[str, TerminalHub]" = OrderedDict()


def hub_for(session_id: str) -> TerminalHub:
    """LRU-bounded factory keyed by chat session."""
    sid = str(session_id or "")
    if not sid:
        return TerminalHub()          # anonymous: no cross-turn memory
    hub = _HUBS.get(sid)
    if hub is None:
        hub = TerminalHub()
        _HUBS[sid] = hub
        while len(_HUBS) > MAX_SESSIONS:
            _HUBS.popitem(last=False)
    else:
        _HUBS.move_to_end(sid)
    return hub


def strip_marker(stdout: str) -> tuple[str, str]:
    """Split a trailing ``@@VP_CWD@@/path`` line off command output."""
    idx = stdout.rfind(MARKER)
    if idx < 0:
        return stdout, ""
    tail = stdout[idx + len(MARKER):].strip().splitlines()
    new_cwd = tail[0].strip() if tail else ""
    cleaned = stdout[:idx].rstrip("\n")
    if idx > 0 and "\n" not in stdout[:idx][-2:]:
        cleaned = cleaned.rstrip()
    return cleaned + ("\n" if cleaned and not cleaned.endswith("\n") else ""), \
        new_cwd
