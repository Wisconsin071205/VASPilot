"""Research CLI uses exactly the same services as the desktop console."""
import json
from pathlib import Path

from ..research.api import dispatch
from ..research import tasks, documents
from ..core.errors import ValidationError


def register(sub):
    p = sub.add_parser("research-task", help="persistent document and research tasks")
    commands = p.add_subparsers(dest="command", required=True)
    for command in ("list", "get", "resume", "cancel", "_worker"):
        q = commands.add_parser(command)
        if command != "list":
            q.add_argument("task_id")
        else:
            q.add_argument("--project", default="")
        q.set_defaults(handler=task_command)
    p = sub.add_parser("research", help="documents, evidence, data profiles and reports")
    commands = p.add_subparsers(dest="command", required=True)
    p = commands.add_parser("call", help="invoke a research API with a JSON body")
    p.add_argument("action")
    p.add_argument("--json", default="{}")
    p.set_defaults(handler=lambda app, args: dispatch(app, args.action, json.loads(args.json)))
    p = commands.add_parser("import")
    p.add_argument("project"); p.add_argument("source"); p.add_argument("--title", default="")
    p.set_defaults(handler=import_document)
    p = commands.add_parser("install-ocr")
    p.add_argument("project"); p.add_argument("--offline-bundle")
    p.set_defaults(handler=lambda app, args: tasks.enqueue(app.config, args.project, "install_ocr", {"offline_bundle": str(Path(args.offline_bundle).resolve())} if args.offline_bundle else {}))
    p = commands.add_parser("verify")
    p.add_argument("bundle")
    p.set_defaults(handler=verify_bundle)
    p = commands.add_parser("export")
    p.add_argument("project"); p.add_argument("report_id"); p.add_argument("--format", choices=["md", "html", "zip"], default="zip")
    p.set_defaults(handler=export_report)
    for command in ("documents", "document", "parse", "page", "ask", "compare", "glossary", "profile", "template", "report", "report-get"):
        p = commands.add_parser(command)
        p.add_argument("project")
        if command in {"document", "parse", "page"}:
            p.add_argument("document_id")
        if command == "page":
            p.add_argument("page", type=int)
        if command in {"ask", "compare", "glossary"}:
            p.add_argument("--document", dest="document_ids", action="append", required=True)
            p.add_argument("--question", default="")
            p.add_argument("--provider", default="")
        if command in {"profile", "template"}:
            p.add_argument("file")
        if command == "profile":
            p.add_argument("--label-column", default=""); p.add_argument("--split-column", default="")
        if command == "template":
            p.add_argument("--template", choices=["descriptive", "correlation", "regression"], required=True)
            p.add_argument("--column", dest="columns", action="append", required=True)
            p.add_argument("--missing", choices=["error", "drop"], default="error")
        if command == "report":
            p.add_argument("--question", required=True)
            for option, dest in (("note", "note_ids"), ("profile-task", "profile_task_ids"), ("plan", "plan_ids")):
                p.add_argument("--" + option, dest=dest, action="append", default=[])
            p.add_argument("--include-pdfs", action="store_true")
        if command == "report-get":
            p.add_argument("report_id")
        p.set_defaults(handler=named_command)


def named_command(app, args):
    if args.command == "page":
        return {"path": str(documents.render_page(app.config, args.project, args.document_id, args.page))}
    action = {"documents": "document.list", "document": "document.get", "parse": "document.parse",
              "ask": "literature.ask", "compare": "literature.compare", "glossary": "literature.glossary",
              "profile": "research.profile", "template": "research.templates", "report": "report.create", "report-get": "report.get"}[args.command]
    body = {k: v for k, v in vars(args).items() if k not in {"handler", "command", "group", "config_dir"}}
    return dispatch(app, action, body)


def import_document(app, args):
    source = Path(args.source).expanduser()
    with source.open("rb") as handle:
        return documents.import_stream(app.config, args.project, handle, source.stat().st_size, args.title or source.stem)


def task_command(app, args):
    if args.command == "_worker":
        tasks.execute(app.config, args.task_id)
        return None
    body = {"project": args.project} if args.command == "list" else {"task_id": args.task_id}
    return dispatch(app, "research_task." + args.command, body)


def verify_bundle(app, args):
    from ..research.reports import verify
    return verify(Path(args.bundle))


def export_report(app, args):
    from ..research.reports import export_path
    return {"path": str(export_path(app.config, args.project, args.report_id, args.format))}
