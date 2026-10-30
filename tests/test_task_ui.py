"""Task-center API contracts and frontend recovery affordances."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.test_ui import ui, call  # noqa: F401
from tests.conftest import ROOT


def test_ui_admission_has_persisted_status_and_never_exposes_receipt(ui, vasp_inputs, monkeypatch):
    # Leave an admitted task queued instead of running it in the unit fixture.
    monkeypatch.setattr("rsci.workflow.worker.launch", lambda engine, plan_id:
                        {"ok": True, "started": plan_id, "background": True})
    prepared = call(ui, "workflow.prepare", {"spec": {"from_dir": str(vasp_inputs),
                    "server": "cl9", "remote_dir": ROOT + "/runs/task-api"}})
    plan_id = prepared["plan_id"]
    approved = call(ui, "workflow.approve", {"plan_id": plan_id, "phrase": "approve " + plan_id})
    assert call(ui, "workflow.run", {"plan_id": plan_id, "approval_ref": approved["approval_ref"]})["ok"]
    status = call(ui, "workflow.status", {"plan_id": plan_id})
    assert status["task"]["phase"] == "queued"
    assert not status["task"]["worker_alive"]
    detail = call(ui, "workflow.get", {"plan_id": plan_id})
    assert "execution_receipt" not in detail["run"]
    assert approved["approval_ref"] not in json.dumps(detail)
    assert call(ui, "workflow.resume", {"plan_id": plan_id})["ok"]


def test_recovery_affordances_are_safe_and_script_parses():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to validate the embedded frontend")
    path = Path(__file__).resolve().parents[1] / "src/rsci/ui/static/index.html"
    program = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const html = fs.readFileSync(process.argv[1], 'utf8');
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
const source = html.slice(html.indexOf('function canResumeTask('), html.indexOf('function taskEvidence('));
const ctx = {}; vm.createContext(ctx); vm.runInContext(source, ctx);
const can = ctx.canResumeTask;
assert.equal(can({status:'running', task:{phase:'monitoring',worker_alive:true}}), false);
assert.equal(can({status:'running', task:{phase:'queued',worker_alive:false}}), true);
for (const reason of ['submit_uncertain','approval_required','invalid_receipt','inputs_changed','invalid_plan']) {
  assert.equal(can({status:'paused', task:{phase:'blocked',blocked_reason:reason}}), false);
}
assert.equal(can({status:'paused', task:{phase:'blocked',blocked_reason:'monitor_timeout'}}), true);
assert.equal(can({status:'needs_review', task:{phase:'finished'}}), false);
assert.ok(html.includes('关闭窗口不会取消远端任务'));
assert.ok(!html.includes('粘贴原 approval_ref'));
// A five-second refresh must preserve both fields even when a button has
// focus after validation failed. Run the actual renderer against a tiny DOM.
const planId = '0123456789abcdef';
const box = {contains:()=>false, querySelectorAll:()=>[
  {id:'wf-job-'+planId,value:'2003'}, {id:'wf-bind-'+planId,value:'wrong'}]};
const notice = {};
const renderer = html.slice(html.indexOf('async function refreshWorkflowCenter('),
                            html.indexOf('async function openWorkflowPlan('));
const renderCtx = {document:{activeElement:null,getElementById:id=>id==='workflowplans'?box:notice},
  api:async()=>({ok:true,plans:[{plan_id:planId,status:'needs_review',task:{phase:'blocked',blocked_reason:'submit_uncertain'}}]}),
  WORKFLOW_BUSY:new Set(), WORKFLOW_EVIDENCE:{}, esc:value=>String(value??''),
  taskReason:()=>'', taskPhase:()=>'', workflowCenterLabel:()=>'', workflowCenterClass:()=>'',
  badge:()=>'', displayTaskTime:()=>'', canResumeTask:()=>false, taskEvidence:()=>''};
vm.createContext(renderCtx); vm.runInContext(renderer, renderCtx);
renderCtx.refreshWorkflowCenter(true).then(()=>{
  assert.ok(box.innerHTML.includes('value="2003"'));
  assert.ok(box.innerHTML.includes('value="wrong"'));
}).catch(error=>{console.error(error);process.exitCode=1;});
console.log('PASS: inline script and safe recovery actions');
"""
    result = subprocess.run([node, "-e", program, str(path)], capture_output=True,
                            text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr


def test_ui_discovery_respects_explicit_config_dir(config_home, tmp_path):
    from rsci.cli.main import App
    from rsci.core.config import Config
    from rsci.ui.server import serve
    explicit = tmp_path / "explicit-ui"
    httpd, url = serve(App(Config(explicit)), host="127.0.0.1", port=0,
                       open_browser=False, run_forever=False)
    try:
        info = json.loads((explicit / "ui.json").read_text(encoding="utf-8"))
        assert info["url"] == url.rsplit("/t/", 1)[0]
        assert not (config_home / "ui.json").exists()
    finally:
        httpd.RequestHandlerClass.state.stopped.set()
        httpd.RequestHandlerClass.state.kick_campaigns()
        httpd.server_close()


def test_cli_keeps_synchronous_default_and_optional_resume_approval():
    from rsci.cli.main import build_parser
    parser = build_parser()
    run = parser.parse_args(["workflow", "run", "0" * 16, "--approval-ref", "example"])
    assert not run.background
    resume = parser.parse_args(["workflow", "resume", "0" * 16, "--background"])
    assert resume.background and resume.approval_ref == ""
