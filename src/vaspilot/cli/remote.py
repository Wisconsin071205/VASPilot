"""``vaspilot remote ...`` — confined remote file operations."""

from __future__ import annotations

from pathlib import Path

from ..core.errors import ValidationError


def register(sub) -> None:
    parser = sub.add_parser("remote", help="remote file operations")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, handler, **kw):
        child = commands.add_parser(name, **kw)
        child.set_defaults(handler=handler)
        child.add_argument("--server", default=None,
                           help="server name (default: the default server)")
        return child

    command("pwd", cmd_pwd)
    p = command("list", cmd_list); p.add_argument("path", nargs="?")
    p = command("read", cmd_read); p.add_argument("path")
    p = command("tail", cmd_tail); p.add_argument("path")
    p.add_argument("--lines", type=int, default=80)
    p = command("find", cmd_find); p.add_argument("path")
    p.add_argument("--pattern", default="*")
    p.add_argument("--max-depth", type=int, default=2)
    p.add_argument("--limit", type=int, default=200)
    p = command("stat", cmd_stat); p.add_argument("path")
    p = command("du", cmd_du); p.add_argument("path")
    p = command("upload", cmd_upload)
    p.add_argument("local_path"); p.add_argument("remote_path")
    p = command("download", cmd_download)
    p.add_argument("remote_path"); p.add_argument("local_path")
    p = command("mkdir", cmd_mkdir); p.add_argument("path")
    p = command("copy", cmd_copy); p.add_argument("path"); p.add_argument("destination")
    p = command("move", cmd_move); p.add_argument("path"); p.add_argument("destination")
    p = command("trash", cmd_trash); p.add_argument("path")
    command("trash-list", cmd_trash_list)
    p = command("restore", cmd_restore); p.add_argument("trash_id")
    p = command("purge", cmd_purge)
    p.add_argument("trash_id")
    p.add_argument("--confirm-trash-id", dest="confirm_trash_id", required=True,
                   help="must repeat the trash id exactly (double match)")


def cmd_pwd(app, args):
    return app.client().pwd(args.server)


def cmd_list(app, args):
    path = args.path or "."
    return app.client().list_dir(path, server=args.server)


def cmd_read(app, args):
    from ..hpc.vasp import denied_text_file
    if denied_text_file(Path(args.path).name):
        raise ValidationError(
            f"{Path(args.path).name} is a protected scientific file and "
            "cannot be read as text")
    return app.client().read(args.path, server=args.server)


def cmd_tail(app, args):
    return app.client().tail(args.path, lines=args.lines, server=args.server)


def cmd_find(app, args):
    return app.client().find(args.path, pattern=args.pattern,
                             max_depth=args.max_depth, limit=args.limit,
                             server=args.server)


def cmd_stat(app, args):
    return app.client().stat(args.path, server=args.server)


def cmd_du(app, args):
    return app.client().du(args.path, server=args.server)


def cmd_upload(app, args):
    local = Path(args.local_path).expanduser()
    if not local.is_file():
        raise ValidationError(f"local file not found: {local}")
    return app.client().upload(local, args.remote_path, server=args.server)


def cmd_download(app, args):
    local = Path(args.local_path).expanduser()
    if local.exists():
        raise ValidationError(
            f"local target {local} exists; downloads never overwrite")
    return app.client().download(args.remote_path, local, server=args.server)


def cmd_mkdir(app, args):
    return app.client().mkdir(args.path, server=args.server)


def cmd_copy(app, args):
    return app.client().copy(args.path, args.destination, server=args.server)


def cmd_move(app, args):
    return app.client().move(args.path, args.destination, server=args.server)


def cmd_trash(app, args):
    """Move a remote path into the recoverable trash (default for delete)."""
    return app.client().remove(args.path, server=args.server)


def cmd_trash_list(app, args):
    return app.client().trash_list(server=args.server)


def cmd_restore(app, args):
    return app.client().restore(args.trash_id, server=args.server)


def cmd_purge(app, args):
    """Irreversibly destroy one trash entry; requires a double-matched id."""
    return app.client().purge(args.trash_id, args.confirm_trash_id,
                              server=args.server)
