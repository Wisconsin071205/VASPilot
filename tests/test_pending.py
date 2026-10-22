from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading

import pytest

from rsci.core.errors import ValidationError
from rsci.workflow import pending
from rsci.workflow.pending import PendingSubmitStore


def test_one_claim_across_independent_store_instances(tmp_path):
    path = tmp_path / "pending.json"
    entry = PendingSubmitStore(path).create(server="cl9", directory="/case", script="run.sh")
    ready = threading.Barrier(2)

    def claim():
        ready.wait(timeout=5)
        try:
            return PendingSubmitStore(path).claim(entry["id"])["status"]
        except ValidationError:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(lambda _: claim(), range(2))) == ["refused", "submitting"]


def test_crashed_claim_does_not_expire_back_to_retryable(tmp_path, monkeypatch):
    path = tmp_path / "pending.json"
    store = PendingSubmitStore(path)
    entry = store.create(server="cl9", directory="/case", script="run.sh")
    store.claim(entry["id"])
    later = pending._utcnow() + timedelta(days=2)
    monkeypatch.setattr(pending, "_utcnow", lambda: later)
    restarted = PendingSubmitStore(path)
    assert restarted.get(entry["id"])["status"] == "submitting"
    with pytest.raises(ValidationError):
        restarted.claim(entry["id"])
    with pytest.raises(ValidationError):
        restarted.regenerate(entry["id"])


def test_uncertain_submission_is_visible_and_never_reclaimed(tmp_path, monkeypatch):
    monkeypatch.setattr(pending, "MAX_ENTRIES", 1)
    store = PendingSubmitStore(tmp_path / "pending.json")
    entry = store.create(server="cl9", directory="/case", script="run.sh")
    store.claim(entry["id"])
    store.finish_submission(entry["id"], error="response lost after scheduler accepted")
    store.create(server="cl9", directory="/other", script="run.sh")
    assert store.get(entry["id"])["status"] == "uncertain"
    assert entry["id"] in {row["id"] for row in store.history()}
    with pytest.raises(ValidationError):
        store.claim(entry["id"])
    with pytest.raises(ValidationError):
        store.settle(entry["id"], "rejected")
