#!/usr/bin/env python3
"""Deploy the VASPilot gateway helper onto the Vlab host.

scp the gateway script to a staging path, normalize CRLF, byte-compile it for
validation, then atomically replace the live helper. The previous version
stays untouched when validation fails. No credentials are handled here: ssh
uses your key/agent exactly like every other VASPilot operation.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATEWAY = REPO / "src" / "vaspilot" / "gateway" / "vaspilot_gateway.py"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity-file", default="",
                        help="Vlab PEM path (or set VASPILOT_IDENTITY_FILE)")
    parser.add_argument("--host", default="vlab.ustc.edu.cn")
    parser.add_argument("--user", default="ubuntu")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--remote-path", default="~/bin/vaspilot-gateway")
    args = parser.parse_args()

    if not GATEWAY.is_file():
        print(f"gateway script not found: {GATEWAY}", file=sys.stderr)
        return 1
    identity = args.identity_file.strip()
    if not identity:
        import os
        identity = os.environ.get("VASPILOT_IDENTITY_FILE", "").strip()
    if not identity:
        print("provide --identity-file or set VASPILOT_IDENTITY_FILE",
              file=sys.stderr)
        return 1

    target = f"{args.user}@{args.host}"
    base = ["-i", identity, "-o", "StrictHostKeyChecking=yes",
            "-o", "UpdateHostKeys=no", "-p", str(args.port)]

    print(f"deploying the gateway helper to {target}:{args.remote_path} ...")
    prepare = subprocess.run(
        ["ssh", "-q", *base, target,
         "mkdir -p ~/bin ~/.config/vaspilot ~/.cache/vaspilot && "
         "chmod 700 ~/bin ~/.config/vaspilot ~/.cache/vaspilot"])
    if prepare.returncode != 0:
        print("could not prepare the Vlab directories", file=sys.stderr)
        return prepare.returncode

    stage = "/tmp/vaspilot-gateway.new"
    copy = subprocess.run(
        ["scp", "-q", "-P", str(args.port), "-i", identity,
         "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
         str(GATEWAY), f"{target}:{stage}"])
    if copy.returncode != 0:
        print("could not stage the gateway script", file=sys.stderr)
        return copy.returncode

    install = subprocess.run(
        ["ssh", "-q", *base, target,
         f"sed -i 's/\\r//' {stage} && chmod 700 {stage} && "
         f"python3 -m py_compile {stage} && "
         f"mv -f -- {stage} {args.remote_path} && "
         f"{args.remote_path} version"])
    if install.returncode != 0:
        subprocess.run(["ssh", "-q", *base, target, f"rm -f -- {stage}"])
        print("validation failed on Vlab; the previous gateway is unchanged",
              file=sys.stderr)
        return install.returncode
    print("installed; the gateway version line is printed above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
