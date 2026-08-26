"""``vaspilot monitor ...`` — fleet snapshots and change-only watching."""

from __future__ import annotations

import json
import time

from ..core.errors import ValidationError
from ..core.jsonio import emit


def register(sub) -> None:
    parser = sub.add_parser("monitor", help="multi-server monitoring")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("snapshot", help="aggregate all servers").set_defaults(
        handler=cmd_snapshot)

    p = commands.add_parser("watch",
                            help="poll all servers; print only changes")
    p.set_defaults(handler=cmd_watch)
    p.add_argument("--servers", default="all",
                   help="'all' or a comma-separated list")
    p.add_argument("--interval", type=int, default=60,
                   help="poll interval in seconds")


def _snapshot(app, names=None) -> dict:
    """One aggregate over every (or the named) registered server."""
    servers = [s.to_dict() for s in app.config.load_servers()]
    if names and names != ["all"]:
        wanted = set(names)
        servers = [s for s in servers if s["name"] in wanted]
    entries = []
    for entry in servers:
        name = entry["name"]
        record = {"server": name, "scheduler": entry["scheduler"]}
        try:
            status = app.client().status(name)
            record["connected"] = bool(status.get("connected"))
        except Exception as exc:
            record["connected"] = False
            record["error"] = str(exc)[:200]
            entries.append(record)
            continue
        if record["connected"]:
            try:
                jobs = app.client().jobs(server=name)
                active = jobs.get("jobs", [])
                record["active_jobs"] = len(active)
                record["states"] = sorted({str(j.get("state", "?"))
                                           for j in active})
                record["scheduler_detected"] = jobs.get("scheduler")
            except Exception as exc:
                record["jobs_error"] = str(exc)[:200]
        else:
            record["active_jobs"] = 0
            record["states"] = []
        entries.append(record)
    return {"servers": entries,
            "connected": sum(1 for e in entries if e.get("connected")),
            "total": len(entries)}


def cmd_snapshot(app, args):
    return _snapshot(app)


def cmd_watch(app, args):
    """Long-running change detector: one JSON line per state change."""
    if args.interval < 5:
        raise ValidationError("watch interval must be at least 5 seconds")
    names = [n.strip() for n in args.servers.split(",") if n.strip()] \
        if args.servers != "all" else ["all"]

    def fingerprint(snapshot: dict) -> dict:
        return {e["server"]: {"connected": e.get("connected", False),
                              "active_jobs": e.get("active_jobs", 0),
                              "states": e.get("states", []),
                              "error": e.get("error", e.get("jobs_error", ""))}
                for e in snapshot.get("servers", [])}

    previous = fingerprint(_snapshot(app, names))
    emit({"ok": True, "event": "baseline", "state": previous})
    try:
        while True:
            time.sleep(args.interval)
            current = fingerprint(_snapshot(app, names))
            for server, state in current.items():
                if state != previous.get(server):
                    previous[server] = state
                    emit({"ok": True, "event": "change", "server": server,
                          "state": state})
            added = set(current) - set(previous)
            for server in added:
                previous[server] = current[server]
    except KeyboardInterrupt:
        emit({"ok": True, "event": "stopped"})
    return None
