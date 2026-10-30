"""Research project file management; writing a script never executes it."""
from pathlib import Path

from ..core.errors import ValidationError
from ..core.project_paths import MAX_IMPORT_BYTES
from ..workflow.research import project_store


def register(sub):
    parser = sub.add_parser("project", help="general research and VASP projects")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("list")
    p.set_defaults(handler=lambda app, args: {"projects": project_store(app.config).list()})
    p = commands.add_parser("create")
    p.add_argument("name")
    p.add_argument("--kind", choices=["general", "vasp"], default="general")
    p.set_defaults(handler=lambda app, args: project_store(app.config, app.audit).create(args.name, kind=args.kind))
    for action in ("files", "read", "write", "import"):
        p = commands.add_parser(action)
        p.add_argument("name")
        if action != "files":
            p.add_argument("file")
        if action in {"write", "import"}:
            p.add_argument("--source", required=True, help="explicit local source file")
            p.add_argument("--expected-sha256", default=None)
        p.set_defaults(handler=handle)


def handle(app, args):
    store = project_store(app.config, app.audit)
    if args.command == "files":
        return {"project": args.name, "kind": store.kind(args.name), "files": store.files(args.name)}
    if args.command == "read":
        return store.read_file(args.name, args.file)
    source = Path(args.source).expanduser()
    if not source.is_file() or source.stat().st_size > MAX_IMPORT_BYTES:
        raise ValidationError("source file missing or exceeds 16 MiB")
    if args.command == "write":
        return store.write_file(args.name, args.file, source.read_text(encoding="utf-8-sig"),
                                expected_sha256=args.expected_sha256)
    return store.import_file(args.name, args.file, source.read_bytes(), expected_sha256=args.expected_sha256)
