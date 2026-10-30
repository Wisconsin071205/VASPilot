from pathlib import Path
import shutil
import subprocess

import pytest


def test_remote_center_incremental_refresh_and_escaping():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for browser logic tests")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / "tests/frontend/remote_center.cjs"),
                             str(root / "src/rsci/ui/static/remote-center.js")],
                            capture_output=True, text=True, encoding="utf-8",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
