"""Bibliographic evidence and experiment notes, separate from scientific claims.

Crossref checks are deliberately narrow: DOI/title metadata matching, never
full-text verification, peer-review certification or validation of a claim.
The only network destination is the fixed public Crossref metadata API.
"""
from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from ..core.errors import ValidationError
from ..core.hashing import obj_sha256
from .research import project_store

API = "https://api.crossref.org/works"
LIMITATION = "仅核对 DOI 和标题元数据；未核验全文、引述是否支持论点或研究质量。非 Crossref 登记的 DOI 需另行核对。"


def _stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_doi(value):
    doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", str(value).strip(), flags=re.I)
    if len(doi) > 256 or not re.fullmatch(r"10\.\d{4,9}/[^\s\x00-\x1f]+", doi):
        raise ValidationError("请输入 DOI，例如 10.xxxx/xxxx；不能用普通网页地址代替。")
    return doi.lower()


def _text(value, limit, label, *, required=False):
    if not isinstance(value, str) or len(value) > limit or "\0" in value or (required and not value.strip()):
        raise ValidationError(f"invalid {label} (maximum {limit} characters)")
    return value.strip()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValidationError("Crossref 返回重定向；未访问其他地址，请稍后重试。")


def _fetch(url):
    # Callers construct a fixed-host URL; users never supply a fetch target.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "RSCI-research/1.5",
                                                   "Accept": "application/json"})
    try:
        with opener.open(request, timeout=12) as response:
            raw = response.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            raise ValidationError("Crossref 响应过大，未核验；请缩小查询。")
        doc = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise ValidationError("Crossref 暂时不可用、限流或未找到记录；这不证明文献不存在。") from exc
    if not isinstance(doc, dict) or doc.get("status") != "ok" or not isinstance(doc.get("message"), dict):
        raise ValidationError("Crossref 响应格式无法确认，未核验。")
    return doc["message"]


def _metadata(row):
    if not isinstance(row, dict):
        raise ValidationError("Crossref 条目格式无法确认。")
    doi = normalize_doi(row.get("DOI", ""))
    titles = row.get("title") or []
    if not isinstance(titles, list) or not titles or not isinstance(titles[0], str) or not titles[0].strip():
        raise ValidationError("Crossref 条目缺少可核对的标题。")
    title = titles[0][:1000]
    author_rows = row.get("author") or []
    if not isinstance(author_rows, list):
        raise ValidationError("Crossref 作者字段格式无法确认。")
    authors = [" ".join(str(a.get(k) or "") for k in ("given", "family")).strip()
               for a in author_rows[:30] if isinstance(a, dict)]
    return {"doi": doi, "title": html.unescape(re.sub(r"<[^>]*>", "", title)),
            "authors": authors, "source": "Crossref", "url": "https://doi.org/" + urllib.parse.quote(doi, safe="/")}


def literature_search(query):
    query = _text(query, 400, "query", required=True)
    url = API + "?" + urllib.parse.urlencode({"query.bibliographic": query, "rows": 5,
        "select": "DOI,title,author"})
    response = _fetch(url)
    rows = []
    items = response.get("items")
    if not isinstance(items, list):
        raise ValidationError("Crossref 搜索结果格式无法确认，请稍后重试。")
    for item in items[:5]:
        if not isinstance(item, dict):
            continue
        try:
            rows.append(_metadata(item))
        except ValidationError:
            continue
    return {"query": query, "source": "Crossref", "retrieved_at": _stamp(), "results": rows,
            "verification_status": "search_results_only", "limitation": LIMITATION}


def _store(config, project, audit=None):
    store = project_store(config, audit)
    if store.kind(project) != "general":
        raise ValidationError("文献与实验记录需要通用科研项目。")
    return store


def add_reference(config, project, doi, title, *, audit=None):
    store = _store(config, project, audit)
    doi = normalize_doi(doi)
    title = _text(title, 1000, "reference title", required=True)
    ref_id = obj_sha256({"doi": doi})[:16]
    record = {"schema": "rsci.reference/1", "id": ref_id, "doi": doi, "title": title,
              "created_at": _stamp(), "verification_status": "unverified", "checks": [],
              "limitation": LIMITATION}
    file = f"references/{ref_id}.json"
    saved = store.write_file(project, file, json.dumps(record, ensure_ascii=False, indent=2), expected_sha256="missing")
    return {**record, "file": file, "sha256": saved["sha256"]}


def check_reference(config, project, ref_id, *, audit=None):
    if not re.fullmatch(r"[0-9a-f]{16}", str(ref_id)):
        raise ValidationError("invalid reference id")
    store = _store(config, project, audit)
    file = f"references/{ref_id}.json"
    current = store.read_file(project, file)
    try:
        record = json.loads(current["content"])
        doi = normalize_doi(record["doi"])
        title = _text(record["title"], 1000, "reference title", required=True)
        if record["schema"] != "rsci.reference/1" or obj_sha256({"doi": doi})[:16] != ref_id:
            raise ValueError("reference identity changed")
        if not isinstance(record.get("checks"), list):
            raise ValueError("invalid check history")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("文献记录损坏，已保留，请人工核对。") from exc
    if len(record["checks"]) >= 100:
        raise ValidationError("此条目已保留 100 次核验记录，请归档后再核对。")
    check = {"checked_at": _stamp(), "source_url": API + "/" + urllib.parse.quote(doi, safe=""),
             "expected_doi": doi, "expected_title": title, "limitation": LIMITATION}
    try:
        metadata = _metadata(_fetch(check["source_url"]))
        normalize = lambda s: " ".join(html.unescape(s).casefold().split())
        matches = {"doi": metadata["doi"] == doi, "title": normalize(metadata["title"]) == normalize(title)}
        check.update(status="metadata_matched" if all(matches.values()) else "metadata_mismatch",
                     matches=matches, metadata=metadata, metadata_sha256=obj_sha256(metadata))
    except ValidationError as exc:
        check.update(status="unavailable", detail=exc.message)
    record["checks"].append(check)
    record["verification_status"] = check["status"]
    saved = store.write_file(project, file, json.dumps(record, ensure_ascii=False, indent=2), expected_sha256=current["sha256"])
    return {"file": file, "sha256": saved["sha256"], **check}


def record_experiment(config, project, *, title, method="", observations="", interpretation="", limitations="", plan_id="", audit=None):
    store = _store(config, project, audit)
    record = {"schema": "rsci.experiment/1", "recorded_at": _stamp(),
              "title": _text(title, 200, "experiment title", required=True),
              "method": _text(method, 8000, "method"), "observations": _text(observations, 8000, "observations"),
              "interpretation": _text(interpretation, 8000, "interpretation"),
              "limitations": _text(limitations, 8000, "limitations"),
              "provenance": "user_or_agent_entered_not_independently_verified"}
    if plan_id:
        from ..cli.main import App
        engine = App(config).engine()
        plan = engine.load_plan(plan_id)
        if plan.get("domain") != "general" or plan.get("project") != project:
            raise ValidationError("实验关联计划不属于此通用项目。")
        run = engine._load_run(plan_id)
        record["run"] = {"plan_id": plan_id, "plan_hash": plan["plan_hash"],
                         "execution_state": (run or {}).get("execution_state", "NOT_STARTED"),
                         "input_hashes": [{"name": row["name"], "sha256": row["sha256"]} for row in plan["files"]],
                         "artifacts": ((run or {}).get("task") or {}).get("artifacts", [])}
    # Unique immutable note; updates use normal hash-checked project editing.
    import secrets
    file = f"notes/experiment-{secrets.token_hex(8)}.json"
    result = store.write_file(project, file, json.dumps(record, ensure_ascii=False, indent=2), expected_sha256="missing")
    return {"file": file, "sha256": result["sha256"], "record": record}
