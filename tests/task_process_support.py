"""Disk-backed fake HPC shared by the UI, CLI, workers and test controller."""
from __future__ import annotations

import base64
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from rsci.core.config import _atomic_write_json
from rsci.core.locking import exclusive_file_lock
from rsci.core.errors import AuthRequiredError, RemoteError
from tests.conftest import FakeGatewayState, FakeTransport, ROOT


def encode(value):
    if isinstance(value, bytes):
        return {"bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, set):
        return {"set": sorted(value)}
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


def decode(value):
    if set(value) == {"bytes"}:
        return base64.b64decode(value["bytes"])
    if set(value) == {"set"}:
        return set(value["set"])
    return value


class DiskHpc:
    def __init__(self, path):
        self.path = Path(path)
        self.root = self.path.parent
        self.stage = self.root / "stage"
        self.stage.mkdir(exist_ok=True)

    def load(self):
        return json.loads(self.path.read_text(encoding="utf-8"), object_hook=decode)

    def save(self, data):
        # Encoding is performed before atomic replacement, under the HPC lock.
        _atomic_write_json(self.path, json.loads(json.dumps(data, default=encode)))

    def update(self, update):
        with exclusive_file_lock(self.path.with_suffix(".lock")):
            data = self.load()
            result = update(data)
            self.save(data)
            return result

    def gate(self, when, op):
        gate = self.root / f"gate-{when}-{op}"
        if gate.exists():
            (self.root / f"entered-{when}-{op}").touch()
            while gate.exists():
                time.sleep(0.05)

    def initialize(self):
        fake = FakeGatewayState(self.root)
        fake.add_server("cl9")
        self.save({"state": vars(fake), "calls": [], "freeze": True})


class DiskTransport:
    def __init__(self):
        self.hpc = DiskHpc(os.environ["RSCI_TASK_TEST_STATE"])

    def run_gateway(self, args, **kwargs):
        op = args[0]
        self.hpc.gate("before", op)
        if op == "download":
            source = FakeTransport._positionals(args)[0]
            self.hpc.gate("before", "download-" + Path(source).name)
        def run(data):
            if data.get("auth_required"):
                raise AuthRequiredError("test session expired")
            fake = FakeGatewayState(self.hpc.root)
            vars(fake).update(data["state"])
            transport = FakeTransport(fake)
            positionals = FakeTransport._positionals(args)
            if op == "upload":
                transport._stage = (self.hpc.stage / Path(positionals[0]).name).read_bytes()
            if op == "job-state" and data["freeze"]:
                known = positionals[0]
                found = next((j for j in fake.jobs["cl9"] if j["job_id"] == known), None)
                document = {"ok": True, "state": found["state"] if found else "UNKNOWN", "job_id": known}
            else:
                document = transport.run_gateway(args, **kwargs)
            if op == "submit":
                fake.workdirs = getattr(fake, "workdirs", {})
                fake.workdirs[document["job_id"]] = document["directory"]
                remote = document["directory"]
                fake.files["cl9"][f"{remote}/OSZICAR"] = b"   1 F= -.10000000E+02  E0= -.10000000E+02\n"
                fake.files["cl9"][f"{remote}/OUTCAR"] = b"reached required accuracy\n"
            if op == "download" and document.get("ok"):
                (self.hpc.stage / Path(positionals[1]).name).write_bytes(transport._stage)
            data["calls"].append(list(args))
            data["state"] = vars(fake)
            return document
        document = self.hpc.update(run)
        self.hpc.gate("after", op)
        if not document.get("ok"):
            error = document.get("error") or {}
            raise RemoteError(error.get("message", "fake gateway operation failed"), detail=error)
        return document

    def stage_path(self):
        return "/tmp/rsci-" + uuid.uuid4().hex

    def scp_to_stage(self, local, stage, **kwargs):
        shutil.copy2(local, self.hpc.stage / Path(stage).name)

    def scp_from_stage(self, stage, local, **kwargs):
        self.hpc.gate("before", "scp-download")
        shutil.copy2(self.hpc.stage / Path(stage).name, local)

    def rm_stage(self, stage):
        (self.hpc.stage / Path(stage).name).unlink(missing_ok=True)


def install():
    from rsci.cli.main import App
    def transport(app):
        if app._transport is None:
            app._transport = DiskTransport()
        return app._transport
    App.transport = transport
