"""Extract the release, verify hashes, and smoke-test exe and installed wheel."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def smoke(command, cwd, home):
    env = dict(os.environ, RSCI_HOME=str(home), PYTHONIOENCODING='utf-8')
    env.pop('PYTHONPATH', None)
    subprocess.run([*command, '--help'], cwd=cwd, env=env, check=True, capture_output=True)
    log = home.with_suffix('.log')
    with log.open('w', encoding='utf-8') as stream:
        process = subprocess.Popen([*command, 'ui', '--no-open', '--port', '0'],
                                   cwd=cwd, env=env, stdout=stream, stderr=stream)
        try:
            for _ in range(120):
                if process.poll() is not None:
                    raise RuntimeError(log.read_text(encoding='utf-8'))
                discovery = home / 'ui.json'
                if discovery.is_file():
                    info = json.loads(discovery.read_text(encoding='utf-8'))
                    break
                time.sleep(0.25)
            else:
                raise TimeoutError('UI discovery timed out')
            url = info['url']
            with urllib.request.urlopen(url + '/healthz', timeout=10) as response:
                assert response.status == 200
            with urllib.request.urlopen(url + '/t/' + info['token'], timeout=10) as response:
                html = response.read()
                assert response.status == 200 and len(html) > 10000
            request = urllib.request.Request(url + '/api/state', data=b'{}',
                headers={'X-RSCI-Token': info['token'], 'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=10) as response:
                assert response.status == 200
                state = json.load(response)
                assert state['ok'] and state['version'] == '1.5.0' and state['servers'] == []
            return 'PASS: CLI help, HTTP health, full UI page, authenticated state API'
        finally:
            process.terminate()
            process.wait(timeout=15)


def main():
    out = ROOT / 'artifacts/competition_submission/03-program'
    archive = out / 'rsci_1.5.0_智能体可部署程序.zip'
    work = ROOT / 'build' / ('verify-' + uuid.uuid4().hex[:10])
    work.mkdir(parents=True)
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        z.extractall(work)
    bundle = work / 'rsci-1.5.0'
    manifest = json.loads((bundle / 'SHA256SUMS.json').read_text(encoding='utf-8'))
    for name, expected in manifest.items():
        assert hashlib.sha256((bundle / name).read_bytes()).hexdigest() == expected, name
    report = {'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
              'files_verified': len(manifest), 'platform': sys.platform, 'python': sys.version}
    report['windows_exe'] = smoke([str(bundle / 'windows/rsci/rsci.exe')], work, work / 'exe-home')
    venv = work / 'venv'
    subprocess.run([sys.executable, '-m', 'venv', str(venv)], check=True)
    python = venv / 'Scripts/python.exe'
    subprocess.run([str(python), '-m', 'pip', 'install', '--no-index', '--no-deps',
                    str(next((bundle / 'python-package').glob('*.whl')))], check=True)
    report['installed_wheel'] = smoke([str(python), '-m', 'rsci'], work, work / 'wheel-home')
    text = json.dumps(report, ensure_ascii=False, indent=2)
    (out / 'verification.json').write_text(text, encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
