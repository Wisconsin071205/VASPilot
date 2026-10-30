import json
from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import ZipFile

import pytest

from scripts.release_common import release_version
from scripts.verify_submission import smoke
from tests.test_ui import ui  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "apps" / "huwei-agent-vscode-bridge"


@pytest.fixture()
def bridge_package():
    manifest = json.loads((BRIDGE / "package.json").read_text(encoding="utf-8"))
    package = BRIDGE / f"{manifest['name']}-{manifest['version']}.vsix"
    assert package.is_file(), "Build the current bridge package before publishing"
    return package, manifest


def test_bundled_vsix_matches_current_console_contract(bridge_package):
    package, manifest = bridge_package
    with ZipFile(package) as archive:
        packaged = json.loads(archive.read("extension/package.json"))
        api = archive.read("extension/out/src/api.js").decode("utf-8")
    assert packaged["version"] == manifest["version"]
    assert packaged["displayName"] == manifest["displayName"]
    assert '".rsci"' in api and '"X-RSCI-Token"' in api
    assert ".vaspilot" not in api and "X-Vaspilot-Token" not in api


def test_packaged_bridge_discovers_and_authenticates_against_console(bridge_package, ui, tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to execute the packaged bridge")
    package, _ = bridge_package
    compiled = tmp_path / "compiled"
    compiled.mkdir()
    with ZipFile(package) as archive:
        for name in archive.namelist():
            if name.startswith("extension/out/src/") and name.endswith(".js"):
                (compiled / Path(name).name).write_bytes(archive.read(name))
    discovery = tmp_path / ".rsci"
    discovery.mkdir()
    (discovery / "ui.json").write_text(json.dumps(
        {"url": ui["base"], "token": ui["token"], "pid": 1}), encoding="utf-8")
    script = """
const {ConsoleClient, readDiscoveryFile} = require(process.argv[1]);
const info = readDiscoveryFile(process.argv[2]);
new ConsoleClient(info.url, info.token, fetch).servers()
  .then(servers => console.log(JSON.stringify(servers)))
  .catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", script, str(compiled / "api.js"), str(tmp_path)],
                            capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    assert "cl9" in {server["name"] for server in json.loads(result.stdout)["servers"]}


def test_release_smoke_checks_actual_source_version(tmp_path):
    launcher = (f"import sys; sys.path.insert(0, {str(ROOT / 'src')!r}); "
                "from rsci.cli.main import main; raise SystemExit(main())")
    result = smoke([sys.executable, "-c", launcher], tmp_path, tmp_path / "isolated-home",
                   expected_version=release_version(ROOT))
    assert result.startswith("PASS:")
