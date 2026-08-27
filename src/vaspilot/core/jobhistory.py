"""Local job ledger: what the cluster forgets, VASPilot remembers.

PBS/Torque exposes only ACTIVE jobs to regular users and the accounting
logs are admin-only, so once a job leaves the queue it vanishes — along
with any notion of WHEN it finished. The UI therefore snapshots every job
it observes into ``~/.vaspilot/jobs/<server>.json``:

  - first time a job is seen (from active or recent listings)
  - every state transition afterwards
  - ``completed_at`` stamped the first time a terminal state is observed

The merged view served to the UI is cluster rows + ledger entries, so
history survives refreshes, restarts, and clusters that keep no history.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

TERMINAL_STATES = {"COMPLETED", "FAILED", "TIMEOUT", "CANCELLED",
                   "CANCELED", "NODE_FAIL", "OUT_OF_MEMORY", "PREEMPTED"}
MAX_ENTRIES = 200
KEEP_DAYS = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_terminal(state: str) -> bool:
    return str(state).upper() in TERMINAL_STATES


class JobLedger:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def _path(self, server: str) -> Path:
        return self.directory / f"{server}.json"

    def _load(self, server: str) -> dict[str, dict[str, Any]]:
        try:
            with open(self._path(server), "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {}

    def _save(self, server: str, jobs: dict[str, dict[str, Any]]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".", suffix=".tmp",
                                   dir=str(self.directory))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(jobs, fh, ensure_ascii=False, indent=1,
                          sort_keys=True)
            os.replace(tmp, self._path(server))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def observe(self, server: str,
                rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fold freshly observed scheduler rows into the ledger."""
        if not server:
            return []
        now = _now()
        jobs = self._load(server)
        for row in rows or []:
            job_id = str(row.get("job_id") or "").strip()
            if not job_id:
                continue
            entry = jobs.setdefault(job_id, {})
            state = str(row.get("state") or "UNKNOWN")
            prior_state = entry.get("state")
            entry.update({
                "job_id": job_id,
                "name": str(row.get("name") or entry.get("name") or ""),
                "partition": str(row.get("partition")
                                 or entry.get("partition") or ""),
                "elapsed": str(row.get("elapsed") or entry.get("elapsed")
                               or ""),
                "state": state,
                "first_seen": entry.get("first_seen") or now,
                "last_seen": now,
                "source": f"{entry.get('source', '')}+cluster".lstrip("+")
                          if "cluster" not in entry.get("source", "")
                          else entry["source"],
            })
            if prior_state != state or "state_history" not in entry:
                entry.setdefault("state_history", []).append(
                    {"state": state, "at": now})
            if _is_terminal(state) and not entry.get("completed_at"):
                entry["completed_at"] = now
        # cap + prune old terminal entries
        horizon = datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)
        for job_id in list(jobs):
            entry = jobs[job_id]
            updated = entry.get("last_seen") or entry.get("first_seen") or ""
            try:
                seen_at = datetime.fromisoformat(updated)
            except ValueError:
                continue
            if _is_terminal(entry.get("state", "")) and seen_at < horizon:
                del jobs[job_id]
        while len(jobs) > MAX_ENTRIES:  # oldest terminal entries first
            oldest = min(
                (j for j in jobs.values()
                 if _is_terminal(str(j.get("state")))),
                key=lambda e: e.get("last_seen", ""), default=None)
            if oldest is None:
                break
            jobs.pop(str(oldest["job_id"]), None)
        self._save(server, jobs)
        return self.merged(server)

    def seed_submitted(self, server: str, job_id: str, name: str = "",
                       partition: str = "") -> None:
        """A submission we performed ourselves: seed before first poll."""
        self.observe(server, [{"job_id": job_id, "name": name,
                               "partition": partition, "state": "PENDING",
                               "elapsed": "0:00"}])

    def merged(self, server: str) -> list[dict[str, Any]]:
        jobs = self._load(server)
        rows = sorted(jobs.values(),
                      key=lambda e: str(e.get("job_id")), reverse=True)
        return [self._view(e) for e in rows]

    @staticmethod
    def _view(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "job_id": entry.get("job_id"),
            "name": entry.get("name"),
            "partition": entry.get("partition"),
            "state": entry.get("state"),
            "elapsed": entry.get("elapsed"),
            "completed_at": entry.get("completed_at"),
            "first_seen": entry.get("first_seen"),
            "last_seen": entry.get("last_seen"),
            "local_record": True,
        }
