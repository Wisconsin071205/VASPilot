"""Build the third competition deliverable from an explicit source allowlist."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from contextlib import nullcontext
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/competition_submission/03-program'


def run(*args, cwd=None):
    subprocess.run(args, cwd=cwd or ROOT, check=True)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    # Keep build intermediates under ignored build/ for inspection. On Windows,
    # tempfile's restrictive directory ACLs can prevent child build tools reading it.
    with nullcontext(ROOT / 'build' / ('submission-' + uuid.uuid4().hex[:10])) as temp:
        work = Path(temp)
        bundle = work / 'rsci-1.5.0'
        source = bundle / 'source'
        source.mkdir(parents=True)
        # Do not traverse the working tree's private configuration or artifacts.
        for name in ('src/rsci', 'gateway', 'recipes', 'scripts', 'tests', 'spec', 'docs'):
            shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns(
                '__pycache__', '*.pyc', '*.pem', '*.key', '.env*'))
        for name in ('README.md', 'pyproject.toml', '.gitignore', '.gitattributes'):
            shutil.copy2(ROOT / name, source / name)
        bridge = ROOT / 'apps/huwei-agent-vscode-bridge'
        shutil.copytree(bridge, source / 'apps/huwei-agent-vscode-bridge',
                        ignore=shutil.ignore_patterns('node_modules', 'out', '*.vsix'))
        # This helper lives outside the Python package in the development tree.
        shutil.copy2(ROOT / 'gateway/huwei-workspace-gateway/huwei_workspace_gateway.py',
                     source / 'src/rsci/gateway/huwei_workspace_gateway.py')
        wheels = bundle / 'python-package'
        run(sys.executable, '-c', 'import sys; from setuptools.build_meta import build_wheel; build_wheel(sys.argv[1])', str(wheels), cwd=source)
        entry = work / 'launcher.py'
        entry.write_text('from rsci.cli.main import main\nif __name__ == "__main__":\n    raise SystemExit(main())\n', encoding='utf-8')
        run(sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
            '--name', 'rsci', '--paths', str(source / 'src'),
            '--collect-all', 'rsci', '--add-data',
            str(source / 'src/rsci/gateway/rsci_gateway.py') + os.pathsep + 'rsci/gateway',
            '--add-data', str(source / 'src/rsci/gateway/huwei_workspace_gateway.py') + os.pathsep + 'rsci/gateway',
            '--distpath', str(bundle / 'windows'), '--workpath', str(work / 'pyinstaller'),
            '--specpath', str(work), str(entry), cwd=work)
        shutil.copy2(ROOT / 'docs/submission-deployment.md', bundle / '使用说明.md')
        (bundle / 'Start-rsci.cmd').write_text(
            '@echo off\ncd /d "%~dp0"\n"windows\\rsci\\rsci.exe" ui %*\n'
            'if errorlevel 1 pause\n', encoding='ascii')
        (bundle / 'Check-rsci.cmd').write_text(
            '@echo off\ncd /d "%~dp0"\n"windows\\rsci\\rsci.exe" --help\npause\n', encoding='ascii')
        def payload(p):
            relative = p.relative_to(bundle)
            return p.is_file() and not any(
                part in ('build', '__pycache__') or part.endswith('.egg-info')
                for part in relative.parts)
        manifest = {p.relative_to(bundle).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted(bundle.rglob('*')) if payload(p)}
        (bundle / 'SHA256SUMS.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
        archive = OUT / 'rsci_1.5.0_智能体可部署程序.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
            for p in sorted(bundle.rglob('*')):
                if payload(p):
                    z.write(p, p.relative_to(work))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        archive.with_suffix('.zip.sha256').write_text(digest + '  ' + archive.name + '\n', encoding='utf-8')
        print(f'Created: {archive} ({archive.stat().st_size} bytes)', flush=True)


if __name__ == '__main__':
    (ROOT / 'build').mkdir(exist_ok=True)
    main()
