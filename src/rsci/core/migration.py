"""Explicit local configuration migration after an application rename.

Credentials are decrypted only in memory and re-encrypted for this app.
The source stays intact; an already configured destination is refused.
Legacy encrypted settings use the entropy prefixes ``VaspLoom`` or
``VASPilot``; supply the exact source prefix used by the old installation.
"""
from __future__ import annotations

import argparse
import copy
import json
import shutil
from pathlib import Path

from . import secretbox
from .config import _atomic_write_json


def migrate(source: Path, target: Path, source_entropy_prefix: str) -> dict:
    source, target = source.resolve(), target.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("Source and destination must be separate directories")
    if (target / "settings.json").exists() or (target / "servers.json").exists():
        raise ValueError("Destination already has configuration; refusing to overwrite")
    original = json.loads((source / "settings.json").read_text(encoding="utf-8-sig"))
    settings = copy.deepcopy(original)
    prefix = source_entropy_prefix.encode("utf-8")
    count = 0

    def reencrypt(blob: str, old_entropy: bytes, new_entropy: bytes) -> str:
        nonlocal count
        plaintext = secretbox.unprotect_b64(blob, entropy=old_entropy)
        encrypted = secretbox.protect_b64(plaintext, entropy=new_entropy)
        if secretbox.unprotect_b64(encrypted, entropy=new_entropy) != plaintext:
            raise ValueError("Credential verification failed")
        count += 1
        return encrypted

    for key, blob in settings.get("provider_keys", {}).items():
        if blob:
            settings["provider_keys"][key] = reencrypt(
                blob, prefix + b"-provider-key/v1", secretbox.APP_ENTROPY)
    for name, entry in settings.get("server_credentials", {}).items():
        for field in ("password", "totp_seed"):
            if entry.get(field):
                entry[field] = reencrypt(
                    entry[field], prefix + b"-server-credential/v1:" + name.encode("utf-8"),
                    secretbox.server_credential_entropy(name))

    # Check the entire copy plan before writing anything. Never move old data
    # or replace the live UI discovery token and lock files.
    files = []
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError("Source contains a symbolic link")
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if relative.as_posix() in ("settings.json", "ui.json") or path.suffix == ".lock":
            continue
        destination = target / relative
        if destination.exists():
            if path.read_bytes() != destination.read_bytes():
                raise ValueError("Destination contains conflicting data; refusing to overwrite")
            continue
        files.append((path, destination))
    for path, destination in files:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    # Publish configuration only after the ciphertext and supporting data
    # have been prepared successfully.
    _atomic_write_json(target / "settings.json", settings)
    return {"copied_files": len(files), "reencrypted_credentials": count,
            "providers": len(settings.get("providers", []))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument(
        "--source-entropy-prefix", required=True,
        help="exact legacy DPAPI prefix (for example VaspLoom or VASPilot)")
    args = parser.parse_args()
    print(json.dumps(migrate(args.source, args.target, args.source_entropy_prefix)))
