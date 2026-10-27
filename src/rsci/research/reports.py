"""Deterministic reports and hash-verifiable, allowlisted reproduction bundles."""
from __future__ import annotations

import hashlib
import html
import json
import secrets
import zipfile
from pathlib import Path

from ..core.errors import ValidationError
from ..core.hashing import file_sha256
from ..core.project_paths import confined_path, atomic_bytes, relative_name
from .common import identifier, location, project, read, save, stamp


def _ids(values):
    if not isinstance(values, list) or len(values) > 30 or len(set(values)) != len(values):
        raise ValidationError("每类最多选择 30 个不重复的记录")
    return [identifier(v) for v in values]


def create(config, name, question, note_ids=None, profile_task_ids=None, plan_ids=None, include_pdfs=False):
    from ..cli.main import App
    from .literature import note_get
    from .documents import get as document_get
    from .tasks import get as task_get
    from ..workflow.local_python import paths
    question = str(question).strip()
    if not 1 <= len(question) <= 2000:
        raise ValidationError("研究问题需要 1–2000 字")
    note_ids, profile_task_ids, plan_ids = _ids(note_ids or []), _ids(profile_task_ids or []), _ids(plan_ids or [])
    if not any((note_ids, profile_task_ids, plan_ids)):
        raise ValidationError("请至少选择一项证据、数据检查或分析计划")
    _, root, pid = project(config, name)
    report_id = secrets.token_hex(16)
    base = location(config, name, f"results/reports/{report_id}")
    payloads, input_hashes, evidence, notes, profiles, analyses = {}, {}, {}, [], [], []
    def add(relative, data):
        relative_name(relative)
        if len(data) > 50 * 1024 * 1024 or sum(map(len, payloads.values())) + len(data) > 512 * 1024 * 1024:
            raise ValidationError("复现包超过限制（单文件 50 MiB，总计 512 MiB）")
        payloads[relative] = data
    def add_json(relative, value):
        add(relative, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode())
    for note_id in note_ids:
        row = note_get(config, name, note_id)
        notes.append(row)
        add_json(f"evidence/notes/{note_id}.json", row)
        for citation in row["citations"]:
            evidence[citation["citation_id"]] = citation
    for task_id in profile_task_ids:
        row = task_get(config, task_id)
        if row["project_id"] != pid or row["kind"] != "profile" or row["status"] != "completed":
            raise ValidationError("只能关联当前项目已完成的数据检查")
        result = row["result"]
        profiles.append(result)
        input_hashes[result["file"]] = result["input_sha256"]
        add_json(f"data-checks/{task_id}.json", result)
    engine = App(config).engine()
    for plan_id in plan_ids:
        plan = engine.load_plan(plan_id)
        belongs = plan.get("project_id") == pid if plan.get("project_id") else plan.get("project") == name and plan.get("project_root") == str(root)
        if plan.get("domain") != "general" or not belongs:
            raise ValidationError("分析计划不属于当前通用科研项目")
        state = engine._load_run(plan_id) or {}
        summary = {"plan_id": plan_id, "plan_hash": plan["plan_hash"], "script": plan["execution"]["script"],
                   "args": plan["execution"].get("args", []), "inputs": [], "outputs": [],
                   "status": state.get("status", "not_run"), "execution_state": state.get("execution_state", "not_run"),
                   "collection_status": state.get("collection_status", "not_collected"), "scientific_status": "not_assessed"}
        workspace = paths(engine, plan)[0]
        if state:
            for item in plan["files"]:
                source = confined_path(workspace, item["name"])
                if not source.is_file() or file_sha256(source) != item["sha256"]:
                    raise ValidationError("分析输入快照缺失或改变，无法生成可信复现包")
                add(f"analyses/{plan_id}/" + item["name"], source.read_bytes())
                input_hashes[item["name"]] = item["sha256"]
                summary["inputs"].append({"file": item["name"], "sha256": item["sha256"]})
            for artifact in (state.get("task") or {}).get("artifacts", []):
                if "results/" + artifact["name"] not in plan["outputs"]:
                    raise ValidationError("运行记录包含未声明的输出")
                source = confined_path(root, artifact["project_file"])
                if not source.is_file() or file_sha256(source) != artifact["sha256"]:
                    raise ValidationError("分析结果哈希不匹配，请先核对运行结果")
                add(f"analyses/{plan_id}/results/" + artifact["name"], source.read_bytes())
                summary["outputs"].append({"file": artifact["name"], "sha256": artifact["sha256"]})
                if artifact["name"] == "statistics.json" and source.stat().st_size <= 1024 * 1024:
                    calculated = read(source)
                    summary["numerical_results"] = {k: calculated.get(k) for k in
                        ("n", "input_rows", "dropped_missing_rows", "statistics", "limitations", "dependencies")}
        else:
            summary["limitation"] = "计划尚未执行，没有可回收的运行结果。"
        # Never export execution receipts, approval tokens, provider configuration or environment.
        add_json(f"analyses/{plan_id}/run.json", summary)
        analyses.append(summary)
    for doc_id in {c["document_id"] for c in evidence.values()}:
        doc = document_get(config, name, doc_id)
        add_json(f"evidence/documents/{doc_id}.json", {k: doc[k] for k in ("document_id", "sha256", "title", "pages")})
        if include_pdfs:
            add(f"papers/{doc_id}.pdf", location(config, name, doc["source"]).read_bytes())
    add_json("evidence/citations.json", list(evidence.values()))
    report = {"schema": "rsci.report/1", "report_id": report_id, "project_id": pid, "question": question,
              "created_at": stamp(), "note_ids": note_ids, "profile_task_ids": profile_task_ids, "plan_ids": plan_ids,
              "input_hashes": input_hashes, "documents": [d for n in notes for d in n["documents"]],
              "include_pdfs": bool(include_pdfs), "analyses": analyses,
              "limitations": ["文献问答和解释是模型草稿，引用仅验证出处，不验证科学结论。",
                              "OCR、单位、数据独立性及统计假设需要人工核对。",
                              "复现包校验检查文件一致性，不自动执行任何脚本。"]}
    sections = [("研究问题", question), ("文献证据", [n for n in notes if n["kind"] == "ask"]), ("论文比较与术语", [n for n in notes if n["kind"] != "ask"]),
                ("数据检查", profiles), ("分析方法与运行结果", analyses), ("局限性", report["limitations"])]
    markdown = ["# " + question.replace('\n', ' '), "", "生成时间：" + report["created_at"], ""]
    web = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
           '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:">',
           '<title>' + html.escape(question) + '</title><style>body{font:16px/1.65 system-ui,sans-serif;max-width:960px;margin:40px auto;padding:0 24px;color:#172334;background:white}h2{margin-top:40px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f6fa;padding:16px}a{color:#165bb6}details{padding:12px 0;border-bottom:1px solid #ccd5e1}blockquote{margin-left:0;padding:12px;background:#f3f6fa}</style><h1>' + html.escape(question) + '</h1>']
    for heading, value in sections:
        if heading in {"文献证据", "论文比较与术语"} and value:
            md, markup = _notes(value)
            markdown += ["## " + heading, "", md, ""]
            web += ['<h2>' + heading + '</h2>', markup]
            continue
        if isinstance(value, str):
            text = value
        elif not value:
            text = "未提供，未作推断。"
        else:
            text = json.dumps(value, ensure_ascii=False, indent=2)
        markdown += ["## " + heading, "", text, ""]
        web += ['<h2>' + html.escape(heading) + '</h2><pre>' + html.escape(text) + '</pre>']
    markdown += ["## 参考文献与可核对出处", ""]
    web.append('<h2>参考文献与可核对出处</h2><nav>')
    for key, citation in evidence.items():
        title = citation["title"] + f" · 第 {citation['page']} 页"
        markdown += [f"[{key}] {title}", "", "> " + citation["text"].replace('\n', '\n> '), ""]
        web.append(f'<a href="#{html.escape(key)}">{html.escape(title)}</a> · ')
    web.append('</nav>')
    for key, citation in evidence.items():
        web.append(f'<details id="{html.escape(key)}"><summary>{html.escape(citation["title"])} · 第 {citation["page"]} 页</summary><blockquote>{html.escape(citation["text"])}</blockquote><p>原文件 SHA-256：{citation["document_id"]}</p></details>')
    web.append('</html>')
    add("report.md", "\n".join(markdown).encode())
    add("report.html", "\n".join(web).encode())
    add_json("report.json", report)
    add("README.txt", "本包只包含显式选择的证据与分析快照。先运行 rsci research verify <本包.zip> 校验。\n分析脚本需要人工审阅、准备相应依赖，并通过 RSCI 重新生成计划和审批后运行。\n没有自动运行脚本。\n".encode())
    manifest = {"schema": "rsci.reproduction/1", "files": {n: hashlib.sha256(b).hexdigest() for n, b in payloads.items()}}
    base.mkdir(parents=True, exist_ok=True)
    # Export only the two human-readable files plus ZIP; internal bundle paths stay inside ZIP.
    for filename in ("report.md", "report.html"):
        atomic_bytes(base / filename, payloads[filename])
    temporary = base / "bundle.tmp"
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, data in payloads.items():
            archive.writestr(filename, data)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    temporary.replace(base / "bundle.zip")
    report["export_hashes"] = {n: file_sha256(base / n) for n in ("report.md", "report.html", "bundle.zip")}
    save(base / "metadata.json", report)
    return {"report_id": report_id, "question": question, "files": ["report.md", "report.html", "bundle.zip"]}


def _notes(notes):
    """Render model text as text; only server-generated anchors become markup."""
    markdown, web = [], []
    def claim(value):
        text = str(value["text"])
        refs = value.get("citations", [])
        markdown.append(text + " " + " ".join("[" + c + "]" for c in refs))
        web.append('<p>' + html.escape(text) + ' ' + ' '.join(
            '<a href="#' + html.escape(c) + '">查看出处</a>' for c in refs) + '</p>')
    for note in notes:
        heading = note["question"] or {"compare": "论文比较", "glossary": "项目术语表", "ask": "文献问答"}[note["kind"]]
        markdown.append("### " + heading)
        web.append('<h3>' + html.escape(heading) + '</h3><p>模型草稿 · ' + html.escape(note["disclosure"]["model"]) + '</p>')
        checks = "DOI 元数据：未核验；原文出处：引用片段已匹配；科学结论：未评估。"
        markdown.append(checks)
        web.append('<p>' + checks + '</p>')
        if note["stale"]:
            markdown.append("来源已更新，需要重新核对。")
            web.append('<p>来源已更新，需要重新核对。</p>')
        content = note["content"]
        if content.get("insufficient_evidence"):
            markdown.append("证据不足，不能据此作出完整结论。")
            web.append('<p>证据不足，不能据此作出完整结论。</p>')
        for limitation in content.get("limitations", []):
            if isinstance(limitation, dict):
                claim(limitation)
            else:
                markdown.append("局限：" + str(limitation))
                web.append('<p>局限：' + html.escape(str(limitation)) + '</p>')
        for item in content.get("claims", []):
            claim(item)
        for row in content.get("rows", []):
            title = next((c["title"] for c in note["citations"] if c["document_id"] == row["document_id"]), row["document_id"])
            markdown.append("#### " + title)
            web.append('<h4>' + html.escape(title) + '</h4>')
            for column, item in row["cells"].items():
                markdown.append("**" + column + "**")
                web.append('<strong>' + html.escape(column) + '</strong>')
                claim(item)
        for term in content.get("terms", []):
            title = ' · '.join(filter(None, [term["term"], term["english"], term["abbreviation"]]))
            markdown.append("#### " + title)
            web.append('<h4>' + html.escape(title) + '</h4>')
            claim(term["definition"])
        for edit in note.get("manual_edits", []):
            markdown.extend(["人工修订（未经出处校验）", edit["text"]])
            web.append('<details><summary>人工修订（未经出处校验）</summary><p>' + html.escape(edit["text"]) + '</p></details>')
    return '\n\n'.join(markdown), '\n'.join(web)


def get(config, name, report_id):
    row = read(location(config, name, f"results/reports/{identifier(report_id)}/metadata.json"))
    if row.get("project_id") != project(config, name)[2]:
        raise ValidationError("报告不属于当前项目")
    stale = False
    for filename, digest in row["input_hashes"].items():
        source = location(config, name, filename)
        stale |= not source.is_file() or file_sha256(source) != digest
    from .documents import get as document_get
    for doc in row["documents"]:
        try:
            stale |= document_get(config, name, doc["document_id"])["revision"] != doc["revision"]
        except ValidationError:
            stale = True
    return {**row, "stale": stale}


def export_path(config, name, report_id, format):
    row = get(config, name, report_id)
    filename = {"md": "report.md", "html": "report.html", "zip": "bundle.zip"}.get(format)
    if not filename:
        raise ValidationError("format must be md, html or zip")
    target = location(config, name, f"results/reports/{report_id}/{filename}")
    if not target.is_file() or file_sha256(target) != row["export_hashes"][filename]:
        raise ValidationError("导出文件已改变，不能当作原报告导出")
    return target


def verify(path):
    errors = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or len(names) > 2000 or sum(i.file_size for i in archive.infolist()) > 512 * 1024 * 1024:
                raise ValueError("duplicate entries or oversized bundle")
            for name in names:
                relative_name(name)
            if archive.getinfo("manifest.json").file_size > 1024 * 1024:
                raise ValueError("oversized manifest")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("schema") != "rsci.reproduction/1":
                raise ValueError("unsupported manifest")
            expected = manifest["files"]
            if set(names) != set(expected) | {"manifest.json"}:
                errors.append("清单与压缩包文件集合不一致")
            for name, digest in expected.items():
                if name not in names:
                    errors.append("缺失：" + name)
                    continue
                hashed = hashlib.sha256()
                with archive.open(name) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        hashed.update(chunk)
                if hashed.hexdigest() != digest:
                    errors.append("哈希不匹配：" + name)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ValidationError) as exc:
        errors.append(str(exc))
    return {"valid": not errors, "errors": errors, "execution_performed": False}
