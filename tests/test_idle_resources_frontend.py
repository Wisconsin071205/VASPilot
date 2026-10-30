"""Idle resources include CPU-only hosts and handle incomplete observations."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_idle_cpu_and_gpu_resources():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for browser logic tests")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, str(root / "tests/frontend/idle_resources.cjs"),
         str(root / "src/rsci/ui/static/index.html")],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stdout + result.stderr
