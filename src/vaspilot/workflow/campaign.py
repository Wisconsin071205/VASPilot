"""A campaign: one .vasp file, one approval, the whole VASPKIT chain.

``plan_campaign`` freezes the approved intent -- structure, recipe, the
INCAR settings and assertions of every stage, the job scripts, the VASPKIT
profile -- into one document whose hash the approval binds. Band and DOS
inputs do not exist yet when a human approves, so the file-level guarantee
the plain workflow has is replaced by what ``CampaignRunner`` does at run
time: after VASPKIT writes a stage's inputs it applies the approved settings,
re-reads the INCAR, checks every assertion, and records the sha256 of what
is about to run. Stage 01 keeps the full guarantee -- its POSCAR is the
user's own file, hashed before approval and re-checked on the server.

The runner is a tick-driven state machine rather than a blocking loop: a
calculation chain runs for days, and each tick persists what it did, so
restarting the UI costs nothing but the next tick.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.errors import AuthRequiredError, ValidationError, VaspilotError
from ..core.hashing import obj_sha256, text_sha256
from ..hpc.jobscript import render_job_script
from ..vaspkit.adapter import GEN_OK, generation_script, remote_command
from ..vaspkit.recipe import validate_recipe
from ..vaspkit.stages import build_campaign
from ..vaspkit.structure import parse_poscar, split_annotation, structure_summary
from ..vaspkit.verify import check_incar
from .engine import TERMINAL_SCHEDULER_STATES

SCHEMA = "vaspilot.campaign/1"
STAGE_LABELS = {"relax": "结构优化", "static": "静态自洽",
                "band": "能带", "dos": "态密度"}

# The first version runs the PBE family only: LDA needs another POTCAR
# library and SCAN/HSE06 need INCAR blocks nobody has checked yet.
SUPPORTED_FUNCTIONALS = {"PBE": {}, "PBEsol": {"GGA": "PS"}}
# Written on top of the VASPKIT template unless an override says otherwise.
STAGE_DEFAULTS = {"relax": {"NSW": "200"}, "dos": {"NEDOS": "2001"}}

STAGE_STOPPED = ("needs_review", "failed", "blocked", "aborted")
CAMPAIGN_FINAL = ("completed", "needs_review", "failed", "blocked",
                  "aborted", "rejected")

_VALUE_RE = re.compile(r"^[A-Za-z0-9.+\-_ ]{1,64}$")
_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_LOG_CAP = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ settings
def stage_settings(stage: dict[str, Any], recipe: dict[str, Any],
                   species: tuple[str, ...]) -> dict[str, str]:
    """Every INCAR value this stage must carry, in the order it is written.

    The equality assertions come first because VASPKIT's 101 menu has no
    band or DOS template: those stages start from the static one, so the
    stage's defining values have to be written in, not merely hoped for.
    """
    settings: dict[str, str] = {}
    for item in stage["assertions"]:
        if item["op"] == "eq":
            settings[item["key"]] = item["value"]
    settings.update(STAGE_DEFAULTS.get(stage["name"], {}))
    settings.update(SUPPORTED_FUNCTIONALS.get(recipe["functional"], {}))
    if recipe["hubbard_u"]:
        u = recipe["hubbard_u"]
        rows = [u.get(symbol, {"L": -1, "U": 0.0, "J": 0.0}) for symbol in species]
        settings["LDAU"] = ".TRUE."
        settings["LDAUTYPE"] = "2"
        settings["LDAUL"] = " ".join(str(int(row["L"])) for row in rows)
        settings["LDAUU"] = " ".join(f"{float(row['U']):g}" for row in rows)
        settings["LDAUJ"] = " ".join(f"{float(row['J']):g}" for row in rows)
        top = max(int(row["L"]) for row in rows)
        if top >= 2:
            settings["LMAXMIX"] = "6" if top == 3 else "4"
    settings.update(stage["overrides"])
    for key, value in settings.items():
        if not _VALUE_RE.fullmatch(str(value)):
            raise ValidationError(
                f"INCAR value for {key} ({value!r}) may only hold letters, "
                "digits, spaces and . + - _")
    return settings


def apply_settings(incar_text: str, settings: dict[str, str]) -> str:
    """Remove every assignment of a key in ``settings``, then append them.

    Assignments are removed rather than edited in place so a key can never
    end up in the file twice, including inside ``A = 1; B = 2`` lines.
    """
    keys = {key.upper() for key in settings}
    kept: list[str] = []
    for raw in incar_text.splitlines():
        if not raw.strip():
            kept.append("")
            continue
        cut = min((raw.find(mark) for mark in "#!" if mark in raw),
                  default=len(raw))
        code, comment = raw[:cut], raw[cut:]
        statements = [part for part in code.split(";")
                      if part.split("=", 1)[0].strip().upper() not in keys
                      or "=" not in part]
        body = ";".join(statements).rstrip()
        if body.strip() or (comment and not code.strip()):
            kept.append(body + (" " + comment if body.strip() and comment
                                else comment))
    while kept and not kept[-1].strip():
        kept.pop()
    block = ["", "# --- VASPilot campaign settings (approved) ---"]
    block += [f"{key} = {value}" for key, value in settings.items()]
    return "\n".join(kept + block) + "\n"


# ---------------------------------------------------------------------- plan
def plan_campaign(*, vasp_text: str, file_name: str, recipe: dict[str, Any],
                  server_entry: Any, profile: dict[str, Any],
                  potcar_library: str = "") -> dict[str, Any]:
    """Freeze one campaign. Touches no server.

    ``potcar_library`` is the library the user set for this server now; the
    probe must have been run against that same setting.
    """
    poscar_text, annotation = split_annotation(vasp_text)
    poscar = parse_poscar(poscar_text)
    summary = structure_summary(poscar)
    raw = dict(recipe or {})
    # the structure block is ours, not the model's: it is what gets hashed
    raw["structure"] = {"file": str(file_name)[:128],
                        "formula": summary["formula"],
                        "sha256": text_sha256(poscar.text)}
    raw.setdefault("resources", {})
    if isinstance(raw["resources"], dict):
        raw["resources"] = {**raw["resources"], "server": server_entry.name}
    checked = validate_recipe(raw, elements=summary["elements"])
    if checked["functional"] not in SUPPORTED_FUNCTIONALS:
        raise ValidationError(
            f"{checked['functional']} is not supported by the campaign runner "
            "yet; use PBE or PBEsol")
    if not profile.get("ready"):
        raise ValidationError(
            f"server {server_entry.name} has no ready VASPKIT profile; run "
            "vaspkit_doctor first")
    if str(profile.get("potcar_library") or "") != str(potcar_library or ""):
        raise ValidationError(
            f"the pseudopotential library for {server_entry.name} changed since "
            "the last probe; run vaspkit_doctor again")
    if not str(server_entry.remote_root or "").startswith("/"):
        raise ValidationError(
            f"server {server_entry.name} has no remote_root to put campaigns in")

    graph = build_campaign(checked)
    job_base = re.sub(r"[^A-Za-z0-9]", "", summary["formula"])[:20] or "vasp"
    stages = []
    for stage in graph["stages"]:
        settings = stage_settings(stage, checked, poscar.symbols)
        script = render_job_script(
            scheduler=server_entry.scheduler
            if server_entry.scheduler in ("slurm", "pbs") else "slurm",
            job_name=f"{job_base}-{stage['name']}",
            partition=checked["resources"]["partition"],
            ntasks=checked["resources"]["ntasks"],
            walltime=checked["resources"]["walltime"])
        stages.append({**stage, "settings": settings, "job_script": script,
                       "job_script_sha256": text_sha256(script)})

    body = {
        "schema": SCHEMA,
        "created_at": _now(),
        "server": server_entry.name,
        "remote_root": server_entry.remote_root.rstrip("/"),
        "structure": {**checked["structure"], "poscar": poscar.text,
                      "summary": {**summary, "elements": list(summary["elements"]),
                                  "abc": list(summary["abc"]),
                                  "angles": list(summary["angles"])}},
        "annotation": annotation[:4000],
        "recipe": checked,
        "vaspkit": {"command": profile["command"], "mode": profile["mode"],
                    "version": profile.get("version", ""), "ready": True,
                    # empty = VASPKIT's own ~/.vaspkit on the server
                    "potcar_library": str(potcar_library or "")},
        "stages": stages,
    }
    # round-trip through JSON so the hash covers exactly what is stored
    body = json.loads(json.dumps(body, ensure_ascii=False))
    campaign_hash = obj_sha256(body)
    return {"campaign_id": campaign_hash[:16], "campaign_hash": campaign_hash,
            "campaign": body}


def campaign_dir(record: dict[str, Any]) -> str:
    return (f"{record['campaign']['remote_root']}/campaigns/"
            f"{record['campaign_id']}")


# --------------------------------------------------------------------- store
class CampaignStore:
    """One JSON file per campaign; the hash is re-checked on every load."""

    _locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def lock(self, campaign_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(campaign_id, threading.Lock())

    def path(self, campaign_id: str) -> Path:
        if not _ID_RE.fullmatch(str(campaign_id or "")):
            raise ValidationError("campaign id must be 16 hex characters")
        return self.directory / f"{campaign_id}.json"

    def create(self, planned: dict[str, Any]) -> dict[str, Any]:
        path = self.path(planned["campaign_id"])
        if path.exists():
            raise ValidationError(
                f"campaign {planned['campaign_id']} already exists")
        record = {**planned, "state": {
            "status": "awaiting_approval", "approval": None,
            "created_at": _now(), "updated_at": _now(), "log": [],
            "stages": {stage["name"]: {"status": "waiting"}
                       for stage in planned["campaign"]["stages"]},
        }}
        self.save(record)
        return record

    def load(self, campaign_id: str) -> dict[str, Any]:
        path = self.path(campaign_id)
        if not path.is_file():
            raise ValidationError(f"campaign {campaign_id} was not found")
        record = json.loads(path.read_text(encoding="utf-8"))
        if obj_sha256(record.get("campaign")) != record.get("campaign_hash") \
                or record["campaign_hash"][:16] != campaign_id:
            raise ValidationError(
                f"campaign {campaign_id} was modified after it was planned")
        return record

    def save(self, record: dict[str, Any]) -> None:
        record["state"]["updated_at"] = _now()
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".campaign.", dir=str(self.directory))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path(record["campaign_id"]))

    def ids(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return sorted(path.stem for path in self.directory.glob("*.json")
                      if _ID_RE.fullmatch(path.stem))

    def list(self) -> list[dict[str, Any]]:
        records = []
        for campaign_id in self.ids():
            try:
                records.append(self.load(campaign_id))
            except (ValidationError, ValueError, OSError):
                continue
        records.sort(key=lambda r: r["state"].get("created_at", ""), reverse=True)
        return records

    # -- human decisions -------------------------------------------------------
    def approve(self, campaign_id: str, *, via: str) -> dict[str, Any]:
        with self.lock(campaign_id):
            record = self.load(campaign_id)
            state = record["state"]
            if state["status"] != "awaiting_approval":
                raise ValidationError(
                    f"campaign {campaign_id} is {state['status']}, not awaiting "
                    "approval")
            state["status"] = "running"
            state["approval"] = {"campaign_hash": record["campaign_hash"],
                                 "approved_at": _now(), "via": str(via)[:40]}
            log(record, "", "approved", f"via {via}")
            self.save(record)
            return record

    def reject(self, campaign_id: str) -> dict[str, Any]:
        with self.lock(campaign_id):
            record = self.load(campaign_id)
            if record["state"]["status"] != "awaiting_approval":
                raise ValidationError(
                    f"campaign {campaign_id} is {record['state']['status']}")
            record["state"]["status"] = "rejected"
            log(record, "", "rejected", "")
            self.save(record)
            return record


def log(record: dict[str, Any], stage: str, event: str, detail: str) -> None:
    rows = record["state"].setdefault("log", [])
    rows.append({"at": _now(), "stage": stage, "event": event,
                 "detail": str(detail)[:500]})
    del rows[:-_LOG_CAP]


# -------------------------------------------------------------------- runner
def same_cell(a_text: str, b_text: str) -> bool:
    """Same lattice (lengths and angles) and atom count, whatever the axes."""
    a = structure_summary(parse_poscar(a_text))
    b = structure_summary(parse_poscar(b_text))
    return (a["natoms"] == b["natoms"]
            and all(abs(x - y) < 1e-3 for x, y in zip(a["abc"], b["abc"]))
            and all(abs(x - y) < 1e-2 for x, y in zip(a["angles"], b["angles"])))


class CampaignRunner:
    def __init__(self, *, store: CampaignStore, client: Any,
                 audit: Any = None) -> None:
        self.store = store
        self.client = client
        self.audit = audit

    def tick_all(self) -> list[str]:
        advanced = []
        for campaign_id in self.store.ids():
            try:
                if self.store.load(campaign_id)["state"]["status"] == "running":
                    self.tick(campaign_id)
                    advanced.append(campaign_id)
            except (VaspilotError, ValueError, OSError):
                continue
        return advanced

    def tick(self, campaign_id: str) -> dict[str, Any]:
        with self.store.lock(campaign_id):
            record = self.store.load(campaign_id)
            state = record["state"]
            if state["status"] != "running":
                return record
            approval = state.get("approval") or {}
            if approval.get("campaign_hash") != record["campaign_hash"]:
                state["status"] = "blocked"
                log(record, "", "blocked",
                    "the approval does not match this campaign")
                self.store.save(record)
                return record
            state.pop("note", None)
            for stage in record["campaign"]["stages"]:
                row = state["stages"][stage["name"]]
                if row["status"] == "converged" or row["status"] in STAGE_STOPPED:
                    continue
                needed = stage["requires"]
                if needed and state["stages"][needed]["status"] != "converged":
                    continue
                try:
                    if row["status"] == "waiting":
                        self._start(record, stage, row)
                    elif row["status"] == "preparing":
                        self._stop(record, stage, row, "failed",
                                   "interrupted while its inputs were being "
                                   "prepared; inspect the stage directory")
                    elif row["status"] == "running":
                        self._poll(record, stage, row)
                except AuthRequiredError as exc:
                    state["note"] = f"server disconnected: {exc.message}"
                    if row["status"] == "preparing":
                        self._stop(record, stage, row, "failed",
                                   "the connection dropped while its inputs "
                                   "were being prepared")
                    self.store.save(record)
                    break
                except VaspilotError as exc:
                    self._stop(record, stage, row, "failed", exc.message)
                self.store.save(record)
            self._settle(record)
            self.store.save(record)
            return record

    def abort(self, campaign_id: str) -> dict[str, Any]:
        with self.store.lock(campaign_id):
            record = self.store.load(campaign_id)
            state = record["state"]
            if state["status"] in CAMPAIGN_FINAL:
                return record
            server = record["campaign"]["server"]
            for name, row in state["stages"].items():
                if row["status"] == "converged" or row["status"] in STAGE_STOPPED:
                    continue
                if row["status"] == "running" and row.get("job_id"):
                    try:
                        self.client.cancel(str(row["job_id"]), str(row["job_id"]),
                                           server=server)
                    except VaspilotError as exc:
                        log(record, name, "cancel_failed", exc.message)
                row["status"] = "aborted"
            state["status"] = "aborted"
            log(record, "", "aborted", "")
            self.store.save(record)
            return record

    # -- stage actions ----------------------------------------------------------
    def _stop(self, record: dict, stage: dict, row: dict, status: str,
              error: str) -> None:
        row["status"] = status
        row["error"] = str(error)[:2000]
        log(record, stage["name"], status, error)

    def _start(self, record: dict, stage: dict, row: dict) -> None:
        doc = record["campaign"]
        server = doc["server"]
        root = campaign_dir(record)
        where = f"{root}/{stage['dir']}"
        self.client.mkdir(where, server=server)
        row.update(status="preparing", remote_dir=where)
        self.store.save(record)

        from_input = stage["poscar_from"] == "input"
        if from_input:
            self._upload_text(doc["structure"]["poscar"], f"{where}/POSCAR",
                              doc["structure"]["sha256"], server)
        names = [s["name"] for s in doc["stages"]]
        script = generation_script(
            stage=stage["name"], profile=doc["vaspkit"], stage_dir=where,
            poscar_src="" if from_input else f"{root}/{stage['poscar_from']}",
            chgcar_src=f"{root}/{stage['chgcar_from']}/CHGCAR"
            if stage["chgcar_from"] else "",
            kspacing=stage["kspacing"], kpoints_task=stage["kpoints_task"],
            check_primitive=stage["index"] == 1 and "band" in names)
        result = self.client.run_command(remote_command(script),
                                         timeout_seconds=300, server=server)
        if result.get("rc") != 0 or GEN_OK not in str(result.get("stdout", "")):
            tail = (str(result.get("stdout", "")) + str(result.get("stderr", "")))
            self._stop(record, stage, row, "failed",
                       "VASPKIT did not produce this stage's inputs; its logs "
                       f"(vaspkit-*.log) are in {where}. Output: {tail[-600:]}")
            return

        original = str(self.client.read(f"{where}/INCAR", server=server)["content"])
        self._write_text(apply_settings(original, stage["settings"]),
                         f"{where}/INCAR", text_sha256(original), server)
        final = str(self.client.read(f"{where}/INCAR", server=server)["content"])
        asserted = {item["key"] for item in stage["assertions"]}
        checks = list(stage["assertions"]) + [
            {"key": key, "op": "eq", "value": value}
            for key, value in stage["settings"].items() if key not in asserted]
        report = check_incar(final, checks)
        row["verify"] = report
        if not report["ok"]:
            self._stop(record, stage, row, "blocked",
                       "the INCAR on the server does not match what was "
                       "approved: " + "; ".join(
                           f"{f['key']} {f.get('reason', '')}"
                           for f in report["failures"]))
            return

        if stage["kpoints_task"] == 303 or (stage["index"] == 1
                                            and "band" in names):
            problem = self._primitive_problem(where, server)
            if problem:
                self._stop(record, stage, row, "blocked", problem)
                return

        files = ["POSCAR", "INCAR", "KPOINTS", "POTCAR"] + \
            (["CHGCAR"] if stage["chgcar_from"] else [])
        digest = self.client.run_command(
            f"cd -- {shlex.quote(where)} && sha256sum -- " + " ".join(files),
            timeout_seconds=120, server=server)
        hashes = {}
        for line in str(digest.get("stdout", "")).splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] in files:
                hashes[parts[1]] = parts[0]
        row["hashes"] = hashes
        if set(hashes) != set(files):
            self._stop(record, stage, row, "failed",
                       "could not hash the stage inputs on the server")
            return
        if from_input and hashes["POSCAR"] != doc["structure"]["sha256"]:
            self._stop(record, stage, row, "blocked",
                       "POSCAR on the server is not the approved structure")
            return

        self._upload_text(stage["job_script"], f"{where}/run.job.sh",
                          stage["job_script_sha256"], server)
        checked = self.client.vasp_validate(where, server=server)
        if checked.get("errors"):
            self._stop(record, stage, row, "failed",
                       "input validation failed: "
                       + "; ".join(str(e) for e in checked["errors"]))
            return
        submitted = self.client.submit(
            where, "run.job.sh", approval_ref=f"campaign-{record['campaign_id']}",
            server=server)
        row.update(status="running", job_id=str(submitted.get("job_id", "")),
                   submitted_at=_now())
        log(record, stage["name"], "submitted", f"job {row['job_id']}")

    def _poll(self, record: dict, stage: dict, row: dict) -> None:
        server = record["campaign"]["server"]
        scheduler = str(self.client.job_state(
            row["job_id"], server=server).get("state") or "UNKNOWN")
        row["scheduler_state"] = scheduler
        try:
            progress = self.client.vasp_live(row["remote_dir"], server=server)
            row["progress"] = {key: progress.get(key) for key in (
                "scientific_converged", "ionic_step", "nsw", "nelm",
                "current_electronic", "current_de", "last_electronic_steps",
                "last_e0", "delta_e", "ediffg", "max_force",
                "electronic_reached_nelm", "nelm_hits", "error_signatures",
                "finished_normally", "elapsed_seconds", "ionic_exhausted",
                "total_electronic")}
        except VaspilotError:
            if scheduler not in TERMINAL_SCHEDULER_STATES:
                return
            raise
        if scheduler not in TERMINAL_SCHEDULER_STATES:
            return
        if scheduler == "COMPLETED" and progress.get("scientific_converged"):
            row["status"] = "converged"
            log(record, stage["name"], "converged", f"job {row['job_id']}")
        elif scheduler == "COMPLETED":
            self._stop(record, stage, row, "needs_review",
                       "the job finished but VASP did not converge; the stages "
                       "after it stay on hold")
        else:
            self._stop(record, stage, row, "failed", f"the job ended {scheduler}")

    def _primitive_problem(self, where: str, server: str) -> str:
        try:
            prim = str(self.client.read(f"{where}/PRIMCELL.vasp",
                                        server=server)["content"])
        except VaspilotError:
            return ""  # nothing to compare against; the band path is VASPKIT's
        poscar = str(self.client.read(f"{where}/POSCAR", server=server)["content"])
        try:
            if same_cell(poscar, prim):
                return ""
        except ValidationError as exc:
            return f"could not compare POSCAR with PRIMCELL.vasp: {exc.message}"
        return ("this structure is not VASPKIT's standard primitive cell "
                "(PRIMCELL.vasp differs), so the high-symmetry band path would "
                "not match it; restart the campaign from PRIMCELL.vasp")

    def _settle(self, record: dict) -> None:
        state = record["state"]
        rows = [state["stages"][s["name"]] for s in record["campaign"]["stages"]]
        if all(row["status"] == "converged" for row in rows):
            state["status"] = "completed"
        elif any(row["status"] in ("running", "preparing") for row in rows):
            return
        else:
            for stage in record["campaign"]["stages"]:
                row = state["stages"][stage["name"]]
                needed = stage["requires"]
                if row["status"] == "waiting" and (
                        not needed
                        or state["stages"][needed]["status"] == "converged"):
                    return  # it starts on the next tick
            state["status"] = next(row["status"] for row in rows
                                   if row["status"] in STAGE_STOPPED)
        state["finished_at"] = _now()
        log(record, "", state["status"], "")
        if self.audit is not None:
            try:
                self.audit.record("campaign.finish", outcome=state["status"],
                                  campaign_id=record["campaign_id"])
            except Exception:
                pass

    # -- file helpers -------------------------------------------------------------
    def _upload_text(self, text: str, remote: str, sha: str, server: str) -> None:
        if text_sha256(text) != sha:
            raise ValidationError(f"{remote} drifted from the approved campaign")
        fd, tmp = tempfile.mkstemp(prefix="vaspilot-campaign-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            self.client.upload(tmp, remote, server=server, expected_sha256=sha)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _write_text(self, text: str, remote: str, expected: str,
                    server: str) -> None:
        fd, tmp = tempfile.mkstemp(prefix="vaspilot-campaign-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            self.client.write_file(remote, tmp, server=server,
                                   expected_sha256=expected)
        finally:
            Path(tmp).unlink(missing_ok=True)


# What the model is told when it has to turn an annotation into a recipe.
RECIPE_HINT = {
    "stages": "subset of relax / static / band / dos; band and dos imply "
              "static, static implies relax",
    "structure_relaxed": "true when the user says the structure is already "
                         "optimised (skips the implied relax)",
    "functional": "PBE (default) or PBEsol",
    "spin": "true for magnetic systems (ISPIN = 2 on every stage)",
    "hubbard_u": "{element: {L, U, J}} for DFT+U, e.g. {\"Fe\": {\"L\": 2, "
                 "\"U\": 4.0, \"J\": 0}}",
    "kspacing": "{stage: 0.01..0.5} in 2*pi/Angstrom; omit for defaults "
                "relax 0.04, static 0.03, dos 0.02",
    "overrides": "{stage: {INCAR_KEY: value}} only when the user asks for a "
                 "specific value; a value that contradicts a stage's safety "
                 "assertion is refused",
    "resources": "{ntasks, walltime HH:MM:SS, partition}",
}


def run_doctor(config: Any, client: Any, server: str,
               command: str = "") -> dict[str, Any]:
    """Probe one server as configured and remember the verdict."""
    from ..vaspkit.adapter import doctor_script, parse_doctor
    library = config.potcar_library(server)
    result = client.run_command(
        remote_command(doctor_script(command, potcar_library=library)),
        timeout_seconds=300, server=server)
    profile = parse_doctor(str(result.get("stdout", "")))
    profile["potcar_library"] = library
    profile["probed_at"] = _now()
    profile_store(config).put(server, profile)
    return profile


def campaign_store(config: Any) -> CampaignStore:
    return CampaignStore(Path(config.home) / "campaigns")


def profile_store(config: Any) -> Any:
    from ..vaspkit.adapter import ProfileStore
    return ProfileStore(Path(config.home) / "vaspkit.json")


def preview(planned: dict[str, Any]) -> dict[str, Any]:
    """The plan as a person should review it before approving."""
    doc = planned["campaign"]
    return {
        "campaign_hash": planned["campaign_hash"],
        "server": doc["server"],
        "structure": {key: doc["structure"][key]
                      for key in ("file", "formula", "sha256", "summary")},
        "annotation": doc["annotation"],
        "recipe": doc["recipe"],
        "vaspkit": doc["vaspkit"],
        "stages": [{
            "name": stage["name"], "label": STAGE_LABELS[stage["name"]],
            "dir": stage["dir"], "requires": stage["requires"],
            "poscar_from": stage["poscar_from"],
            "kpoints": ("high-symmetry path (VASPKIT 303)"
                        if stage["kpoints_task"] == 303
                        else f"Gamma grid, spacing {stage['kspacing']}"),
            "settings": stage["settings"], "assertions": stage["assertions"],
        } for stage in doc["stages"]],
    }


def view(record: dict[str, Any]) -> dict[str, Any]:
    """What a person (or the model) needs to see of one campaign."""
    doc, state = record["campaign"], record["state"]
    return {
        "campaign_id": record["campaign_id"],
        "campaign_hash": record["campaign_hash"],
        "formula": doc["structure"]["formula"],
        "file": doc["structure"]["file"],
        "functional": doc["recipe"]["functional"],
        "server": doc["server"],
        "potcar_library": doc["vaspkit"].get("potcar_library", ""),
        "status": state["status"],
        "note": state.get("note", ""),
        "created_at": state.get("created_at", ""),
        "approved_at": (state.get("approval") or {}).get("approved_at", ""),
        "finished_at": state.get("finished_at", ""),
        "remote_dir": campaign_dir(record),
        "stages": [{
            "name": stage["name"], "label": STAGE_LABELS[stage["name"]],
            "dir": stage["dir"], "requires": stage["requires"],
            "settings": stage["settings"],
            "kpoints": ("VASPKIT 303 high-symmetry path"
                        if stage["kpoints_task"] == 303
                        else f"Gamma, spacing {stage['kspacing']}"),
            **{key: state["stages"][stage["name"]].get(key) for key in (
                "status", "job_id", "scheduler_state", "error", "progress",
                "verify", "hashes", "submitted_at", "remote_dir")},
        } for stage in doc["stages"]],
        "log": state.get("log", [])[-20:],
    }
