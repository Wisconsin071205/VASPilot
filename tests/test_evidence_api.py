import json
import urllib.error
import urllib.request

from tests.test_ui import ui, call
from tests.test_evidence_center import pdf_bytes


def test_authenticated_stream_import_page_and_isolation(ui):
    call(ui, "project.create", {"name": "papers"})
    base = ui["base"]
    data = pdf_bytes()
    request = urllib.request.Request(base + "/api/document/import?project=papers&title=sample",
        data=data, headers={"X-RSCI-Token": ui["token"], "Content-Type": "application/pdf"})
    with urllib.request.urlopen(request) as response:
        doc = json.load(response)
    assert doc["ok"] and doc["pages"] == 1
    listed = call(ui, "document.list", {"project": "papers"})
    assert listed["documents"][0]["document_id"] == doc["document_id"]
    body = {"project": "papers", "document_id": doc["document_id"], "page": 1}
    request = urllib.request.Request(base + "/api/document.page", data=json.dumps(body).encode(),
        headers={"X-RSCI-Token": ui["token"], "Content-Type": "application/json"})
    with urllib.request.urlopen(request) as response:
        assert response.headers["Content-Type"] == "image/png"
        assert response.read().startswith(b'\x89PNG')
    assert not call(ui, "document.get", {"project": "papers", "document_id": "../escape"})["ok"]
    request = urllib.request.Request(base + "/api/document/import?project=papers", data=data)
    try:
        urllib.request.urlopen(request)
        assert False, "unauthenticated import accepted"
    except urllib.error.HTTPError as error:
        assert error.code == 403
    with urllib.request.urlopen(base + "/t/" + ui["token"] + "/research.js") as response:
        assert "evidence-center" in response.read().decode()


def test_data_profile_admission_and_bad_model_validation(ui):
    call(ui, "project.create", {"name": "papers"})
    assert not call(ui, "literature.ask", {"project": "papers", "document_ids": [], "question": "hello"})["ok"]
    assert not call(ui, "research.profile", {"project": "papers", "file": "../outside.csv"})["ok"]
    assert not call(ui, "research.templates", {"project": "papers", "file": "data/missing.csv"})["ok"]
