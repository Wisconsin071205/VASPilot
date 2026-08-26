"""``vaspilot server ...`` — catalog, sessions, connectivity doctor."""

from __future__ import annotations

import argparse
import socket
from pathlib import Path

from ..core.config import ServerEntry
from ..core.errors import ValidationError
from ..core.validation import valid_server_name


def register(sub) -> None:
    parser = sub.add_parser("server", help="server catalog and sessions")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, handler, **kw):
        child = commands.add_parser(name, **kw)
        child.set_defaults(handler=handler)
        return child

    command("list", cmd_list)
    p = command("add", cmd_add)
    p.add_argument("name")
    p.add_argument("--target", required=True, help="user@host of the HPC login node")
    p.add_argument("--port", type=int, default=22)
    p.add_argument("--root", default="", help="absolute remote_root confining all paths")
    p.add_argument("--persist", default="8h", help="ControlPersist window (8h, 1d, ...)")
    p.add_argument("--scheduler", default="auto", choices=["auto", "slurm", "pbs"])
    p.add_argument("--set-default", action="store_true")

    p = command("edit", cmd_edit)
    p.add_argument("name")
    p.add_argument("--target")
    p.add_argument("--port", type=int)
    p.add_argument("--root")
    p.add_argument("--persist")
    p.add_argument("--scheduler", choices=["auto", "slurm", "pbs"])

    p = command("remove", cmd_remove)
    p.add_argument("name")
    p = command("connect", cmd_connect)
    p.add_argument("server", nargs="?")
    p = command("disconnect", cmd_disconnect)
    p.add_argument("server", nargs="?")
    p = command("status", cmd_status)
    p.add_argument("server", nargs="?")
    p = command("set-default", cmd_set_default)
    p.add_argument("name")
    command("doctor", cmd_doctor).add_argument(
        "server", nargs="?", help="probe this server too")


def cmd_list(app, args):
    try:
        catalog = app.client().servers()
        app.client().refresh_server_mirror()
        servers = catalog.get("servers", [])
        default = catalog.get("default", "")
    except Exception:
        # gateway unreachable: show the local mirror so users can still see
        # what is configured
        servers = [s.to_dict() for s in app.config.load_servers()]
        default = app.config.default_server()
    for entry in servers:
        entry["is_default"] = entry.get("name") == default
    return {"servers": servers, "default": default}


def cmd_add(app, args):
    valid_server_name(args.name)
    if not args.root:
        raise ValidationError(
            "server add requires --root: every remote path must be confined")
    return app.client().server_add(
        name=args.name, target=args.target, port=args.port,
        remote_root=args.root, persist=args.persist,
        scheduler=args.scheduler, set_default=args.set_default)


def cmd_edit(app, args):
    return app.client().server_edit(
        args.name, target=args.target, port=args.port,
        remote_root=args.root, persist=args.persist,
        scheduler=args.scheduler)


def cmd_remove(app, args):
    return app.client().server_remove(args.name)


def cmd_set_default(app, args):
    return app.client().server_set_default(args.name)


def cmd_connect(app, args):
    """Interactive login in the CURRENT terminal (password + TOTP here)."""
    return app.client().connect_interactive(args.server)


def cmd_disconnect(app, args):
    return app.client().disconnect(args.server)


def cmd_status(app, args):
    return app.client().status(args.server)


def cmd_doctor(app, args):
    """Read-only connectivity diagnosis. Never changes system settings."""
    report: dict = {"checks": [], "ok": True}
    vlab = app.config.vlab

    def check(name, ok, detail=""):
        report["checks"].append({"name": name, "ok": bool(ok),
                                 "detail": detail[:300]})
        if not ok:
            report["ok"] = False

    # 1) DNS
    try:
        infos = socket.getaddrinfo(vlab["host"], vlab["port"],
                                   proto=socket.IPPROTO_TCP)
        addresses = sorted({info[4][0] for info in infos})
        check("dns", True, f"{vlab['host']} -> {', '.join(addresses[:4])}")
    except OSError as exc:
        check("dns", False, str(exc))

    # 2) identity file
    identity = app.config.identity_file()
    if identity and Path(identity).expanduser().is_file():
        check("identity_file", True, identity)
    else:
        check("identity_file", False,
              "no usable PEM; set VASPILOT_IDENTITY_FILE or settings.vlab."
              "identity_file")

    # 3) Vlab reachability + gateway version (only when identity exists)
    if identity and Path(identity).expanduser().is_file():
        reachable, detail = app.transport().probe_reachable()
        check("vlab_ssh", reachable, detail)
        if reachable:
            try:
                version = app.client().version()
                check("gateway_version", True,
                      f"gateway {version.get('gateway_version')} "
                      f"protocol {version.get('protocol')}")
            except Exception as exc:
                check("gateway_version", False,
                      f"gateway script missing or broken: {exc}")
        else:
            check("gateway_version", False, "skipped: ssh unreachable")
    else:
        check("vlab_ssh", False, "skipped: no identity file")
        check("gateway_version", False, "skipped: no identity file")

    # 4) per-server session + scheduler probe
    names = [args.server] if args.server else \
        [s.name for s in app.config.load_servers()]
    server_reports = []
    for name in names:
        valid_server_name(name)
        entry: dict = {"server": name}
        try:
            status = app.client().status(name)
            entry["connected"] = bool(status.get("connected"))
            if entry["connected"]:
                sched = app.client().diagnostic("scheduler", server=name)
                entry["scheduler"] = (sched.get("output") or "").strip()[:120]
            server_reports.append(entry)
        except Exception as exc:
            entry["connected"] = False
            entry["error"] = str(exc)[:200]
            report["ok"] = False
            server_reports.append(entry)
    report["servers"] = server_reports
    report["note"] = ("doctor is read-only: it never closes proxies, edits "
                      "known_hosts or changes network settings")
    return report
