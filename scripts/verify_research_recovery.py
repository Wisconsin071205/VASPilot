"""Crash a packaged OCR worker after a real checkpoint, then race two recoveries."""
import argparse
import io
import json
import os
import subprocess
import time
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from rsci.core.config import Config
from rsci.core.hashing import file_sha256
from rsci.research import documents, tasks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-dir', required=True)
    parser.add_argument('--exe', required=True)
    args = parser.parse_args()
    config = Config(Path(args.config_dir).resolve())
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    content = ['BT /F1 13 Tf 40 760 Td (Two-column fixture) Tj ET']
    for x, label in ((40, 'Left'), (310, 'Right')):
        for row in range(6):
            content.append(f'BT /F1 12 Tf {x} {710-row*26} Td ({label} column: measurement {row+1}.) Tj ET')
    for row in range(5):
        content.append(f'40 {400-row*32} m 280 {400-row*32} l S')
    for x in (40, 160, 280):
        content.append(f'{x} 400 m {x} 272 l S')
    for row, (x, y) in enumerate((('x','y'),('1','3'),('2','5'),('3','7'))):
        for pos, value in ((60,x),(180,y)):
            content.append(f'BT /F1 12 Tf {pos} {379-row*32} Td ({value}) Tj ET')
    stream.set_data('\n'.join(content).encode())
    page[NameObject('/Contents')] = writer._add_object(stream)
    fixtures = config.home.parent / 'fixtures'
    rotated = PdfReader(fixtures / 'native.pdf').pages[0]
    rotated.rotate(90)
    writer.add_page(rotated)
    writer.append(fixtures / 'scanned.pdf')
    writer.add_blank_page(width=595, height=842)
    payload = io.BytesIO(); writer.write(payload)
    doc = documents.import_stream(config, 'acceptance', io.BytesIO(payload.getvalue()), len(payload.getvalue()), 'Layout and crash recovery')
    task = tasks.enqueue(config, 'acceptance', 'parse', {'document_id':doc['document_id']}, start=False)
    command = [args.exe, '--config-dir', str(config.home), 'research-task', '_worker', task['task_id']]
    env = dict(os.environ, PATH=str(Path(os.environ['SystemRoot']) / 'System32'))
    options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    children = [subprocess.Popen(command, **options)]
    try:
        deadline = time.monotonic()+300
        checkpoint = None
        while time.monotonic()<deadline:
            row = tasks.get(config, task['task_id'])
            if row.get('revision'):
                checkpoint = config.home / f"research-cache/extraction/{doc['document_id']}/{row['revision']}/1.json"
                if checkpoint.exists():break
            if children[0].poll() is not None:raise RuntimeError(str(row))
            time.sleep(.1)
        assert checkpoint and checkpoint.is_file(), 'No durable first-page checkpoint'
        original_hash = file_sha256(checkpoint)
        children[0].terminate(); children[0].wait(timeout=15)
        children.extend(subprocess.Popen(command, **options) for _ in range(2))
        for child in children[1:]:child.wait(timeout=300)
        row = tasks.get(config, task['task_id'])
        assert row['status']=='completed', row
        result = documents.evidence(config, 'acceptance', doc['document_id'])
        assert file_sha256(checkpoint)==original_hash
        assert [p['page'] for p in result['pages']]==[1,2,3,4]
        assert all(p['blocks'] for p in result['pages'][:3])
        assert result['pages'][3]['warnings'], 'Blank page needs explicit review notice'
        assert any('table' in b['kind'].lower() for b in result['pages'][0]['blocks'])
        assert all(0<=v<=1 for p in result['pages'] for b in p['blocks'] for v in b['bbox'])
        proof = {'worker_killed_after_checkpoint':True,'concurrent_recovery_workers':2,'checkpoint_preserved':True,
                 'two_column_table_rotated_scanned_blank':True,'task_id':task['task_id'],'document_id':doc['document_id']}
        (config.home.parent / 'research-recovery.json').write_text(json.dumps(proof,indent=2),encoding='utf-8')
        print(json.dumps(proof),flush=True)
    finally:
        for child in children:
            if child.poll() is None:child.terminate();child.wait(timeout=15)


if __name__=='__main__':main()
