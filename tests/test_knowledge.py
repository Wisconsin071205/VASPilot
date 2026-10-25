"""Bibliographic verification must not turn metadata into scientific proof."""
import json

import pytest

from rsci.core.errors import ValidationError
from rsci.workflow import knowledge
from tests.test_research import research, prepared


def metadata():
    return {"DOI": "10.1234/example", "title": ["An example study"],
            "author": [{"given": "Test", "family": "Author"}]}


def test_search_is_unverified_and_uses_fixed_host(monkeypatch):
    urls = []
    def fetch(url):
        urls.append(url)
        return {"items": [metadata()]}
    monkeypatch.setattr(knowledge, "_fetch", fetch)
    result = knowledge.literature_search("an example & results")
    assert result["verification_status"] == "search_results_only"
    assert result["results"][0]["doi"] == "10.1234/example"
    assert urls[0].startswith("https://api.crossref.org/works?")
    assert "%26" in urls[0]


def test_reference_history_matched_mismatch_unavailable(research, monkeypatch):
    app, store, _ = research
    ref = knowledge.add_reference(app.config, "experiment", "https://doi.org/10.1234/EXAMPLE", "An example study")
    assert ref["verification_status"] == "unverified"
    with pytest.raises(ValidationError, match="already exists"):
        knowledge.add_reference(app.config, "experiment", "10.1234/example", "different title")
    monkeypatch.setattr(knowledge, "_fetch", lambda _: metadata())
    result = knowledge.check_reference(app.config, "experiment", ref["id"])
    assert result["status"] == "metadata_matched"
    assert "未核验全文" in result["limitation"]
    monkeypatch.setattr(knowledge, "_fetch", lambda _: {**metadata(), "title": ["Wrong title"]})
    assert knowledge.check_reference(app.config, "experiment", ref["id"])["status"] == "metadata_mismatch"
    def unavailable(_):
        raise ValidationError("network unavailable")
    monkeypatch.setattr(knowledge, "_fetch", unavailable)
    assert knowledge.check_reference(app.config, "experiment", ref["id"])["status"] == "unavailable"
    record = json.loads(store.read_file("experiment", ref["file"])["content"])
    assert [c["status"] for c in record["checks"]] == ["metadata_matched", "metadata_mismatch", "unavailable"]


def test_reference_concurrent_edit_does_not_get_overwritten(research, monkeypatch):
    app, store, _ = research
    ref = knowledge.add_reference(app.config, "experiment", "10.1234/example", "An example study")
    def edited(_):
        store.write_file("experiment", ref["file"], "changed by another editor")
        return metadata()
    monkeypatch.setattr(knowledge, "_fetch", edited)
    with pytest.raises(ValidationError, match="changed"):
        knowledge.check_reference(app.config, "experiment", ref["id"])
    assert store.read_file("experiment", ref["file"])["content"] == "changed by another editor"


@pytest.mark.parametrize("doi", ["http://localhost/private", "../secret", "10.1234/a\nsecret"])
def test_reference_doi_not_an_arbitrary_url(doi):
    with pytest.raises(ValidationError):
        knowledge.normalize_doi(doi)


@pytest.mark.parametrize("row", [None, [], {"DOI": "10.1234/a", "title": []},
    {"DOI": "10.1234/a", "title": [None]}, {**metadata(), "author": 1}])
def test_malformed_metadata_fails_closed(row):
    with pytest.raises(ValidationError):
        knowledge._metadata(row)


def test_malformed_search_not_mistaken_for_no_results(monkeypatch):
    monkeypatch.setattr(knowledge, "_fetch", lambda _: {"items": {"unexpected": True}})
    with pytest.raises(ValidationError):
        knowledge.literature_search("synthetic query")


def test_experiment_binds_plan_and_separates_claims(research):
    engine, plan_id, _ = prepared(research)
    app, store, _ = research
    result = knowledge.record_experiment(app.config, "experiment", title="Synthetic experiment",
        method="CSV descriptive analysis", observations="No run yet", interpretation="Hypothesis, not measured",
        limitations="Synthetic data", plan_id=plan_id)
    record = json.loads(store.read_file("experiment", result["file"])["content"])
    assert record["run"]["execution_state"] == "NOT_STARTED"
    assert record["run"]["plan_hash"] == engine.load_plan(plan_id)["plan_hash"]
    assert record["run"]["input_hashes"]
    assert record["observations"] != record["interpretation"]
    assert "not_independently_verified" in record["provenance"]
    store.create("other")
    with pytest.raises(ValidationError, match="不属于"):
        knowledge.record_experiment(app.config, "other", title="Mismatch", plan_id=plan_id)
