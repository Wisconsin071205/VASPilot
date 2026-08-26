"""``vaspilot workflow ...`` — plan, approve, execute, resume."""

from __future__ import annotations

import json
from pathlib import Path

from ..core.errors import ValidationError
from ..core.validation import valid_server_name
from ..hpc.vasp import validate_inputs


def register(sub) -> None:
    parser = sub.add_parser("workflow", help="approval-gated VASP workflows")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, handler, **kw):
        child = commands.add_parser(name, **kw)
        child.set_defaults(handler=handler)
        return child

    p = command("prepare", cmd_prepare)
    p.add_argument("--from-dir", required=True,
                   help="local directory with INCAR/KPOINTS/POSCAR(/POTCAR)")
    p.add_argument("--server", required=True)
    p.add_argument("--remote-dir", required=True)
    p.add_argument("--scheduler", choices=["slurm", "pbs"])
    p.add_argument("--job-name", default="vaspilot")
    p.add_argument("--partition", default="")
    p.add_argument("--ntasks", type=int, default=8)
    p.add_argument("--walltime", default="24:00:00")
    p.add_argument("--vasp-executable", dest="vasp_executable",
                   default="vasp_std")
    p.add_argument("--skip-potcar", dest="skip_potcar", action="store_true",
                   help="plan without POTCAR (e.g. remote assembly later)")

    p = command("validate", cmd_validate)
    p.add_argument("directory")
    p.add_argument("--server", default=None,
                   help="omit to validate the local directory instead")
    p.add_argument("--local", action="store_true",
                   help="force local validation of DIRECTORY")

    p = command("preview", cmd_preview)
    p.add_argument("plan_id", nargs="?",
                   help="existing plan id; omit to preview a fresh spec")
    p.add_argument("--from-dir")
    p.add_argument("--server")
    p.add_argument("--remote-dir")

    p = command("approve", cmd_approve)
    p.add_argument("plan_id")
    p.add_argument("--validity-hours", dest="validity_hours", type=int,
                   default=24)
    p.add_argument("--yes", action="store_true",
                   help="skip the interactive phrase (automation only; the "
                        "caller asserts a human reviewed the preview)")

    p = command("approve-submit", cmd_approve_submit)
    p.add_argument("--server", required=True)
    p.add_argument("--directory", required=True)
    p.add_argument("--script", required=True)
    p.add_argument("--validity-hours", dest="validity_hours", type=int,
                   default=24)

    p = command("run", cmd_run)
    p.add_argument("plan_id")
    p.add_argument("--approval-ref", dest="approval_ref", required=True)
    p.add_argument("--poll-seconds", dest="poll_seconds", type=int, default=None)
    p.add_argument("--download-root", dest="download_root", default=None)
    p.add_argument("--force", action="store_true",
                   help="proceed even if a stale attempt is marked running")

    p = command("resume", cmd_resume)
    p.add_argument("plan_id")
    p.add_argument("--approval-ref", dest="approval_ref", required=True)
    p.add_argument("--poll-seconds", dest="poll_seconds", type=int, default=None)
    p.add_argument("--download-root", dest="download_root", default=None)

    p = command("status", cmd_status)
    p.add_argument("plan_id")


def _spec_from_args(args) -> dict:
    spec = {
        "from_dir": args.from_dir,
        "server": args.server,
        "remote_dir": args.remote_dir,
        "job_name": getattr(args, "job_name", None),
        "partition": getattr(args, "partition", None),
        "ntasks": getattr(args, "ntasks", None),
        "walltime": getattr(args, "walltime", None),
        "vasp_executable": getattr(args, "vasp_executable", None),
        "skip_potcar": getattr(args, "skip_potcar", False),
    }
    if getattr(args, "scheduler", None):
        spec["scheduler"] = args.scheduler
    return spec


def cmd_prepare(app, args):
    spec = _spec_from_args(args)
    result = app.engine().prepare(spec)
    return {"plan_id": result["plan_id"],
            "plan_hash": result["plan_hash"],
            "files_hash": result["files_hash"],
            "plan_file": result["plan_file"],
            "plan": result["plan"],
            "next": "review the plan, then run 'vaspilot workflow approve'"}


def cmd_validate(app, args):
    if args.local or not args.server:
        directory = Path(args.directory).expanduser()
        if not directory.is_dir():
            raise ValidationError(f"local directory not found: {directory}")
        files = {}
        for name in ("INCAR", "KPOINTS", "POSCAR"):
            path = directory / name
            if path.is_file():
                files[name] = path.read_text(encoding="utf-8", errors="replace")
        result = validate_inputs(files, require_potcar=False)
        result["directory"] = str(directory)
        return result
    return app.client().vasp_validate(args.directory, server=args.server)


def cmd_preview(app, args):
    if args.plan_id:
        plan = app.engine().load_plan(args.plan_id)
        from ..workflow.plan import plan_files_hash
        return {"plan": plan, "files_hash": plan_files_hash(plan),
                "plan_id": plan["plan_id"]}
    if not (args.from_dir and args.server and args.remote_dir):
        raise ValidationError(
            "preview needs either PLAN_ID or --from-dir/--server/--remote-dir")
    valid_server_name(args.server)
    return app.engine().preview(_spec_from_args(args))


def cmd_approve(app, args):
    """LOCAL interactive approval; mints the one-shot reference."""
    return app.engine().approve(args.plan_id,
                                validity_hours=args.validity_hours,
                                confirmed=args.yes)


def cmd_approve_submit(app, args):
    return app.engine().approve_submit(
        server=args.server, directory=args.directory, script=args.script,
        validity_hours=args.validity_hours)


def cmd_run(app, args):
    return app.engine().run(args.plan_id, args.approval_ref,
                            poll_seconds=args.poll_seconds,
                            download_root=args.download_root,
                            force=args.force)


def cmd_resume(app, args):
    return app.engine().resume(args.plan_id, args.approval_ref,
                               poll_seconds=args.poll_seconds,
                               download_root=args.download_root)


def cmd_status(app, args):
    return app.engine().status(args.plan_id)
