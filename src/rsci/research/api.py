"""Shared UI/CLI service dispatch, with bounded validated admissions."""
from . import documents, tasks, ocr_runtime
from .common import project, location, identifier
from ..core.errors import ValidationError


def dispatch(app, action, body):
    config, name = app.config, str(body.get("project") or body.get("name") or "")
    if action == "ocr.status":
        return ocr_runtime.status(config)
    if action.startswith("research_task."):
        command = action.split('.')[1]
        if command == "list":
            return {"tasks": tasks.listing(config, name or None)}
        return {"get": tasks.get, "resume": tasks.resume, "cancel": tasks.cancel}[command](config, str(body.get("task_id", "")))
    project(config, name)
    if action == "document.list":
        return {"documents": documents.list_documents(config, name)}
    if action == "document.get":
        return documents.get(config, name, str(body.get("document_id", "")))
    if action == "document.parse":
        doc = documents.get(config, name, str(body.get("document_id", "")))
        return tasks.enqueue(config, name, "parse", {"document_id": doc["document_id"]})
    if action == "document.evidence":
        return documents.evidence(config, name, str(body.get("document_id", "")), body.get("revision"))
    if action == "ocr.install":
        params = {}
        if body.get("offline_bundle"):
            from pathlib import Path
            source = Path(str(body["offline_bundle"])).expanduser()
            if not source.is_absolute() or not source.is_file() or source.suffix.lower() != ".zip":
                raise ValidationError("请选择本机 OCR 组件 ZIP 文件")
            params["offline_bundle"] = str(source.resolve())
        return tasks.enqueue(config, name, "install_ocr", params)
    if action in {"literature.ask", "literature.compare", "literature.glossary"}:
        from .literature import prepare
        kind = action.split('.')[1]
        params = prepare(config, name, kind, body.get("document_ids"), body.get("question", ""), body.get("provider", ""))
        return tasks.enqueue(config, name, kind, params, request_id=body.get("request_id"))
    if action == "literature.note_get":
        from .literature import note_get
        return note_get(config, name, body.get("note_id", ""))
    if action == "literature.note_edit":
        from .literature import note_edit
        return note_edit(config, name, body.get("note_id", ""), body.get("text"), body.get("expected_sha256"), body.get("content"))
    if action.startswith("research."):
        from .analysis import profile, prepare_template
        if action == "research.profile":
            from ..core.hashing import file_sha256
            from ..core.project_paths import MAX_IMPORT_BYTES
            source = project(config, name)[0]._file(name, str(body.get("file", "")))
            if not source.is_file() or source.suffix.lower() not in {".csv", ".tsv"} or source.stat().st_size > MAX_IMPORT_BYTES:
                raise ValidationError("请选择不超过 16 MiB 的 CSV/TSV")
            digest = file_sha256(source)
            return tasks.enqueue(config, name, "profile", {"file": str(body.get("file", "")),
                "label_column": str(body.get("label_column", "")), "split_column": str(body.get("split_column", "")), "expected_sha256": digest})
        if action == "research.templates":
            return prepare_template(config, name, str(body.get("file", "")), str(body.get("template", "")),
                body.get("columns"), str(body.get("missing", "error")))
    if action.startswith("report."):
        from . import reports
        if action == "report.create":
            return tasks.enqueue(config, name, "report", {"question": str(body.get("question", "")),
                "note_ids": body.get("note_ids", []), "profile_task_ids": body.get("profile_task_ids", []),
                "plan_ids": body.get("plan_ids", []), "include_pdfs": body.get("include_pdfs") is True})
        if action == "report.get":
            return reports.get(config, name, str(body.get("report_id", "")))
        if action == "report.verify":
            return reports.verify(reports.export_path(config, name, str(body.get("report_id", "")), "zip"))
    raise ValidationError("unknown research action")
