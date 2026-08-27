"""The named tool registry — the single model-facing action surface.

Every consumer (CLI ``agent chat/run``, the MCP server, the Codex plugin)
dispatches through this registry. Most tools are named operations whose
arguments are validated through :mod:`vaspilot.core.validation` and map to
one :class:`GatewayClient` method; the shell tools (shell_run/remote_run)
are deliberately audit-only with no interception, per the operator's
explicit policy choice.

Kinds:
  read      — safe for any provider, including degraded analysis_only ones
  write     — mutates local/remote state; requires a provider whose
              capability probe passed everything
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..core.config import Config
from ..core.errors import ToolNotAllowedError, ValidationError
from ..core.hashing import file_sha256
from ..gateway.client import GatewayClient
from ..hpc.vasp import denied_text_file, potcar_metadata

Handler = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class ToolDef:
    name: str
    description: str
    kind: str  # "read" | "write"
    parameters: dict[str, Any]
    handler: Handler
    schema_extras: dict[str, Any] = field(default_factory=dict)

    def to_openai(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    def to_mcp(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.parameters,
        }


def _server_param() -> dict[str, Any]:
    return {"type": "string",
            "description": "registered server name (empty = default server)",
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$"}


def _path_param(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description,
            "minLength": 1, "maxLength": 1024}


class ToolContext:
    """Everything a tool handler may touch. Constructed once per process."""

    def __init__(self, *, config: Config, client: GatewayClient,
                 project_root: Path | None = None,
                 potcar_library: Path | None = None,
                 workflow_engine: Any = None,
                 audit: Any = None,
                 session_id: str = "") -> None:
        self.config = config
        self.client = client
        self.project_root = Path(project_root).resolve() if project_root else None
        self.potcar_library = Path(potcar_library).expanduser() if potcar_library else None
        self.workflow_engine = workflow_engine
        self.audit = audit
        self.session_id = str(session_id or "")

    # -- lazily built stores (imported here to avoid cycles) -------------------
    @property
    def project_store(self) -> Any:
        from ..workflow.projects import ProjectStore
        return ProjectStore(projects_dir=self.config.projects_dir,
                            index_path=self.config.projects_index_path,
                            audit=self.audit)

    @property
    def skill_store(self) -> Any:
        from ..agents.skills import SkillStore
        return SkillStore(self.config.skills_dir, audit=self.audit)

    @property
    def pending_store(self) -> Any:
        from ..workflow.pending import PendingSubmitStore
        return PendingSubmitStore(self.config.pending_submits_path)

    def audit_record(self, event: str, **fields: Any) -> None:
        if self.audit is not None:
            try:
                self.audit.record(event, **fields)
            except Exception:
                pass


class ToolRegistry:
    """Name -> ToolDef map plus the enforcement of provider modes."""

    def __init__(self, context: ToolContext) -> None:
        self.context = context
        self._tools: dict[str, ToolDef] = {}
        self._register_all()

    # -- registry api ----------------------------------------------------------
    def get(self, name: str) -> ToolDef:
        if name not in self._tools:
            raise ValidationError(f"unknown tool {name!r}")
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def list_tools(self, *, kinds: tuple[str, ...] = ("read", "write")) -> list[ToolDef]:
        return [self._tools[n] for n in sorted(self._tools)
                if self._tools[n].kind in kinds]

    def dispatch(self, name: str, arguments: dict[str, Any] | None,
                 *, provider_mode: str = "full") -> dict[str, Any]:
        tool = self.get(name)
        args = arguments if isinstance(arguments, dict) else {}
        if tool.kind == "write" and provider_mode != "full":
            raise ToolNotAllowedError(
                f"tool {name} mutates remote state; the active provider is "
                f"{provider_mode} and may only use read tools")
        return tool.handler(args)

    # -- registration ------------------------------------------------------------
    def _add(self, name: str, description: str, kind: str,
             parameters: dict[str, Any], handler: Handler) -> None:
        self._tools[name] = ToolDef(
            name=name, description=description, kind=kind,
            parameters={"type": "object", "properties": parameters,
                        "additionalProperties": False},
            handler=handler)

    def _register_all(self) -> None:
        client = self.context.client

        # ---- servers / monitoring (read) ------------------------------------
        self._add(
            "server_list", "List registered HPC servers and their bounded roots.", "read",
            {}, lambda a: client.servers())
        self._add(
            "server_status", "Check whether a server has a reusable SSH session.", "read",
            {"server": _server_param()},
            lambda a: client.status(a.get("server")))
        self._add(
            "server_diagnostic",
            "Inspect one fixed aspect of a server: hostname, system, python, "
            "disk, quota, partitions, queues, modules or scheduler.", "read",
            {"server": _server_param(),
             "diagnostic": {"type": "string", "enum": [
                 "hostname", "system", "python", "disk", "quota",
                 "partitions", "queues", "modules", "scheduler"]}},
            lambda a: client.diagnostic(str(a.get("diagnostic", "system")),
                                        server=a.get("server")))

        # ---- remote filesystem (read) ---------------------------------------
        self._add(
            "remote_pwd", "Return the remote working root for a server.", "read",
            {"server": _server_param()}, lambda a: client.pwd(a.get("server")))
        self._add(
            "remote_list", "List one remote directory under the server root.", "read",
            {"server": _server_param(), "path": _path_param("remote directory")},
            lambda a: client.list_dir(str(a["path"]), server=a.get("server")))
        self._add(
            "remote_read",
            "Read a small remote text file. POTCAR and large scientific "
            "binaries are refused.", "read",
            {"server": _server_param(), "path": _path_param("remote file")},
            lambda a: self._read_text(a))
        self._add(
            "remote_tail", "Tail the last lines of a remote text file.", "read",
            {"server": _server_param(),
             "path": _path_param("remote file"),
             "lines": {"type": "integer", "minimum": 1, "maximum": 2000,
                       "default": 80}},
            lambda a: client.tail(str(a["path"]),
                                  lines=int(a.get("lines", 80)),
                                  server=a.get("server")))
        self._add(
            "remote_find", "Find files by name pattern under a remote directory.", "read",
            {"server": _server_param(), "path": _path_param("search root"),
             "pattern": {"type": "string", "maxLength": 128, "default": "*"},
             "max_depth": {"type": "integer", "minimum": 1, "maximum": 8,
                           "default": 2}},
            lambda a: client.find(str(a["path"]),
                                  pattern=str(a.get("pattern", "*")),
                                  max_depth=int(a.get("max_depth", 2)),
                                  server=a.get("server")))
        self._add(
            "remote_stat", "Type, size and mtime of one remote path.", "read",
            {"server": _server_param(), "path": _path_param("remote path")},
            lambda a: client.stat(str(a["path"]), server=a.get("server")))
        self._add(
            "remote_du", "Byte usage of one remote path.", "read",
            {"server": _server_param(), "path": _path_param("remote path")},
            lambda a: client.du(str(a["path"]), server=a.get("server")))
        self._add(
            "trash_list", "List recoverable trash entries for a server.", "read",
            {"server": _server_param()}, lambda a: client.trash_list(server=a.get("server")))

        # ---- jobs (read) ------------------------------------------------------
        self._add(
            "job_list", "Active scheduler jobs for a server.", "read",
            {"server": _server_param()}, lambda a: client.jobs(server=a.get("server")))
        self._add(
            "job_recent", "Recent scheduler history for a server.", "read",
            {"server": _server_param()},
            lambda a: client.recent_jobs(server=a.get("server")))
        self._add(
            "job_state",
            "Scheduler lifecycle state of one job. Scheduler state says "
            "nothing about scientific convergence.", "read",
            {"server": _server_param(),
             "job_id": {"type": "string", "pattern": r"^[0-9]{1,19}$"}},
            lambda a: client.job_state(str(a["job_id"]), server=a.get("server")))

        # ---- vasp science (read) ----------------------------------------------
        self._add(
            "vasp_progress",
            "Scientific progress of a remote calculation: ionic steps, "
            "energies, convergence flags. Independent from scheduler state.",
            "read",
            {"server": _server_param(),
             "directory": _path_param("remote calculation directory")},
            lambda a: client.vasp_progress(str(a["directory"]),
                                           server=a.get("server")))
        self._add(
            "vasp_validate",
            "Preflight-check the VASP inputs of a remote directory.", "read",
            {"server": _server_param(),
             "directory": _path_param("remote calculation directory")},
            lambda a: client.vasp_validate(str(a["directory"]),
                                           server=a.get("server")))

        # ---- potcar metadata (read, local) --------------------------------------
        self._add(
            "potcar_metadata",
            "Metadata (TITEL, ENMAX, functional, size, SHA-256) of POTCAR "
            "datasets in the authorized local library. Never returns "
            "pseudopotential content.", "read",
            {"variants": {"type": "array", "minItems": 1, "maxItems": 32,
                          "items": {"type": "string",
                                    "pattern": r"^[A-Za-z][A-Za-z0-9._+-]{0,63}$"}}},
            lambda a: self._potcar_metadata(a))

        # ---- remote filesystem (write) ------------------------------------------
        self._add(
            "remote_mkdir", "Create one remote directory under the server root.",
            "write",
            {"server": _server_param(), "path": _path_param("new directory")},
            lambda a: client.mkdir(str(a["path"]), server=a.get("server")))
        self._add(
            "remote_copy", "Copy a remote path inside the same server.", "write",
            {"server": _server_param(), "path": _path_param("source"),
             "destination": _path_param("destination")},
            lambda a: client.copy(str(a["path"]), str(a["destination"]),
                                  server=a.get("server")))
        self._add(
            "remote_move", "Move/rename a remote path inside the same server.", "write",
            {"server": _server_param(), "path": _path_param("source"),
             "destination": _path_param("destination")},
            lambda a: client.move(str(a["path"]), str(a["destination"]),
                                  server=a.get("server")))
        self._add(
            "remote_trash",
            "Move one remote path into the recoverable trash. The trash id "
            "is returned for restore/purge.", "write",
            {"server": _server_param(), "path": _path_param("path to trash")},
            lambda a: client.remove(str(a["path"]), server=a.get("server")))
        self._add(
            "remote_restore", "Restore a trash entry back to its original path.",
            "write",
            {"server": _server_param(),
             "trash_id": {"type": "string"}},
            lambda a: client.restore(str(a["trash_id"]), server=a.get("server")))

        # ---- file transfer (write) -------------------------------------------------
        self._add(
            "upload_file",
            "Upload one local file from the active project to a remote path "
            "with SHA-256 verification. Never overwrites existing remote "
            "content.", "write",
            {"server": _server_param(),
             "local_path": {"type": "string", "description": "file inside the project root"},
             "remote_path": _path_param("destination under the server root")},
            lambda a: self._upload(a))
        self._add(
            "download_file",
            "Download one remote file into the active project with SHA-256 "
            "verification. Existing local files are never overwritten.", "write",
            {"server": _server_param(),
             "path": _path_param("remote file"),
             "local_path": {"type": "string"}},
            lambda a: self._download(a))

        # ---- jobs (write) --------------------------------------------------------
        self._add(
            "job_submit",
            "Submit a job script inside a remote directory. Default mode "
            "pauses for human confirmation in the web UI (tell the user to "
            "approve the card, then check job_state next turn); an optional "
            "approval_ref from 'vaspilot workflow approve' submits directly.",
            "write",
            {"server": _server_param(),
             "directory": _path_param("remote directory"),
             "script": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$"},
             "approval_ref": {"type": "string",
                              "description": "optional approval token from workflow approve"}},
            lambda a: self._submit(a))

        # ---- server resource metrics (read) --------------------------------------
        self._add(
            "server_metrics",
            "Live node resources of a server: CPU/load/memory/disk, GPU "
            "telemetry (when present) and scheduler queue summary. Use this "
            "to pick the least-loaded server or partition before submitting.",
            "read",
            {"server": _server_param()}, lambda a: client.metrics(a.get("server")))

        # ---- shell (write; audit-only, no interception by design) ------------------
        self._add(
            "shell_run",
            "Run one shell command on the LOCAL Windows machine. Every "
            "command, working dir, exit code and output is fully audited. "
            "Timeouts kill the process.", "write",
            {"command": {"type": "string", "minLength": 1, "maxLength": 8000},
             "cwd": {"type": "string", "maxLength": 1024,
                     "description": "optional working directory"},
             "timeout_seconds": {"type": "integer", "minimum": 1,
                                 "maximum": 600, "default": 60}},
            lambda a: self._shell_run(a))
        self._add(
            "remote_run",
            "Run one shell command on a remote HPC server (login node) "
            "through the gateway. Fully audited on both sides.", "write",
            {"server": _server_param(),
             "command": {"type": "string", "minLength": 1, "maxLength": 8000},
             "timeout_seconds": {"type": "integer", "minimum": 1,
                                 "maximum": 600, "default": 120}},
            lambda a: client.run_command(
                str(a["command"]),
                timeout_seconds=int(a.get("timeout_seconds", 120)),
                server=a.get("server")))

        # ---- local projects ----------------------------------------------------------
        self._add(
            "project_create",
            "Create a local calculation project under ~/.vaspilot/projects "
            "with INCAR/KPOINTS/POSCAR text (blank strings are skipped). "
            "POTCAR cannot be written as text — ask the user for a local "
            "POTCAR path instead.", "write",
            {"name": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"},
             "incar": {"type": "string", "maxLength": 65536},
             "kpoints": {"type": "string", "maxLength": 65536},
             "poscar": {"type": "string", "maxLength": 262144},
             "potcar_remote": {"type": "string", "maxLength": 512,
                               "description": "optional HPC-side POTCAR "
                               "library path recorded for later assembly"}},
            lambda a: {**self.context.project_store.create(
                str(a["name"]),
                {"INCAR": str(a.get("incar") or ""),
                 "KPOINTS": str(a.get("kpoints") or ""),
                 "POSCAR": str(a.get("poscar") or "")},
                potcar_remote=str(a.get("potcar_remote") or "")), "ok": True})
        self._add(
            "project_list",
            "List local calculation projects with input-file completeness.",
            "read", {}, lambda a: {"ok": True,
                                   "projects": self.context.project_store.list()})
        self._add(
            "project_read",
            "Read one file of a local project. POTCAR returns metadata "
            "only (TITEL/ENMAX/size/sha256). Empty project = the session's "
            "active project.", "read",
            {"project": {"type": "string", "maxLength": 64},
             "file": {"type": "string", "enum": ["INCAR", "KPOINTS", "POSCAR",
                                                 "POTCAR", "run.job.sh"]}},
            lambda a: {"ok": True,
                       **self.context.project_store.read_file(
                           self._project_name(a), str(a["file"]))})
        self._add(
            "project_write",
            "Write one file of a local project (INCAR/KPOINTS/POSCAR/"
            "run.job.sh). POTCAR is refused — copy it via a user-supplied "
            "path instead. Empty project = the session's active project.",
            "write",
            {"project": {"type": "string", "maxLength": 64},
             "file": {"type": "string", "enum": ["INCAR", "KPOINTS", "POSCAR",
                                                 "run.job.sh"]},
             "content": {"type": "string", "maxLength": 262144}},
            lambda a: {"ok": True,
                       **self.context.project_store.write_file(
                           self._project_name(a), str(a["file"]),
                           str(a["content"]))})
        self._add(
            "project_validate",
            "Preflight-check a local project's VASP inputs (empty project = "
            "the session's active project).", "read",
            {"project": {"type": "string", "maxLength": 64}},
            lambda a: self.context.project_store.validate(self._project_name(a)))

        # ---- skills (self-evolution) ----------------------------------------------------
        self._add(
            "skill_list", "List saved skills (name + description).", "read",
            {}, lambda a: {"ok": True,
                           "skills": self.context.skill_store.list()})
        self._add(
            "skill_read",
            "Fetch the full guide of one saved skill.", "read",
            {"name": {"type": "string", "pattern": r"^[a-z0-9][a-z0-9-]{0,63}$"}},
            lambda a: {"ok": True,
                       **self.context.skill_store.read(str(a["name"]))})
        self._add(
            "skill_write",
            "Create or update a skill (max 16 KiB body, 50 skills) so later "
            "sessions automatically know the procedure. Deletion is "
            "human-only in the UI settings.", "write",
            {"name": {"type": "string", "pattern": r"^[a-z0-9][a-z0-9-]{0,63}$"},
             "description": {"type": "string", "minLength": 4, "maxLength": 200},
             "body": {"type": "string", "maxLength": 16384}},
            lambda a: {"ok": True,
                       **self.context.skill_store.write(
                           str(a["name"]), str(a["description"]),
                           str(a["body"]))})

        # ---- web (read) --------------------------------------------------------------------
        self._add(
            "web_search",
            "Search the web (literature values, reference data, error "
            "fixes). Requires a configured search API key.", "read",
            {"query": {"type": "string", "minLength": 2, "maxLength": 400}},
            lambda a: self._web_search(a))
        self._add(
            "web_fetch",
            "Fetch one public http(s) page as plain text (HTML stripped, "
            "50 KiB cap). Private/internal targets are refused.", "read",
            {"url": {"type": "string", "minLength": 8, "maxLength": 2048}},
            lambda a: self._web_fetch(a))

        # ---- workflow (read-only previews; approve/run are local-only) ------------
        if self.context.workflow_engine is not None:
            engine = self.context.workflow_engine
            self._add(
                "workflow_preview",
                "Build a deterministic workflow plan (plan_id, plan_hash, "
                "files, steps, risk summary) without touching any server.", "read",
                {"spec": {"type": "object"}},
                lambda a: engine.preview(a.get("spec") or {}))
            self._add(
                "workflow_status", "Show the persisted state of one workflow run.",
                "read",
                {"plan_id": {"type": "string", "pattern": r"^[0-9a-f]{16}$"}},
                lambda a: engine.status(str(a["plan_id"])))

    # -- handlers needing context ------------------------------------------------
    def _read_text(self, args: dict[str, Any]) -> dict[str, Any]:
        path = str(args["path"])
        if denied_text_file(Path(path).name):
            raise ValidationError(
                f"{Path(path).name} may not be read as text through tools")
        return self.context.client.read(path, server=args.get("server"))

    def _potcar_metadata(self, args: dict[str, Any]) -> dict[str, Any]:
        library = self.context.potcar_library
        if library is None or not library.is_dir():
            raise ValidationError(
                "no authorized local POTCAR library configured "
                "(VASPILOT_POTCAR_LIBRARY)")
        datasets = []
        for variant in args.get("variants", []):
            path = library / str(variant) / "POTCAR"
            if not path.is_file():
                path = library / f"POTCAR.{variant}"
            if not path.is_file():
                raise ValidationError(
                    f"POTCAR variant {variant!r} not found in the library")
            metadata = potcar_metadata(path.read_text(encoding="utf-8",
                                                      errors="replace"))
            if metadata is None:
                raise ValidationError(f"{path.name} is not a readable POTCAR")
            metadata["sha256"] = file_sha256(path)
            metadata["variant"] = str(variant)
            datasets.append(metadata)
        return {"library": str(library), "datasets": datasets}

    def _upload(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.context.project_root is None:
            raise ValidationError("no project root bound for uploads")
        from ..core.validation import local_project_path
        local = local_project_path(str(args["local_path"]),
                                   project_root=self.context.project_root)
        if not local.is_file():
            raise ValidationError(f"local file not found: {local}")
        return self.context.client.upload(
            local, str(args["remote_path"]), server=args.get("server"),
            expected_sha256=file_sha256(local))

    def _download(self, args: dict[str, Any]) -> dict[str, Any]:
        if self.context.project_root is None:
            raise ValidationError("no project root bound for downloads")
        from ..core.validation import local_project_path
        local = local_project_path(str(args["local_path"]),
                                   project_root=self.context.project_root)
        return self.context.client.download(
            str(args["path"]), local, server=args.get("server"))

    def _submit(self, args: dict[str, Any]) -> dict[str, Any]:
        server = str(args.get("server") or "")
        directory = str(args["directory"])
        script = str(args["script"])
        approval_ref = str(args.get("approval_ref") or "").strip()
        if approval_ref:
            # legacy one-shot approval: verify + consume + submit directly
            if self.context.workflow_engine is not None:
                self.context.workflow_engine.verify_submit_approval(
                    approval_ref, server=server,
                    directory=directory, script=script)
            result = self.context.client.submit(
                directory, script, approval_ref=approval_ref, server=server)
            if self.context.workflow_engine is not None:
                self.context.workflow_engine.mark_approval_consumed(approval_ref)
            return result
        mode = self.context.config.agent_submit_mode()
        if mode == "auto":
            return self.context.client.submit(directory, script, server=server)
        # confirm mode: freeze the exact parameters for a human click
        script_content = ""
        script_sha = ""
        try:
            remote = f"{directory.rstrip('/')}/{script}"
            doc = self.context.client.read(remote, server=server)
            script_content = str(doc.get("content", ""))
            from ..core.hashing import text_sha256
            script_sha = text_sha256(script_content)
        except Exception:
            script_content = ""  # card will show "content unavailable"
        entry = self.context.pending_store.create(
            server=server or self.context.config.default_server(),
            directory=directory, script=script,
            script_sha256=script_sha, script_content=script_content,
            session_id=self.context.session_id)
        return {
            "ok": True, "status": "pending_confirmation",
            "id": entry["id"], "expires_at": entry["expires_at"],
            "server": entry["server"], "directory": directory,
            "message": "submission queued for human confirmation: ask the "
                       "user to approve the card in the web UI, then verify "
                       "with job_state in the next turn",
        }

    # -- local shell -----------------------------------------------------------------
    def _shell_run(self, args: dict[str, Any]) -> dict[str, Any]:
        import subprocess
        command = str(args.get("command") or "").strip()
        if not command:
            raise ValidationError("command is required")
        timeout = max(1, min(int(args.get("timeout_seconds", 60) or 60), 600))
        cwd = str(args.get("cwd") or "").strip() or None
        if cwd and not Path(cwd).is_dir():
            raise ValidationError(f"cwd is not a directory: {cwd}")
        try:
            completed = subprocess.run(
                command, shell=True, cwd=cwd, capture_output=True,
                timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or b"").decode("utf-8", "replace")
            stderr = (exc.stderr or b"").decode("utf-8", "replace")
            self.context.audit_record(
                "shell.run", outcome="timeout", command=command, cwd=cwd,
                output=(stdout + stderr)[:262144])
            return {"ok": False, "timeout": True, "rc": None,
                    "stdout": stdout[:20000], "stderr": stderr[:20000],
                    "message": f"command exceeded {timeout}s and was killed"}
        stdout = completed.stdout.decode("utf-8", "replace")
        stderr = completed.stderr.decode("utf-8", "replace")
        self.context.audit_record(
            "shell.run", outcome="ok" if completed.returncode == 0 else "fail",
            command=command, cwd=cwd, rc=completed.returncode,
            output=(stdout + stderr)[:262144])
        total = len(stdout) + len(stderr)
        return {"ok": completed.returncode == 0, "rc": completed.returncode,
                "stdout": stdout[:20000], "stderr": stderr[:20000],
                "truncated": total > 40000}

    # -- web -----------------------------------------------------------------------------
    def _web_search(self, args: dict[str, Any]) -> dict[str, Any]:
        from . import web
        settings = self.context.config.websearch()
        if not settings["enabled"]:
            raise ValidationError(
                "web search is disabled; enable it in the UI settings")
        result = web.web_search(str(args["query"]),
                                provider=settings["provider"],
                                api_key=self.context.config.websearch_key())
        self.context.audit_record(
            "web.search", outcome="ok", query=result["query"],
            provider=result["provider"], results=len(result["results"]))
        return {"ok": True, **result}

    def _web_fetch(self, args: dict[str, Any]) -> dict[str, Any]:
        from . import web
        result = web.web_fetch(str(args["url"]))
        self.context.audit_record(
            "web.fetch", outcome="ok", url=result["url"],
            bytes=len(result["text"]))
        return {"ok": True, **result}

    # -- local projects -----------------------------------------------------------------
    def _project_name(self, args: dict[str, Any]) -> str:
        name = str(args.get("project") or "").strip()
        if name:
            return name
        root = self.context.project_root
        store_root = self.context.config.projects_dir.resolve()
        if root is not None and root.parent.resolve() == store_root:
            return root.name
        raise ValidationError(
            "no project name given and the session has no active project")
