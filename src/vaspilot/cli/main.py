"""VASPilot CLI entry point.

Every command prints exactly one stable JSON document (or one JSON line per
event for ``monitor watch``) and exits with a documented code:

  0  ok
  1  error
  2  usage
  3  auth_required (SSH session missing/expired; never auto-filled)
  4  approval required/invalid
  5  validation error
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from .. import __version__
from ..core.audit import AuditLog
from ..core.config import Config
from ..core.errors import EXIT_USAGE, VaspilotError
from ..core.jsonio import emit, emit_error
from ..gateway.client import GatewayClient
from ..gateway.transport import SshTransport
from ..tools.registry import ToolContext, ToolRegistry
from ..workflow.engine import WorkflowEngine


class App:
    """Lazily-wired application context shared by all subcommands."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.audit = AuditLog(config.audit_dir)
        self._transport: SshTransport | None = None
        self._client: GatewayClient | None = None
        self._engine: WorkflowEngine | None = None
        self._registry: ToolRegistry | None = None

    def transport(self) -> SshTransport:
        if self._transport is None:
            vlab = self.config.vlab
            # A missing identity is NOT fatal here: ssh may use an agent or
            # default keys, and 'server doctor' reports the gap explicitly.
            # Real auth failures surface as auth_required at call time.
            self._transport = SshTransport(
                host=vlab["host"], user=vlab["user"], port=vlab["port"],
                identity_file=self.config.identity_file(),
                gateway_path=vlab["gateway_path"])
        return self._transport

    def client(self) -> GatewayClient:
        if self._client is None:
            self._client = GatewayClient(self.config, self.transport(),
                                         self.audit)
        return self._client

    def engine(self) -> WorkflowEngine:
        if self._engine is None:
            self._engine = WorkflowEngine(config=self.config,
                                          client=self.client(),
                                          audit=self.audit)
        return self._engine

    def registry(self, *, project_root=None, potcar_library=None) -> ToolRegistry:
        if self._registry is None:
            import os
            from pathlib import Path
            context = ToolContext(
                config=self.config,
                client=self.client(),
                project_root=Path(project_root).resolve()
                if project_root else None,
                potcar_library=Path(potcar_library).expanduser()
                if potcar_library else
                (Path(os.environ["VASPILOT_POTCAR_LIBRARY"]).expanduser()
                 if os.environ.get("VASPILOT_POTCAR_LIBRARY") else None),
                workflow_engine=self.engine())
            self._registry = ToolRegistry(context)
        return self._registry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vaspilot",
        description="CLI-first, multi-model VASP/HPC agent")
    parser.add_argument("--version", action="version",
                        version=f"vaspilot {__version__}")
    parser.add_argument("--config-dir", default=None,
                        help="override the configuration directory "
                             "(default: ~/.vaspilot, env VASPILOT_HOME)")
    sub = parser.add_subparsers(dest="group", required=True)

    from .server import register as register_server
    from .remote import register as register_remote
    from .job import register as register_job
    from .workflow import register as register_workflow
    from .monitor import register as register_monitor
    from .agent import register as register_agent
    register_server(sub)
    register_remote(sub)
    register_job(sub)
    register_workflow(sub)
    register_monitor(sub)
    register_agent(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = Config(args.config_dir)
    app = App(config)
    handler: Any = getattr(args, "handler", None)
    if handler is None:
        emit_error({"code": "usage_error",
                    "message": f"unknown command group {args.group}"})
        return EXIT_USAGE
    try:
        payload = handler(app, args)
        if payload is not None:
            if isinstance(payload, dict):
                payload.setdefault("ok", True)
            else:
                payload = {"ok": True, "result": payload}
            emit(payload)
        return 0
    except VaspilotError as exc:
        emit_error(exc.to_dict())
        return exc.exit_code
    except KeyboardInterrupt:
        emit_error({"code": "interrupted", "message": "interrupted by user"})
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
