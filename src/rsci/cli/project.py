"""Research project file management; writing a script never executes it."""
from pathlib import Path

from ..core.errors import ValidationError
from ..core.project_paths import MAX_IMPORT_BYTES
from ..workflow.research import project_store, prepare_csv_template


def register(sub):
    parser = sub.add_parser("project", help="general research and VASP projects")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("list")
    p.set_defaults(handler=lambda app, args: {"projects": project_store(app.config).list()})
    p = commands.add_parser("create")
    p.add_argument("name")
    p.add_argument("--kind", choices=["general", "vasp"], default="general")
    p.set_defaults(handler=lambda app, args: project_store(app.config, app.audit).create(args.name, kind=args.kind))
    from ..workflow import knowledge
    p = commands.add_parser("literature-search", help="public Crossref metadata search; does not verify claims")
    p.add_argument("query")
    p.set_defaults(handler=lambda app, args: knowledge.literature_search(args.query))
    p = commands.add_parser("reference-add")
    p.add_argument("name")
    p.add_argument("--doi", required=True)
    p.add_argument("--title", required=True)
    p.set_defaults(handler=lambda app, args: knowledge.add_reference(app.config, args.name, args.doi, args.title, audit=app.audit))
    p = commands.add_parser("reference-check", help="compare DOI/title with Crossref and record evidence, not claim verification")
    p.add_argument("name")
    p.add_argument("reference_id")
    p.set_defaults(handler=lambda app, args: knowledge.check_reference(app.config, args.name, args.reference_id, audit=app.audit))
    p = commands.add_parser("experiment")
    p.add_argument("name")
    p.add_argument("--title", required=True)
    for key in ("method", "observations", "interpretation", "limitations", "plan-id"):
        p.add_argument("--" + key, default="")
    p.set_defaults(handler=lambda app, args: knowledge.record_experiment(app.config, args.name,
        **{k: getattr(args, k) for k in ("title", "method", "observations", "interpretation", "limitations", "plan_id")}, audit=app.audit))
    for action in ("files", "read", "write", "import", "csv-template"):
        p = commands.add_parser(action)
        p.add_argument("name")
        if action != "files":
            p.add_argument("file")
        if action in {"write", "import"}:
            p.add_argument("--source", required=True, help="explicit local source file")
            p.add_argument("--expected-sha256", default=None)
        if action == "csv-template":
            p.add_argument("--script", default="scripts/analyze.py")
        p.set_defaults(handler=handle)


def handle(app, args):
    store = project_store(app.config, app.audit)
    if args.command == "files":
        return {"project": args.name, "kind": store.kind(args.name), "files": store.files(args.name)}
    if args.command == "read":
        return store.read_file(args.name, args.file)
    if args.command == "csv-template":
        return prepare_csv_template(app.config, args.name, args.file, script=args.script, audit=app.audit)
    source = Path(args.source).expanduser()
    if not source.is_file() or source.stat().st_size > MAX_IMPORT_BYTES:
        raise ValidationError("source file missing or exceeds 16 MiB")
    if args.command == "write":
        return store.write_file(args.name, args.file, source.read_text(encoding="utf-8-sig"),
                                expected_sha256=args.expected_sha256)
    return store.import_file(args.name, args.file, source.read_bytes(), expected_sha256=args.expected_sha256)
