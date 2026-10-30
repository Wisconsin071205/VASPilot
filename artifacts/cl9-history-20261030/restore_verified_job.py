"""Restore only the locally submitted job corroborated by the saved live logs."""
import json
from datetime import datetime
from pathlib import Path
from rsci.core.config import Config
from rsci.hpc.center import RemoteCenter

root = Path(__file__).resolve().parent
config = Config()
entry = json.loads(config.pending_submits_path.read_text(encoding='utf-8'))['ps-8sDkJFgL-B0h']
result = entry['result']
assert entry['status'] == 'approved' and entry['server'] == 'cl9'
assert result['job_id'] == '129371' and result['raw'] == '129371.admin'
logs = json.loads((root / 'run-logs.json').read_text(encoding='utf-8'))
receipt = logs['NaOH_opt.pbsout']['content']
fields = dict(line.strip().split(':', 1) for line in receipt.splitlines() if ':' in line)
fields = {key.strip(): value.strip() for key, value in fields.items()}
assert fields['jobid'] == result['raw']
assert fields['workdir'] == entry['directory'] == result['directory']
assert fields['vasp_exit_code'] == '0'
submitted = datetime.fromisoformat(entry['submission_started_at']).timestamp()
assert 0 <= int(fields['start_epoch']) - submitted < 60
center = RemoteCenter(config, None)
target = center.path('cl9')
backup = root / 'remote-center-before-restore.json'
if target.exists() and not backup.exists():
    backup.write_bytes(target.read_bytes())
reconciled = {**result, 'server_identity': center.identity('cl9'),
              'submitted_at': entry['submission_started_at'],
              'script': entry['script'], 'submission_id': entry['id']}
center.record_submission('cl9', reconciled)
row = center.known('cl9', '129371')
assert row['directory'] == entry['directory']
print(json.dumps({'restored': True, 'job_id': row['job_id'], 'state': row['state'],
                  'directory': row['directory'], 'backup': str(backup)}, ensure_ascii=False))
