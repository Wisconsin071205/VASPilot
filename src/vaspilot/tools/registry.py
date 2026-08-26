"""The named tool registry — the single model-facing action surface.

Every consumer (CLI ``agent chat/run``, the MCP server, the Codex plugin)
dispatches through this registry. There is deliberately NO shell tool and no
path-freeform tool: each entry validates its arguments through
:mod:`vaspilot.core.validation` and calls one :class:`GatewayClient` method.

Kinds:
  read      — safe for any provider, including degraded analysis_only ones
  write     — mutates remote state (mkdir/copy/move/trash/restore/upload/
              download/submit); requires a provider whose capability probe
              passed everything, and destructive entries carry their own
              double-confirm gate
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
                 workflow_engine: Any = None) -> None:
        self.config = config
        self.client = client
        self.project_root = Path(project_root).resolve() if project_root else None
        self.potcar_library = Path(potcar_library).expanduser() if potcar_library else None
        self.workflow_engine = workflow_engine


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
            "Submit a job script inside a remote directory. Requires an "
            "approval_ref issued by the local trusted approval flow; the "
            "model cannot create one.", "write",
            {"server": _server_param(),
             "directory": _path_param("remote directory"),
             "script": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$"},
             "approval_ref": {"type": "string",
                              "description": "approval token from workflow approve"}},
            lambda a: self._submit(a))

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
        approval_ref = str(args.get("approval_ref") or "").strip()
        if not approval_ref:
            raise ValidationError(
                "job_submit requires an approval_ref from 'vaspilot workflow "
                "approve'; the model cannot mint one")
        if self.context.workflow_engine is not None:
            # verify + consume the one-shot approval reference
            self.context.workflow_engine.verify_submit_approval(
                approval_ref,
                server=str(args.get("server") or ""),
                directory=str(args["directory"]),
                script=str(args["script"]))
        result = self.context.client.submit(
            str(args["directory"]), str(args["script"]),
            approval_ref=approval_ref, server=args.get("server"))
        if self.context.workflow_engine is not None:
            self.context.workflow_engine.mark_approval_consumed(approval_ref)
        return result
