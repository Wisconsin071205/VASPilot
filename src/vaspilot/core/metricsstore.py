"""Minute-bucket metrics history and per-user GPU usage accounting.

Local/UI-side only: none of this is exposed to the agent tool surface.
History comes from two feeds sharing one shape — the offline collector's
TSV files on the HPC node (merged through ``monitor.history``) and live
``server.metrics`` samples appended by the UI poller. Both are folded into
one-minute buckets that keep min/max ranges, so a bucket fed by a collector
row and a live poll in the same minute simply widens its range instead of
duplicating points.

hist.jsonl row : {"t": minute_epoch, "c": [cpu_lo, cpu_hi],
                  "m": [mem_lo, mem_hi],
                  "g": {"<idx>": [util_lo, util_hi, usedmb_lo, usedmb_hi]}}
usage.jsonl row: {"t": minute_epoch, "u": user, "g": gpu_index,
                  "mem": mib}
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

KEEP_DAYS = 90
SLOTS_PER_DAY = 8          # heat-map granularity: 3-hour cells like RackTop
SLOT_SECONDS = 86400 // SLOTS_PER_DAY
USAGE_GAP_SECONDS = 120    # snapshot gaps up to this size still count as one
                           # continuous occupation interval

# login/display/system identities whose GPU footprint must not be billed to
# any lab member (mirrors the RackTop denylist idea, trimmed for PBS/Slurm)
DEFAULT_EXCLUDED_USERS = frozenset({
    "root", "daemon", "bin", "sys", "sync", "shutdown", "halt",
    "gdm", "lightdm", "sddm", "sshd", "dbus", "nobody", "systemd-coredump",
})


def _f(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class MetricsStore:
    """jsonl-backed minute buckets per server, plus usage snapshots."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- plumbing
    def _hist_path(self, server: str) -> Path:
        return self.dir / f"{server}.hist.jsonl"

    def _usage_path(self, server: str) -> Path:
        return self.dir / f"{server}.usage.jsonl"

    @staticmethod
    def _read_rows(path: Path) -> list[dict]:
        if not path.is_file():
            return []
        rows: list[dict] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("t"), int):
                    rows.append(row)
        except OSError:
            return []
        return rows

    @staticmethod
    def _write_rows(path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        payload = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True)
                          + "\n" for r in rows)
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _prune(rows: list[dict]) -> list[dict]:
        cutoff = time.time() - KEEP_DAYS * 86400
        return [r for r in rows if r["t"] >= cutoff]

    # ------------------------------------------------------------ write feed
    def append_sample(self, server: str, ts: float | None = None,
                      cpu_pct: float | None = None,
                      mem_pct: float | None = None,
                      gpus: list[dict] | None = None) -> None:
        """Fold one live snapshot into the matching minute bucket."""
        now = ts if ts is not None else time.time()
        minute = int(now // 60 * 60)
        rows = self._read_rows(self._hist_path(server))
        bucket = next((r for r in rows if r["t"] == minute), None)
        if bucket is None:
            bucket = {"t": minute, "c": [None, None], "m": [None, None],
                      "g": {}}
            rows.append(bucket)
        self._widen(bucket, cpu_pct, mem_pct, gpus or [])
        rows = self._prune([r for r in rows if isinstance(r.get("c"), list)])
        self._write_rows(self._hist_path(server), rows)

    @staticmethod
    def _widen(bucket: dict, cpu_pct, mem_pct, gpus) -> None:
        for key, value in (("c", cpu_pct), ("m", mem_pct)):
            span = bucket[key]
            if value is None:
                if span[0] is None:
                    bucket[key] = [None, None]
                continue
            if span[0] is None:
                bucket[key] = [value, value]
            else:
                bucket[key] = [min(span[0], value), max(span[1], value)]
        for gpu in gpus:
            try:
                idx = str(int(gpu["index"]))
            except (KeyError, TypeError, ValueError):
                continue
            util = _f(str(gpu.get("util_pct"))) \
                if gpu.get("util_pct") is not None else None
            used = gpu.get("mem_used_gb")
            used_mb = used * 1024 if isinstance(used, (int, float)) else None
            span = list((bucket["g"].get(idx) or [None, None, None,
                                                  None])[:4])
            span += [None] * (4 - len(span))
            if util is not None:
                span[0] = util if span[0] is None else min(span[0], util)
                span[1] = util if span[1] is None else max(span[1], util)
            if used_mb is not None:
                span[2] = used_mb if span[2] is None else min(span[2],
                                                              used_mb)
                span[3] = used_mb if span[3] is None else max(span[3],
                                                              used_mb)
            bucket["g"][idx] = span

    # ----------------------------------------------------- offline collector
    def merge_hist_tsv(self, server: str, text: str) -> dict:
        """Merge ``t|cpu|mem|idx:util:usedmb:totalmb;…`` collector rows."""
        known = {r["t"] for r in self._read_rows(self._hist_path(server))}
        fresh: dict[int, dict] = {}
        added = skipped = 0
        for line in str(text or "").splitlines():
            parts = line.strip().split("|")
            if len(parts) < 3:
                continue
            stamp = _f(parts[0])
            if stamp is None:
                continue
            minute = int(stamp // 60 * 60)
            cpu = _f(parts[1])
            mem = _f(parts[2])
            gpus: dict[str, list] = {}
            for chunk in (parts[3] if len(parts) > 3 else "").split(";"):
                fields = chunk.split(":")
                if len(fields) >= 4:
                    values = [_f(f) for f in fields[:4]]
                    if all(v is not None for v in values):
                        # collector layout idx:util:usedMB:totalMB folds into
                        # the span shape [util_lo, util_hi, used_lo, used_hi]
                        gpus[str(int(values[0]))] = [
                            values[1], values[1], values[2], values[2]]
            if minute in known:
                skipped += 1
                continue
            bucket = fresh.setdefault(
                minute, {"t": minute, "c": [None, None], "m": [None, None],
                         "g": {}})
            if cpu is not None:
                bucket["c"] = [cpu, cpu]
            if mem is not None:
                bucket["m"] = [mem, mem]
            bucket["g"] = {k: list(v) for k, v in gpus.items()}
            added += 1
        if fresh:
            rows = self._read_rows(self._hist_path(server))
            rows.extend(fresh.values())
            rows.sort(key=lambda r: r["t"])
            self._write_rows(self._hist_path(server), self._prune(rows))
        return {"added": added, "duplicate": skipped}

    def merge_usage_tsv(self, server: str, text: str) -> dict:
        """Merge ``t|user|gpu_index|mem_mib`` collector snapshots."""
        seen: set[tuple[int, str, str]] = {
            (r["t"], str(r.get("u")), str(r.get("g")))
            for r in self._read_rows(self._usage_path(server))}
        fresh: list[dict] = []
        added = 0
        for line in str(text or "").splitlines():
            parts = line.strip().split("|")
            if len(parts) != 4:
                continue
            stamp = _f(parts[0])
            if stamp is None or not parts[1].strip():
                continue
            mem = _f(parts[3])
            key = (int(stamp // 60 * 60), parts[1].strip(),
                   (parts[2].strip() or "-"))
            if key in seen:
                continue
            seen.add(key)
            fresh.append({"t": key[0], "u": key[1][:32], "g": key[2][:4],
                          "mem": mem})
            added += 1
        if fresh:
            rows = self._read_rows(self._usage_path(server))
            rows.extend(fresh)
            rows.sort(key=lambda r: r["t"])
            self._write_rows(self._usage_path(server),
                             [r for r in self._prune(rows)])
        return {"added": added}

    # ---------------------------------------------------------------- views
    def heatmap(self, server: str, days: int = 30) -> dict:
        """Day × 3-hour-slot averages for the CPU/MEM and every known GPU."""
        days = max(1, min(int(days or 30), KEEP_DAYS))
        midnight = int(time.time() // 86400 * 86400) - (days - 1) * 86400
        rows = [r for r in self._read_rows(self._hist_path(server))
                if r["t"] >= midnight]
        slots: dict[str, dict] = {}

        def slot_for(row_ts: int) -> tuple[str, int]:
            day_start = int(row_ts // 86400 * 86400)
            label = time.strftime("%Y-%m-%d", time.localtime(day_start))
            return label, (row_ts - day_start) // SLOT_SECONDS

        def acc(cell: dict, values: list):
            cell.extend(v for v in values if v is not None)

        def mean(cell):
            return round(sum(cell) / len(cell), 1) if cell else None

        out_days: dict[str, dict] = {}
        for row in rows:
            label, pos = slot_for(row["t"])
            cells = out_days.setdefault(label, {"_cells": {}})["_cells"]
            cpu_cells = cells.setdefault(
                "c", [[] for _ in range(SLOTS_PER_DAY)])
            mem_cells = cells.setdefault(
                "m", [[] for _ in range(SLOTS_PER_DAY)])
            acc(cpu_cells[pos], row.get("c"))
            acc(mem_cells[pos], row.get("m"))
            for idx, span in (row.get("g") or {}).items():
                gpu_cells = cells.setdefault(idx, {"u": [
                    [] for _ in range(SLOTS_PER_DAY)], "m": [
                    [] for _ in range(SLOTS_PER_DAY)]})
                util, _, used_lo, used_hi = span[0], span[1], span[2], span[3]
                acc(gpu_cells["u"][pos], [util])
                acc(gpu_cells["m"][pos], [used_hi])
        gpu_indexes: set[str] = set()
        rendered = []
        for label in sorted(out_days):
            cells = out_days[label]["_cells"]
            gpu_indexes.update(k for k in cells if k not in ("c", "m"))
            entry = {"d": label,
                     "cpu": [mean(c) for c in cells.get("c", [])],
                     "mem": [mean(c) for c in cells.get("m", [])],
                     "gpu": {}}
            for idx in cells:
                if idx in ("c", "m"):
                    continue
                entry["gpu"][idx] = {
                    "u": [mean(c) for c in cells[idx]["u"]],
                    "m": [mean(c) and round(mean(c) / 1024, 1)
                          for c in cells[idx]["m"]]}
            rendered.append(entry)
        return {"days": rendered, "window_days": days,
                "gpu_indexes": sorted(gpu_indexes, key=lambda x: int(x))}

    def usage_summary(self, server: str, days: int = 30,
                      excluded_users: frozenset | None = None) -> dict:
        """Integrate per-user GPU occupation from collected snapshots."""
        days = max(1, min(int(days or 30), KEEP_DAYS))
        cutoff = time.time() - days * 86400
        excluded = excluded_users if excluded_users is not None \
            else DEFAULT_EXCLUDED_USERS
        rows = [r for r in self._read_rows(self._usage_path(server))
                if r["t"] >= cutoff and str(r.get("u")) not in excluded]
        streams: dict[tuple[str, str], list[dict]] = {}
        covered: set[str] = set()
        for row in rows:
            streams.setdefault((row["u"], row["g"]), []).append(row)
            covered.add(time.strftime("%Y-%m-%d", time.localtime(row["t"])))
        totals: dict[str, dict[str, float]] = {}
        for (_user, _gpu), items in streams.items():
            items.sort(key=lambda r: r["t"])
            stats = totals.setdefault(_user, {"seconds": 0.0, "gb_hours": 0.0})
            previous = None
            for item in items:
                if previous is not None and 0 < item["t"] - previous["t"] \
                        <= USAGE_GAP_SECONDS:
                    delta = item["t"] - previous["t"]
                    mem_a = previous.get("mem") or 0.0
                    mem_b = item.get("mem") or 0.0
                    stats["seconds"] += delta
                    stats["gb_hours"] += ((mem_a + mem_b) / 2 / 1024) \
                        * delta / 3600
                previous = item
        users = [{"user": name,
                  "minutes": round(v["seconds"] / 60, 1),
                  "gb_hours": round(v["gb_hours"], 2)}
                 for name, v in totals.items()]
        users.sort(key=lambda u: u["minutes"], reverse=True)
        peak = users[0]["minutes"] if users else 1.0
        for user in users:
            user["share"] = round(user["minutes"] * 100.0 / peak) if peak \
                else 0
        return {"users": users, "covered_days": len(covered),
                "window_days": days}

    def latest(self, server: str) -> dict | None:
        rows = self._read_rows(self._hist_path(server))
        return rows[-1] if rows else None

    def recent(self, server: str, hours: int = 48) -> list[dict]:
        """Raw minute buckets for the last N hours (idle-persistence math)."""
        cutoff = time.time() - max(1, min(int(hours or 48), KEEP_DAYS * 24)) \
            * 3600
        return [r for r in self._read_rows(self._hist_path(server))
                if r["t"] >= cutoff]
