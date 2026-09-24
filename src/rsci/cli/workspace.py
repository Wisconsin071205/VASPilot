"""``huwei workspace ...`` — Vlab 完整工作区的结构化控制命令。

挂载的实际管理始终在 Vlab 的 Workspace Gateway 中完成；本模块只做
参数校验与 JSON 转发，绝不拼接或执行目标服务器 shell 命令。
"""

from __future__ import annotations


def register(sub) -> None:
    parser = sub.add_parser("workspace", help="Vlab rclone complete workspaces")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name, handler, **kw):
        child = commands.add_parser(name, **kw)
        child.set_defaults(handler=handler)
        return child

    doctor = command("doctor", cmd_doctor)
    doctor.add_argument("--server", default="")
    doctor.add_argument("--path", default="")
    open_p = command("open", cmd_open)
    open_p.add_argument("--server", required=True)
    open_p.add_argument("--path", required=True)
    open_p.add_argument("--mode", choices=["full", "read-only"], default="full")
    status = command("status", cmd_status)
    status.add_argument("--workspace", default="")
    command("list", cmd_list)
    close = command("close", cmd_close)
    close.add_argument("--workspace", required=True)
    close.add_argument("--wait", type=int, default=60)
    recover = command("recover", cmd_recover)
    recover.add_argument("--workspace", default="")
    recover.add_argument("--action", choices=["list", "retry", "keep", "discard"],
                         default="list")
    recover.add_argument("--confirm-workspace-id", default="")
    cleanup = command("cleanup", cmd_cleanup)
    cleanup.add_argument("--apply", action="store_true")
    cleanup.add_argument("--confirm", default="")


def cmd_doctor(app, args):
    return app.client().workspace_doctor(server=args.server, path=args.path)


def cmd_open(app, args):
    return app.client().workspace_open(args.path, server=args.server, mode=args.mode)


def cmd_status(app, args):
    return app.client().workspace_status(args.workspace)


def cmd_list(app, args):
    return app.client().workspace_list()


def cmd_close(app, args):
    return app.client().workspace_close(args.workspace, wait_seconds=args.wait)


def cmd_recover(app, args):
    return app.client().workspace_recover(
        workspace_id=args.workspace, action=args.action,
        confirm_workspace_id=args.confirm_workspace_id)


def cmd_cleanup(app, args):
    return app.client().workspace_cleanup(apply=bool(args.apply), confirm=args.confirm)
