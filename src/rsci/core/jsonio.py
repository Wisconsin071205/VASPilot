"""Stable JSON output for every CLI command.

Rules:
  - one JSON document per invocation, UTF-8, sorted keys, newline-terminated
  - success: ``{"ok": true, ...}``
  - failure: ``{"ok": false, "error": {"code", "message", ...}}``
  - values are never locale-formatted; timestamps are UTC ISO-8601
"""

from __future__ import annotations

import json
import sys


def emit(payload: dict, *, stream=None) -> None:
    """Write one stable JSON document. ``ok`` is forced to a bool first."""
    document = dict(payload)
    document["ok"] = bool(document.get("ok", False))
    text = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2)
    out = stream if stream is not None else sys.stdout
    out.write(text + "\n")
    out.flush()


def emit_error(error_dict: dict, *, stream=None) -> None:
    emit({"ok": False, "error": error_dict}, stream=stream)
