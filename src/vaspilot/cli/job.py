"""``vaspilot job ...`` — scheduler jobs and VASP scientific status."""

from __future__ import annotations


def register(sub) -> None:
    parser = sub.add_parser("job", help="scheduler jobs and VASP progress")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, handler, **kw):
        child = commands.add_parser(name, **kw)
        child.set_defaults(handler=handler)
        child.add_argument("--server", default=None)
        return child

    command("list", cmd_list)
    command("recent", cmd_recent)
    p = command("submit", cmd_submit)
    p.add_argument("directory")
    p.add_argument("script")
    p.add_argument("--approval-ref", dest="approval_ref", required=True,
                   help="approval reference from 'vaspilot workflow approve "
                        "--for-submit' (cannot be minted by a model)")
    p = command("cancel", cmd_cancel)
    p.add_argument("job_id")
    p.add_argument("--confirm-job-id", dest="confirm_job_id", required=True,
                   help="must repeat the job id exactly (double match)")
    p = command("progress", cmd_progress)
    p.add_argument("directory")
    p = command("diagnose", cmd_diagnose)
    p.add_argument("directory")


def cmd_list(app, args):
    return app.client().jobs(server=args.server)


def cmd_recent(app, args):
    return app.client().recent_jobs(server=args.server)


def cmd_submit(app, args):
    """Submit with a verified one-shot approval reference."""
    client = app.client()
    server = client._require(args.server)
    app.engine().verify_submit_approval(args.approval_ref,
                                        server=server,
                                        directory=args.directory,
                                        script=args.script)
    return client.submit(args.directory, args.script,
                         approval_ref=args.approval_ref, server=server)


def cmd_cancel(app, args):
    """Cancel needs a double-matched job id."""
    return app.client().cancel(args.job_id, args.confirm_job_id,
                               server=args.server)


def cmd_progress(app, args):
    """Scientific progress. Scheduler state is queried separately."""
    progress = app.client().vasp_progress(args.directory, server=args.server)
    progress["note"] = ("scheduler state is a separate dimension; "
                        "scientific_converged is the scientific verdict")
    return progress


def cmd_diagnose(app, args):
    """Combine scheduler state, scientific progress and validation issues."""
    progress = app.client().vasp_progress(args.directory, server=args.server)
    validate = app.client().vasp_validate(args.directory, server=args.server)
    return {
        "directory": args.directory,
        "scheduler_state": None,
        "scheduler_note": "query 'vaspilot job list/recent' for scheduler "
                          "state; it is never a proxy for convergence",
        "scientific": {k: v for k, v in progress.items()
                       if k not in ("directory",)},
        "input_issues": {"errors": validate.get("errors", []),
                         "warnings": validate.get("warnings", [])},
    }
