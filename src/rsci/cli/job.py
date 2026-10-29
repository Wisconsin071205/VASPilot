"""``rsci job ...`` — scheduler jobs and VASP scientific status."""

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
                   help="approval reference from 'rsci workflow approve "
                        "--for-submit' (cannot be minted by a model)")
    p = command("cancel", cmd_cancel)
    p.add_argument("job_id")
    p.add_argument("--confirm-job-id", dest="confirm_job_id", required=True,
                   help="must repeat the job id exactly (double match)")
    p = command("progress", cmd_progress)
    p.add_argument("directory")
    p = command("diagnose", cmd_diagnose)
    p.add_argument("directory")
    command("fleet", cmd_fleet)
    for name, handler in (("details", cmd_details), ("analyze", cmd_analyze)):
        p = command(name, handler)
        p.add_argument("job_id")
    p = command("associate", cmd_associate)
    p.add_argument("job_id")
    p.add_argument("project")
    p = command("logs", cmd_logs)
    p.add_argument("job_id")
    p.add_argument("path")
    p = command("collect", cmd_collect)
    p.add_argument("job_id")
    p.add_argument("paths", nargs="+")
    command("collections", cmd_collections)
    for name, handler in (("resume-collection", cmd_resume_collection),
                          ("stop-collection", cmd_stop_collection), ("_collect", cmd_worker)):
        p = command(name, handler)
        p.add_argument("task_id")


def _center(app):
    from ..hpc.center import RemoteCenter
    return RemoteCenter(app.config, app.client(), app.audit)


def _queue(app):
    from ..hpc.collections import Collections
    return Collections(app.config, app.client(), app.audit)


def _server(app, args):
    return args.server or app.config.default_server()


def cmd_fleet(app, args):
    return _center(app).overview(args.server or "")


def cmd_details(app, args):
    return _center(app).details(_server(app, args), args.job_id)


def cmd_analyze(app, args):
    return _center(app).diagnose(_server(app, args), args.job_id)


def cmd_associate(app, args):
    return _center(app).associate(_server(app, args), args.job_id, args.project)


def cmd_logs(app, args):
    return _center(app).text(_server(app, args), args.job_id, args.path)


def cmd_collect(app, args):
    from ..hpc.collections import launch
    queue = _queue(app)
    state = queue.enqueue(_server(app, args), args.job_id, args.paths)
    return launch(queue, state["task_id"])


def cmd_collections(app, args):
    return _queue(app).list()


def cmd_resume_collection(app, args):
    return _queue(app).resume(args.task_id)


def cmd_stop_collection(app, args):
    return _queue(app).cancel(args.task_id)


def cmd_worker(app, args):
    _queue(app).execute(args.task_id)
    return {"ok": True, "task_id": args.task_id}


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
        "scheduler_note": "query 'rsci job list/recent' for scheduler "
                          "state; it is never a proxy for convergence",
        "scientific": {k: v for k, v in progress.items()
                       if k not in ("directory",)},
        "input_issues": {"errors": validate.get("errors", []),
                         "warnings": validate.get("warnings", [])},
    }
