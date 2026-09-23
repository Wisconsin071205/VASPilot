"""Local job ledger: what the cluster forgets, VASPilot remembers.

PBS/Torque exposes only ACTIVE jobs to regular users and the accounting
logs are admin-only, so once a job leaves the queue it vanishes — along
with any notion of WHEN it finished. The UI therefore snapshots every job
it observes into ``~/.vaspilot/jobs/<server>.json``:

  - first time a job is seen (from active or recent listings)
  - every state transition afterwards
  - ``completed_at`` stamped the first time a terminal state is observed
  - ``workdir`` learned while the job is still queued/running, so its
    results can be opened after the scheduler has forgotten it
  - for a job that vanished (``assumed_end``), the real start/end read
    back from its directory: VASP's OUTCAR header and the last write of
    its outputs, accepted only inside the window in which it must have
    ended (last seen in the queue .. first noticed gone)

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


def _epoch(stamp: Any) -> float | None:
    try:
        moment = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


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

    def observe(self, server: str, rows: list[dict[str, Any]], *,
                infer_missing: bool = False) -> list[dict[str, Any]]:
        """Fold freshly observed scheduler rows into the ledger.

        ``infer_missing=True`` marks previously-tracked non-terminal jobs
        that have vanished from the scheduler as COMPLETED at first
        absence (PBS keeps finished jobs only briefly — when even the
        finished listing no longer knows a job, it is finished).
        """
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
            # a queued job has used no time; older gateways put the
            # requested walltime there
            elapsed = "" if state.upper() == "PENDING" else \
                str(row.get("elapsed") or "")
            entry.update({
                "job_id": job_id,
                "name": str(row.get("name") or entry.get("name") or ""),
                "partition": str(row.get("partition")
                                 or entry.get("partition") or ""),
                "elapsed": elapsed or ("" if state.upper() == "PENDING"
                                       else str(entry.get("elapsed") or "")),
                "state": state,
                "first_seen": entry.get("first_seen") or now,
                "last_seen": now,
                "source": f"{entry.get('source', '')}+cluster".lstrip("+")
                          if "cluster" not in entry.get("source", "")
                          else entry["source"],
            })
            if row.get("started_at") and not entry.get("started_at"):
                entry["started_at"] = str(row["started_at"])
            if row.get("completed_at") and not entry.get("completed_at"):
                entry["completed_at"] = str(row["completed_at"])
            if prior_state != state or "state_history" not in entry:
                entry.setdefault("state_history", []).append(
                    {"state": state, "at": now})
            if _is_terminal(state) and not entry.get("completed_at"):
                entry["completed_at"] = now
        if infer_missing:
            known = {str(r.get("job_id") or "") for r in rows or []}
            for job_id, entry in jobs.items():
                if job_id in known or _is_terminal(str(entry.get("state"))):
                    continue
                entry["state"] = "COMPLETED"
                entry["assumed_end"] = True
                entry.setdefault("state_history", []).append(
                    {"state": "COMPLETED", "assumed": True, "at": now})
                if not entry.get("completed_at"):
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
                       partition: str = "", workdir: str = "") -> None:
        """A submission we performed ourselves: seed before first poll."""
        self.observe(server, [{"job_id": job_id, "name": name,
                               "partition": partition, "state": "PENDING",
                               "elapsed": "0:00"}])
        if workdir:
            self.remember_workdirs(server, {job_id: workdir})

    # asking the scheduler costs a round trip: a job it could not place is
    # retried a few times, then left for the user to pick by hand
    WORKDIR_TRIES = 3

    def workdir(self, server: str, job_id: str) -> str:
        return str(self._load(server).get(str(job_id), {}).get("workdir") or "")

    def needs_workdir(self, server: str, job_ids: list[str]) -> list[str]:
        jobs = self._load(server)
        return [job for job in job_ids
                if job in jobs and not jobs[job].get("workdir")
                and int(jobs[job].get("workdir_tries") or 0) < self.WORKDIR_TRIES]

    def remember_workdirs(self, server: str, found: dict[str, str]) -> None:
        """Record what the scheduler answered; '' counts as one try."""
        if not server or not found:
            return
        jobs = self._load(server)
        changed = False
        for job_id, workdir in found.items():
            entry = jobs.get(str(job_id))
            if entry is None or entry.get("workdir"):
                continue
            if workdir and str(workdir).startswith("/"):
                entry["workdir"] = str(workdir)
            else:
                entry["workdir_tries"] = int(entry.get("workdir_tries") or 0) + 1
            changed = True
        if changed:
            self._save(server, jobs)

    TIMING_TRIES = 3
    CLOCK_SLACK = 300  # seconds of skew allowed between this PC and the cluster

    def needs_timing(self, server: str) -> dict[str, str]:
        """{job_id: workdir} of vanished jobs whose times are still guesses."""
        return {job_id: str(entry["workdir"])
                for job_id, entry in self._load(server).items()
                if entry.get("assumed_end") and entry.get("workdir")
                and int(entry.get("timing_tries") or 0) < self.TIMING_TRIES}

    def remember_timing(self, server: str, found: dict[str, dict]) -> None:
        jobs = self._load(server)
        changed = False
        for job_id, times in (found or {}).items():
            entry = jobs.get(str(job_id))
            if not entry or not entry.get("assumed_end"):
                continue
            changed = True
            entry["timing_tries"] = int(entry.get("timing_tries") or 0) + 1
            end = times.get("last_write")
            low = _epoch(entry.get("last_seen"))
            high = _epoch(entry.get("completed_at"))
            if end is None or low is None or high is None:
                continue
            if end > high + self.CLOCK_SLACK:
                # something newer ran in that directory: its files say
                # nothing about this job any more
                entry["timing_tries"] = self.TIMING_TRIES
                continue
            if end < low - self.CLOCK_SLACK:
                continue
            end = min(max(end, low), high)
            entry["noticed_at"] = entry.get("completed_at")
            entry["completed_at"] = _iso(end)
            entry["completion_source"] = "files"
            entry["assumed_end"] = False
            start = times.get("vasp_started")
            if not entry.get("started_at") and start is not None and start <= end:
                entry["started_at"] = _iso(start)
                entry["start_source"] = "outcar"
        if changed:
            self._save(server, jobs)

    def merged(self, server: str) -> list[dict[str, Any]]:
        jobs = self._load(server)
        rows = sorted(jobs.values(),
                      key=lambda e: str(e.get("job_id")), reverse=True)
        return [self._view(e) for e in rows]

    @staticmethod
    def _view(entry: dict[str, Any]) -> dict[str, Any]:
        start, end = _epoch(entry.get("started_at")), _epoch(entry.get("completed_at"))
        return {
            # only when both ends are known; a vanished job's end is a guess
            "duration_seconds": int(end - start) if start is not None
            and end is not None and end >= start
            and not entry.get("assumed_end") else None,
            "noticed_at": entry.get("noticed_at"),
            "completion_source": entry.get("completion_source") or (
                "noticed" if entry.get("assumed_end") else "scheduler"),
            "start_source": entry.get("start_source") or (
                "scheduler" if entry.get("started_at") else ""),
            "job_id": entry.get("job_id"),
            "name": entry.get("name"),
            "partition": entry.get("partition"),
            "state": entry.get("state"),
            "elapsed": entry.get("elapsed"),
            "completed_at": entry.get("completed_at"),
            "started_at": entry.get("started_at"),
            "assumed_end": bool(entry.get("assumed_end")),
            "first_seen": entry.get("first_seen"),
            "last_seen": entry.get("last_seen"),
            "workdir": entry.get("workdir") or "",
            "local_record": True,
        }
