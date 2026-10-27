"""Real PDF/OCR, detached UI lifecycle, optional live model and analysis acceptance."""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from rsci.cli.main import App
from rsci.core.config import Config
from rsci.research import documents, tasks, ocr_runtime, literature, analysis, reports
from rsci.workflow.research import project_store


def wait(config, task_id, timeout=900):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = tasks.get(config, task_id)
        if row['status'] in {'completed', 'failed', 'needs_review', 'cancelled'}:
            if row['status'] != 'completed':
                raise RuntimeError(json.dumps(row, ensure_ascii=False))
            return row
        time.sleep(.5)
    raise TimeoutError(task_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', required=True)
    parser.add_argument('--model-home', help='Explicit existing model configuration; credentials stay encrypted locally')
    parser.add_argument('--exe', help='Packaged RSCI executable instead of source CLI')
    parser.add_argument('--restricted-path', action='store_true', help='Windows EXE with only system directories on PATH')
    args = parser.parse_args()
    config = Config(Path(args.config_dir).resolve())
    runtime = ocr_runtime.status(config)
    assert runtime['ready'], runtime
    store = project_store(config)
    name = 'acceptance'
    if not config.projects_index_path.exists():
        store.create(name, kind='general')
    fixtures = config.home.parent / 'fixtures'
    subprocess.run([runtime['executable'], str(Path(__file__).with_name('create_research_fixtures.py').resolve()), str(fixtures)], check=True)
    docs = []
    for filename in ('native.pdf','mixed.pdf','scanned.pdf'):
        source = fixtures / filename
        with source.open('rb') as handle:
            docs.append(documents.import_stream(config, name, handle, source.stat().st_size, filename))
    prefix = [args.exe] if args.exe else [sys.executable, '-m', 'rsci']
    child_env = dict(os.environ)
    if args.restricted_path:
        assert args.exe and os.name == 'nt'
        child_env['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
        child_env.pop('PYTHONPATH', None)
        child_env.pop('PYTHONHOME', None)
    log = (config.home.parent / 'acceptance-ui.log').open('wb')
    def start_ui():
        discovery = config.home / 'ui.json'
        discovery.unlink(missing_ok=True)
        child = subprocess.Popen(prefix + ['--config-dir',str(config.home),'ui','--port','0','--no-open'],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=child_env,
            **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
        for _ in range(200):
            if discovery.is_file():
                return child, json.loads(discovery.read_text(encoding='utf-8'))
            if child.poll() is not None:
                raise RuntimeError('UI failed to start')
            time.sleep(.1)
        raise TimeoutError('UI startup')
    def call(info, action, body):
        req=urllib.request.Request(info['url']+'/api/'+action, data=json.dumps(body).encode(),headers={'X-RSCI-Token':info['token'],'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=60) as response:
            result=json.load(response)
        assert result['ok'], result
        return result
    child, info = start_ui()
    queued=[]
    try:
        for doc in docs:
            queued.append(call(info, 'document.parse', {'project':name,'document_id':doc['document_id']})['task_id'])
        child.terminate();child.wait(timeout=15)
        results=[wait(config,t) for t in queued]
        assert all(r['status']=='completed' for r in results)
        for doc in docs:
            result=documents.evidence(config,name,doc['document_id'])
            text=' '.join(b['text'] for p in result['pages'] for b in p['blocks'])
            assert '2' in text and ('slope' in text.lower() or '斜率' in text), text
        child, info=start_ui()
        assert all(call(info,'research_task.get',{'task_id':t})['status']=='completed' for t in queued)
        print('PASS real native/mixed/scanned PDF; UI closed during OCR; restart retained results',flush=True)
        notes=[]
        if args.model_home:
            source=Config(args.model_home)
            provider_id=source.default_provider()
            entry=next(p for p in source.load_providers() if p.id==provider_id)
            config.add_provider(entry);config.set_default_provider(provider_id)
            key=source.get_provider_key(provider_id)
            if key:
                config.set_provider_key(provider_id,key)
            for kind in ('ask','compare','glossary'):
                task=call(info,'literature.'+kind,{'project':name,'document_ids':[d['document_id'] for d in docs], 'question':'这些研究采用什么方法，斜率是多少，有何局限？'})
                notes.append(wait(config,task['task_id'])['result']['note_id'])
                print('PASS live evidence model: '+kind,flush=True)
        data=(fixtures/'data.csv').read_bytes()
        existing=store._file(name,'data/input.csv')
        if not existing.exists():store.import_file(name,'data/input.csv',data)
        profile=call(info,'research.profile',{'project':name,'file':'data/input.csv'})
        wait(config,profile['task_id'])
        config.update_settings(research_python=runtime['executable'])
        template=call(info,'research.templates',{'project':name,'file':'data/input.csv','template':'regression','columns':['x','y'],'missing':'error'})
        engine=App(config).engine()
        prepared=call(info,'workflow.prepare',{'spec':template['spec']})
        plan_id=prepared['plan_id']
        if not engine._load_run(plan_id):
            approved=call(info,'workflow.approve',{'plan_id':plan_id,'phrase':'approve '+plan_id})
            call(info,'workflow.run',{'plan_id':plan_id,'approval_ref':approved['approval_ref']})
        child.terminate();child.wait(timeout=15)
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            result=engine.status(plan_id)
            if result.get('execution_state') in {'COMPLETED','FAILED'}:break
            time.sleep(.5)
        assert result['execution_state']=='COMPLETED',result
        child, info=start_ui()
        report=call(info,'report.create',{'project':name,'question':'Synthetic OCR and numerical acceptance', 'note_ids':notes,'profile_task_ids':[profile['task_id']],'plan_ids':[plan_id]})
        child.terminate();child.wait(timeout=15)
        report_id=wait(config,report['task_id'])['result']['report_id']
        assert reports.verify(reports.export_path(config,name,report_id,'zip'))['valid']
        summary={'documents':[d['document_id'] for d in docs],'parse_tasks':queued,'note_ids':notes,'plan_id':plan_id,'report_id':report_id,
                 'live_model':bool(args.model_home),'ui_exit_during_ocr':True,'bundle_verified':True,'packaged':bool(args.exe),'restricted_path':args.restricted_path}
        (config.home.parent/'research-acceptance.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(summary,ensure_ascii=False),flush=True)
    finally:
        if child.poll() is None:child.terminate();child.wait(timeout=15)
        log.close()


if __name__=='__main__':main()
