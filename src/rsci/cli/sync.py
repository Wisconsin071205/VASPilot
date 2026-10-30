"""``rsci sync`` — WebDAV connection checks and manual synchronization."""

from __future__ import annotations

import argparse

from ..core.errors import rsciError
from ..sync.engine import SyncEngine


def register(sub) -> None:
    parser = sub.add_parser(
        "sync", help="WebDAV configuration/conversation synchronization")
    children = parser.add_subparsers(dest="sync_command", required=True)

    status = children.add_parser("status", help="show saved WebDAV sync state")
    status.set_defaults(handler=_status)

    test = children.add_parser("test", help="check the saved WebDAV endpoint")
    test.set_defaults(handler=_test)

    now = children.add_parser("now", help="run one bidirectional sync")
    now.set_defaults(handler=_now)


def _status(app, args):
    return SyncEngine(app.config).status()


def _test(app, args):
    return SyncEngine(app.config).test()


def _now(app, args):
    result = SyncEngine(app.config).sync()
    if not result.get("ok"):
        errors = result.get("errors") or []
        message = (errors[0].get("message") if errors else "") or \
            result.get("message") or "WebDAV 同步失败"
        raise rsciError(message, detail={"result": result})
    return result
