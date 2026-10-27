"""Extract the release, verify hashes, and smoke-test exe and installed wheel."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
import zipfile

if __package__:
    from .release_common import archive_name, release_version
else:
    from release_common import archive_name, release_version

ROOT = Path(__file__).resolve().parents[1]


def local_analysis_smoke(base_url, token, home, ui_pid, stop_ui):
    """Execute actual Python through the packaged detached supervisor.

    All inputs are synthetic. Stop the actual UI server while Python sleeps,
    then read the disk-backed result from a freshly constructed application.
    """
    import base64
    from rsci.cli.main import App
    from rsci.core.config import Config
    def call(action, payload):
        request = urllib.request.Request(base_url + '/api/' + action,
            data=json.dumps(payload).encode('utf-8'),
            headers={'X-RSCI-Token': token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.load(response)
        assert result.get('ok'), result
        return result
    name = 'packaged-csv'
    call('project.create', {'name': name})
    call('project.import', {'name': name, 'file': 'data/sample.csv',
         'content_base64': base64.b64encode(b'x,y\n1,2\n3,4\n').decode()})
    template = call('project.csv_template', {'name': name, 'file': 'data/sample.csv'})
    script = call('project.read', {'name': name, 'file': 'scripts/analyze.py'})
    call('project.write', {'name': name, 'file': 'scripts/analyze.py',
         'expected_sha256': script['sha256'],
         'content': 'import time\ntime.sleep(4)\n' + script['content']})
    spec = dict(template['spec'], python=sys.executable)
    plan = call('workflow.prepare', {'spec': spec})
    plan_id = plan['plan_id']
    approved = call('workflow.approve', {'plan_id': plan_id, 'phrase': 'approve ' + plan_id})
    call('workflow.run', {'plan_id': plan_id, 'approval_ref': approved['approval_ref']})
    engine = App(Config(home)).engine()
    for _ in range(120):
        state = engine._load_run(plan_id)
        if (state or {}).get('execution_state') == 'RUNNING':
            assert state['task']['worker']['pid'] != ui_pid
            stop_ui()
            break
        if (state or {}).get('task', {}).get('phase') in {'finished', 'blocked'}:
            raise AssertionError('Test must stop the UI during execution: ' + str(state))
        time.sleep(.1)
    else:
        raise TimeoutError('Packaged local Python did not start')
    reopened = App(Config(home)).engine()
    for _ in range(200):
        state = reopened._load_run(plan_id)
        if (state or {}).get('task', {}).get('phase') == 'finished':
            assert state['status'] == 'completed', state
            assert state['collection_status'] == 'completed'
            assert state['scientific_converged'] is None
            assert len([e for e in state['events'] if e['event'] == 'python.execution_intent']) == 1
            result = json.loads((Path(state['task']['download_root']) / 'summary.json').read_text())
            assert result['rows'] == 2 and result['columns']['x']['mean'] == 2
            assert len(state['task']['artifacts']) == 3
            return plan_id
        if (state or {}).get('task', {}).get('phase') == 'blocked':
            raise AssertionError(state)
        time.sleep(.1)
    raise TimeoutError('Detached local Python did not finish after UI exit')


def background_smoke(base_url, token, home, ui_pid):
    """Exercise the shipped worker without contacting any SSH/HPC service.

    This fixture is deliberately parse-only. Full scheduler and parent-exit
    behavior is covered by the disk-backed process integration tests.
    """
    from rsci.cli.main import App
    from rsci.core.config import Config, ServerEntry, _atomic_write_json
    from rsci.core.hashing import obj_sha256
    app = App(Config(home))
    app.config.upsert_server(ServerEntry(name='worker-smoke', target='nobody@invalid',
                                         port=22, remote_root='/isolated/worker-smoke',
                                         scheduler='slurm'))
    body = {'schema': 'rsci.plan/1', 'server': 'worker-smoke',
            'remote_dir': '/isolated/worker-smoke', 'files': [],
            'job_script': {}, 'risk_summary': {},
            'steps': [{'id': 'parse', 'type': 'parse',
                       'sources': ['INCAR', 'OSZICAR', 'OUTCAR']}]}
    digest = obj_sha256(body)
    plan_id = digest[:16]
    plan = dict(body, plan_id=plan_id, plan_hash=digest, job_script_content='')
    _atomic_write_json(app.engine().plan_path(plan_id), plan)
    outputs = app.config.home / 'downloads' / plan_id
    outputs.mkdir(parents=True)
    (outputs / 'INCAR').write_text('NSW=0\nNELM=60\n', encoding='utf-8')
    (outputs / 'OSZICAR').write_text('   1 F= -.10000000E+02  E0= -.10000000E+02\n', encoding='utf-8')
    (outputs / 'OUTCAR').write_text('reached required accuracy\n', encoding='utf-8')
    def call(action, payload):
        request = urllib.request.Request(base_url + '/api/' + action,
            data=json.dumps(payload).encode('utf-8'),
            headers={'X-RSCI-Token': token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=10) as response:
            result = json.load(response)
        assert result.get('ok'), result.get('error')
        return result
    approved = call('workflow.approve', {'plan_id': plan_id, 'phrase': 'approve ' + plan_id})
    call('workflow.run', {'plan_id': plan_id, 'approval_ref': approved['approval_ref']})
    for _ in range(120):
        state = app.engine()._load_run(plan_id)
        if (state or {}).get('task', {}).get('phase') == 'finished':
            assert state['task']['worker']['pid'] != ui_pid
            assert state['collection_status'] == 'completed'
            assert 'job_id' not in state  # no remote scheduler was contacted
            return
        if (state or {}).get('task', {}).get('phase') == 'blocked':
            raise AssertionError(state.get('failure'))
        time.sleep(0.25)
    raise TimeoutError('Packaged background worker did not finish')


def smoke(command, cwd, home, *, expected_version):
    env = dict(os.environ, RSCI_HOME=str(home), PYTHONIOENCODING='utf-8')
    env.pop('PYTHONPATH', None)
    subprocess.run([*command, '--help'], cwd=cwd, env=env, check=True, capture_output=True)
    log = home.with_suffix('.log')
    with log.open('w', encoding='utf-8') as stream:
        process = subprocess.Popen([*command, 'ui', '--no-open', '--port', '0'],
                                   cwd=cwd, env=env, stdout=stream, stderr=stream)
        stopped = False
        info = {}
        def stop_ui():
            nonlocal stopped
            if stopped:
                return
            stopped = True
            # Windows venv python.exe may be a launcher with a separate server
            # PID. The discovery file belongs to this isolated fixture only.
            server_pid = info.get('pid', process.pid)
            if server_pid != process.pid:
                try:
                    os.kill(server_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=15)
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
                assert state['ok'] and state['version'] == expected_version and state['servers'] == []
            background_smoke(url, info['token'], home, process.pid)
            plan_id = local_analysis_smoke(url, info['token'], home, info['pid'], stop_ui)
            status = subprocess.run([*command, 'workflow', 'status', plan_id],
                cwd=cwd, env=env, check=True, capture_output=True, text=True, encoding='utf-8', timeout=20)
            assert json.loads(status.stdout)['status'] == 'completed'
            return 'PASS: CLI/UI/API, detached VASP parse, CSV/Python supervisor survives UI exit, results recovered, fresh CLI reads completion'
        finally:
            stop_ui()


def main(archive=None):
    out = ROOT / 'artifacts/competition_submission/03-program'
    version = release_version(ROOT)
    archive = Path(archive) if archive else out / archive_name(version)
    work = ROOT / 'build' / ('verify-' + uuid.uuid4().hex[:10])
    work.mkdir(parents=True)
    with zipfile.ZipFile(archive) as z:
        assert z.testzip() is None
        z.extractall(work)
    bundle = work / f'rsci-{version}'
    metadata = json.loads((bundle / 'release.json').read_text(encoding='utf-8'))
    assert metadata == {'name': 'rsci', 'version': version}
    assert release_version(bundle / 'source') == version
    manifest = json.loads((bundle / 'SHA256SUMS.json').read_text(encoding='utf-8'))
    for name, expected in manifest.items():
        assert hashlib.sha256((bundle / name).read_bytes()).hexdigest() == expected, name
    report = {'version': version, 'archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
              'files_verified': len(manifest), 'platform': sys.platform, 'python': sys.version}
    report['windows_exe'] = smoke([str(bundle / 'windows/rsci/rsci.exe')], work, work / 'exe-home',
                                expected_version=version)
    venv = work / 'venv'
    subprocess.run([sys.executable, '-m', 'venv', str(venv)], check=True)
    python = venv / 'Scripts/python.exe'
    subprocess.run([str(python), '-m', 'pip', 'install', '--no-index', '--find-links', str(bundle / 'python-package'),
                    str(next((bundle / 'python-package').glob('rsci-*.whl')))], check=True)
    report['installed_wheel'] = smoke([str(python), '-m', 'rsci'], work, work / 'wheel-home',
                                    expected_version=version)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    (out / 'verification.json').write_text(text, encoding='utf-8')
    print(text)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, help='Archive to verify (defaults to this source version)')
    main(parser.parse_args().archive)
