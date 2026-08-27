"""Minute-bucket store, TSV merging, heat-map aggregation, usage积分."""

from __future__ import annotations

import time

import pytest

from vaspilot.core.metricsstore import (DEFAULT_EXCLUDED_USERS,
                                        KEEP_DAYS, MetricsStore)


@pytest.fixture()
def store(tmp_path):
    return MetricsStore(tmp_path / "metrics")


# ------------------------------------------------------------------ buckets
def test_append_sample_creates_and_widen_buckets(store):
    now = int(time.time()) // 60 * 60 + 30      # mid-minute: no bucket roll
    store.append_sample("cl9", ts=now, cpu_pct=30.0, mem_pct=40.0,
                        gpus=[{"index": 0, "util_pct": 10,
                               "mem_used_gb": 1.0, "mem_total_gb": 40}])
    bucket = store.latest("cl9")
    assert bucket["c"] == [30.0, 30.0]
    assert bucket["m"] == [40.0, 40.0]
    assert bucket["g"]["0"][:2] == [10.0, 10.0]
    # same minute, different values -> range widens instead of new row
    store.append_sample("cl9", ts=now + 5, cpu_pct=80.0, mem_pct=None,
                        gpus=[{"index": 0, "util_pct": 60,
                               "mem_used_gb": 2.0, "mem_total_gb": 40}])
    rows = [r for r in _rows(store._hist_path("cl9"))]
    assert len(rows) == 1
    assert rows[0]["c"] == [30.0, 80.0]
    assert rows[0]["g"]["0"][:4] == [10.0, 60.0, 1024.0, 2048.0]


def test_append_sample_tolerant_to_missing_gpu(store):
    store.append_sample("node", cpu_pct=None, mem_pct=12.0, gpus=[])
    bucket = store.latest("node")
    assert bucket["g"] == {}
    assert bucket["c"] == [None, None]


def test_merge_hist_tsv_dedups_and_accumulates(store):
    base = int(time.time()) // 60 * 60          # recent, survives pruning
    text = (f"{base - 60}|11.0|22.0|0:5:100:40000\n"
            f"{base}|12.0|23.0|0:6:110:40000;1:7:120:40000")
    first = store.merge_hist_tsv("cl9", text)
    assert first == {"added": 2, "duplicate": 0}
    second = store.merge_hist_tsv("cl9", text)
    assert second["added"] == 0 and second["duplicate"] == 2
    rows = _rows(store._hist_path("cl9"))
    assert [r["t"] for r in rows] == [base - 60, base]
    assert rows[-1]["g"]["1"] == [7.0, 7.0, 120.0, 120.0]


def test_merge_ignores_garbage_lines(store):
    recent = int(time.time()) // 60 * 60
    result = store.merge_hist_tsv(
        "cl9", f"not-a-number|x|y\nalso|bad\n\n{recent}|5.0|6.0|0:1:2:3\n")
    assert result["added"] == 1


def test_prune_drops_rows_older_than_keep_days(store):
    old = int(time.time()) - (KEEP_DAYS + 2) * 86400
    fresh = int(time.time()) - 3600
    store.merge_hist_tsv("cl9", f"{old}|1.0|2.0|0:0:0:1\n"
                                f"{fresh}|3.0|4.0|0:1:1:1\n")
    rows = _rows(store._hist_path("cl9"))
    assert [r["t"] for r in rows] == [fresh // 60 * 60]


# ------------------------------------------------------------------ heatmap
def test_heatmap_slot_averages(store):
    base = int(time.time() // 86400 * 86400)          # today (UTC midnight)
    noon = base + 13 * 3600                            # slot 4 (12-15h)
    store.merge_hist_tsv("cl9", "\n".join(
        f"{noon + i * 60}|{20.0 + i}|{30.0}|0:{50 + i}:100:40000"
        for i in range(3)))
    view = store.heatmap("cl9", days=7)
    today = [d for d in view["days"] if d["d"] == time.strftime(
        "%Y-%m-%d", time.localtime(noon))]
    assert today, "today's row must exist in the heat map"
    today = today[0]
    assert today["cpu"][4] == pytest.approx(21.0)
    assert today["cpu"][:4] == [None] * 4
    assert today["gpu"]["0"]["u"][4] == pytest.approx(51.0)
    assert today["gpu"]["0"]["m"][4] == pytest.approx(100 / 1024, abs=0.01)
    assert view["gpu_indexes"] == ["0"]


def test_heatmap_empty_window(store):
    view = store.heatmap("cl9", days=30)
    assert all(d["cpu"][i] is None for d in view["days"]
               for i in range(len(d["cpu"])))


# -------------------------------------------------------------------- usage
def test_usage_summary_integrates_and_filters(store):
    t0 = (int(time.time()) - 600) // 60 * 60        # minute-aligned stamps
    rows = []
    # wuhong on gpu0: 3 snapshots x 60 s apart -> 120 s billed
    for i in (0, 60, 120):
        rows.append(f"{t0 + i}|wuhong|0|2000")
    # zhang gaps > GAP (300 s) between each -> nothing integrable
    for i in (0, 300, 600):
        rows.append(f"{t0 + i}|zhang|1|8000")
    # system users excluded outright
    rows.append(f"{t0}|root|0|90000")
    store.merge_usage_tsv("cl9", "\n".join(rows))
    summary = store.usage_summary("cl9", days=7,
                                  excluded_users=DEFAULT_EXCLUDED_USERS)
    by_user = {u["user"]: u for u in summary["users"]}
    assert by_user["wuhong"]["minutes"] == pytest.approx(2.0)
    # average mem ~2000 MiB = 1.953125 GiB over 120 s => ~0.0651 GB·h
    assert by_user["wuhong"]["gb_hours"] == pytest.approx(0.065, abs=0.01)
    assert "root" not in by_user
    assert summary["covered_days"] >= 1


def test_usage_summary_counts_mem_change(store):
    t0 = (int(time.time()) - 600) // 60 * 60
    store.merge_usage_tsv("cl9", f"{t0}|li|0|1000\n{t0 + 60}|li|0|3000\n")
    user = store.usage_summary("cl9", days=1)["users"][0]
    assert user["minutes"] == pytest.approx(1.0)
    # trapezoid: mean((1000,3000)/1024)=1.953125 GiB * h/3600 (output 2dp)
    assert user["gb_hours"] == pytest.approx(1.953125 / 60, abs=0.005)


def _rows(path):
    import json
    from pathlib import Path

    if not Path(path).is_file():
        return []
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out
