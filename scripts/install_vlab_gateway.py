#!/usr/bin/env python3
"""Deploy both VaspLoom gateway helpers onto the Vlab host.

scp the gateway script to a staging path, normalize CRLF, byte-compile it for
validation, then atomically replace the live helper. The previous version
stays untouched when validation fails. No credentials are handled here: ssh
uses your key/agent exactly like every other VaspLoom operation.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATEWAY = REPO / "src" / "vasploom" / "gateway" / "vasploom_gateway.py"
WORKSPACE_GATEWAY = (REPO / "gateway" / "huwei-workspace-gateway"
                     / "huwei_workspace_gateway.py")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity-file", default="",
                        help="Vlab PEM path (or set VASPLOOM_IDENTITY_FILE)")
    parser.add_argument("--host", default="vlab.ustc.edu.cn")
    parser.add_argument("--user", default="ubuntu")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--remote-path", default="~/bin/vasploom-gateway")
    parser.add_argument("--workspace-remote-path",
                        default="~/bin/huwei-workspace-gateway")
    args = parser.parse_args()

    for source in (GATEWAY, WORKSPACE_GATEWAY):
        if not source.is_file():
            print(f"gateway script not found: {source}", file=sys.stderr)
            return 1
    identity = args.identity_file.strip()
    if not identity:
        import os
        identity = os.environ.get("VASPLOOM_IDENTITY_FILE", "").strip()
    if not identity:
        print("provide --identity-file or set VASPLOOM_IDENTITY_FILE",
              file=sys.stderr)
        return 1

    target = f"{args.user}@{args.host}"
    base = ["-i", identity, "-o", "StrictHostKeyChecking=yes",
            "-o", "UpdateHostKeys=no", "-p", str(args.port)]

    print(f"deploying the gateway helper to {target}:{args.remote_path} ...")
    prepare = subprocess.run(
        ["ssh", "-q", *base, target,
         "mkdir -p ~/bin ~/.config/vasploom ~/.cache/vasploom && "
         "mkdir -p ~/.huwei-agent/workspaces && "
         "chmod 700 ~/bin ~/.config/vasploom ~/.cache/vasploom "
         "~/.huwei-agent ~/.huwei-agent/workspaces"])
    if prepare.returncode != 0:
        print("could not prepare the Vlab directories", file=sys.stderr)
        return prepare.returncode

    helpers = (
        (GATEWAY, args.remote_path, "/tmp/vasploom-gateway.new"),
        (WORKSPACE_GATEWAY, args.workspace_remote_path,
         "/tmp/huwei-workspace-gateway.new"),
    )
    for source, remote_path, stage in helpers:
        copy = subprocess.run(
            ["scp", "-q", "-P", str(args.port), "-i", identity,
             "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
             str(source), f"{target}:{stage}"])
        if copy.returncode != 0:
            print(f"could not stage {source.name}", file=sys.stderr)
            return copy.returncode

        install = subprocess.run(
            ["ssh", "-q", *base, target,
             f"sed -i 's/\\r//' {stage} && chmod 700 {stage} && "
             f"python3 -m py_compile {stage} && "
             f"mv -f -- {stage} {remote_path} && "
             f"{remote_path} version"])
        if install.returncode != 0:
            subprocess.run(["ssh", "-q", *base, target,
                            f"rm -f -- {stage}"])
            print(f"validation failed for {source.name}; the previous helper "
                  "is unchanged", file=sys.stderr)
            return install.returncode
        print(f"installed {source.name} at {remote_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
