"""Chat durability, admission and recovery without any paid model/HPC calls."""
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from rsci.agents.memory import ConversationStore
from rsci.core.locking import try_file_lock
from rsci.providers.base import ProviderReply
from tests.test_ui import ui, call, sse_chat  # noqa: F401


def test_concurrent_writes_archive_every_message(tmp_path):
    store = ConversationStore(tmp_path)
    sid = store.create_session()["session_id"]
    def append(index):
        ConversationStore(tmp_path).append(sid, "user", f"question-{index}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(100)))
    data = store.load(sid)
    rows = data["archived_messages"] + data["messages"]
    assert {row["content"] for row in rows} == {f"question-{i}" for i in range(100)}
    assert len(rows) == 100 and len(data["messages"]) <= 60
    assert "question-0" in store.export_markdown(sid)["content"]
    assert store.list_sessions(query="question-0")[0]["session_id"] == sid


def test_separate_processes_do_not_lose_history(tmp_path):
    sid = ConversationStore(tmp_path).create_session()["session_id"]
    program = """
import sys
from rsci.agents.memory import ConversationStore
store = ConversationStore(sys.argv[1])
for i in range(20):
    store.append(sys.argv[2], 'user', sys.argv[3] + str(i))
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    processes = [subprocess.Popen([sys.executable, "-c", program, str(tmp_path), sid, prefix],
                                 env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for prefix in ("first-", "second-")]
    try:
        for process in processes:
            _, error = process.communicate(timeout=20)
            assert process.returncode == 0, error.decode(errors="replace")
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert len(ConversationStore(tmp_path).load(sid)["messages"]) == 40


def test_interrupted_owner_is_visible_and_answer_slot_is_exact(tmp_path):
    store = ConversationStore(tmp_path)
    sid = store.create_session()["session_id"]
    with try_file_lock(store.execution_path(sid)) as owned:
        assert owned
        store.begin_turn(sid, "request-1", "question")
        store.append(sid, "user", "approval note arrived during reply")
        store.update_turn(sid, "request-1", answer="partial answer")
    data = store.snapshot(sid)
    assert data["turn"]["status"] == "interrupted"
    assert data["messages"][1]["content"] == "partial answer"
    assert data["messages"][2]["content"] == "approval note arrived during reply"
    with pytest.raises(Exception, match="已经接收"):
        store.begin_turn(sid, "request-1", "question")


def test_corrupt_history_is_preserved(tmp_path):
    path = tmp_path / "s-corrupt.json"
    original = '{"session_id":"s-corrupt","messages":"invalid"}'
    path.write_text(original, encoding="utf-8")
    store = ConversationStore(tmp_path)
    with pytest.raises(Exception, match="损坏"):
        store.begin_turn("s-corrupt", "request-1", "question")
    assert path.read_text(encoding="utf-8") == original


def test_new_turn_after_crash_preserves_interrupted_answer_in_archive(tmp_path):
    store = ConversationStore(tmp_path)
    sid = store.create_session()["session_id"]
    with try_file_lock(store.execution_path(sid)) as owned:
        assert owned
        store.begin_turn(sid, "first", "question")
        for i in range(65):
            store.append(sid, "user", f"concurrent note {i}")
        store.update_turn(sid, "first", answer="partial preserved in archive")
    # Simulate a caller that submits again after restart without loading first.
    with try_file_lock(store.execution_path(sid)) as owned:
        assert owned
        store.begin_turn(sid, "second", "new question")
    first = store.load(sid)["archived_messages"][1]
    assert first["content"] == "partial preserved in archive"
    assert first["status"] == "interrupted" and first["error"]


def test_completed_request_cannot_be_replayed(ui):
    sid = call(ui, "chat.new")["session_id"]
    ui["scripted"].script = [{"text": "saved once"}]
    payload = {"provider": "mock", "session_id": sid, "request_id": "once", "message": "question"}
    frames = sse_chat(ui, payload)
    assert len([frame for frame in frames if frame["type"] == "final"]) == 1
    saved = call(ui, "chat.status", {"session_id": sid, "request_id": "once"})
    assert saved["turn"]["status"] == "completed" and saved["answer"] == "saved once"
    with pytest.raises(urllib.error.HTTPError) as error:
        call(ui, "agent/chat", payload)
    assert error.value.code == 409
    assert json.load(error.value)["error"]["code"] == "chat_duplicate"
    assert len(ui["scripted"].calls) == 1


def test_setup_failure_releases_every_slot(ui, monkeypatch):
    sid = call(ui, "chat.new")["session_id"]
    original = ConversationStore.context_stats
    def broken(*args, **kwargs):
        raise OSError("disk read failed before SSE headers")
    monkeypatch.setattr(ConversationStore, "context_stats", broken)
    for _ in range(6):
        result = call(ui, "agent/chat", {"session_id": sid, "message": "question"})
        assert not result["ok"] and result["error"]["code"] != "chat_rate_limited"
    monkeypatch.setattr(ConversationStore, "context_stats", original)
    ui["scripted"].script = [{"text": "recovered"}]
    assert sse_chat(ui, {"provider": "mock", "session_id": sid, "message": "question"})[-1]["result"]["ok"]


def test_concurrent_session_and_destructive_actions_refused(ui, monkeypatch):
    sid = call(ui, "chat.new")["session_id"]
    entered, release = threading.Event(), threading.Event()
    def slow(messages, tools, *, stream_cb=None):
        entered.set()
        assert release.wait(10)
        return ProviderReply(text="one answer")
    monkeypatch.setattr(ui["scripted"], "chat", slow)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        first = pool.submit(sse_chat, ui, {"provider": "mock", "session_id": sid, "message": "first", "request_id": "first"})
        try:
            assert entered.wait(5)
            with pytest.raises(urllib.error.HTTPError) as error:
                call(ui, "agent/chat", {"session_id": sid, "message": "second", "request_id": "second"})
            assert error.value.code == 409
            for action in ("chat.clear", "chat.delete", "chat.compact", "chat.fork"):
                assert not call(ui, action, {"session_id": sid})["ok"]
            assert call(ui, "chat.load", {"session_id": sid})["turn"]["active"]
        finally:
            release.set()
        assert first.result(timeout=5)[-1]["result"]["ok"]
    assert len(call(ui, "chat.load", {"session_id": sid})["messages"]) == 2


def test_disconnect_keeps_original_answer_and_does_not_reexecute(ui, monkeypatch):
    sid = call(ui, "chat.new")["session_id"]
    release = threading.Event()
    calls = []
    def slow(messages, tools, *, stream_cb=None):
        calls.append(messages)
        assert release.wait(10)
        return ProviderReply(text="finished after disconnect")
    monkeypatch.setattr(ui["scripted"], "chat", slow)
    req = urllib.request.Request(ui["base"] + "/api/agent/chat",
        data=json.dumps({"provider": "mock", "session_id": sid, "request_id": "disconnected", "message": "question"}).encode(),
        headers={"X-RSCI-Token": ui["token"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            assert b'"meta"' in response.readline()
    finally:
        release.set()
    for _ in range(100):
        state = call(ui, "chat.status", {"session_id": sid, "request_id": "disconnected"})
        if state["turn"]["status"] == "completed":
            break
        time.sleep(0.02)
    assert state["answer"] == "finished after disconnect"
    assert len(calls) == 1


def test_failed_answer_keeps_partial_and_history(ui, monkeypatch):
    sid = call(ui, "chat.new")["session_id"]
    def partial(runtime, *args, **kwargs):
        runtime.stream_cb("already received")
        raise RuntimeError("test connection failure")
    monkeypatch.setattr("rsci.agents.runtime.AgentRuntime.run", partial)
    frames = sse_chat(ui, {"provider": "mock", "session_id": sid, "message": "question"})
    assert frames[-1]["result"]["ok"] is False
    loaded = call(ui, "chat.load", {"session_id": sid})
    assert loaded["turn"]["status"] == "failed"
    assert loaded["messages"][-1]["content"] == "already received"
    assert "connection failure" in loaded["messages"][-1]["error"]
    assert "already received" in call(ui, "chat.export", {"session_id": sid})["content"]


def test_stop_during_initial_provider_probe(ui, monkeypatch):
    entered = threading.Event()
    class Probing:
        def bind_cancel_event(self, event):
            self.cancel_event = event
        def probe(self):
            entered.set()
            assert self.cancel_event.wait(8)
            raise InterruptedError("probe cancelled")
        def cancel(self):
            self.cancel_event.set()
    provider = Probing()
    entry = ui["app"].config.load_providers()[0]
    monkeypatch.setattr("rsci.cli.agent.provider_by_id", lambda *args: (entry, provider))
    with concurrent.futures.ThreadPoolExecutor() as pool:
        response = pool.submit(sse_chat, ui, {"provider":"mock", "message":"question", "request_id":"probe-stop"})
        assert entered.wait(5)
        assert call(ui, "chat.stop", {"request_id":"probe-stop"})["stopped"]
        assert response.result(timeout=5)[-1]["result"]["cancelled"]


def test_browser_stream_decoder_and_session_races():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js needed for frontend behavior tests")
    path = Path(__file__).resolve().parents[1] / "src/rsci/ui/static/index.html"
    program = r"""
const fs=require('fs'), vm=require('vm'), assert=require('assert/strict');
const html=fs.readFileSync(process.argv[1], 'utf8');
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
const source=html.slice(html.indexOf('function chatEventDecoder('), html.indexOf('async function pollChatView('));
const ctx={TextDecoder}; vm.createContext(ctx); vm.runInContext(source,ctx);
const events=[]; const feed=ctx.chatEventDecoder(event=>events.push(event));
const stream=': heartbeat\r\n\r\ndata: {"type":"delta","text":"中文😀"}\r\n\r\ndata: {"type":"final",\r\ndata: "result":{"ok":true}}';
for (const byte of new TextEncoder().encode(stream)) feed(new Uint8Array([byte]));
feed(undefined,true);
assert.equal(events.length,2); assert.equal(events[0].text,'中文😀'); assert.equal(events[1].result.ok,true);
assert.throws(()=>ctx.chatEventDecoder(()=>{}) (new TextEncoder().encode('data: {bad}\n\n')));
const stored=new Map([['rsci.chat.draft.A',JSON.stringify({text:'keep my draft',files:[]})]]);
const draftElements={chatinput:{value:''},draftstatus:{textContent:''}};
const drafts={CURRENT_SESSION:'A',COMPOSER_SESSION:'',COMPOSER_READY:false,CHAT_ATTACHMENTS:[],CHAT_DRAFTS:new Map(),
  CHAT_FILE_LIMIT:5,document:{getElementById:id=>draftElements[id]},renderChatAttachments(){},
  localStorage:{getItem:key=>stored.get(key),setItem:(key,value)=>stored.set(key,value),removeItem:key=>stored.delete(key)}};
vm.createContext(drafts);
vm.runInContext(html.slice(html.indexOf('function draftKey('),html.indexOf('function setChatNotice(')),drafts);
drafts.saveChatDraft(); drafts.restoreChatDraft();
assert.equal(draftElements.chatinput.value,'keep my draft');
drafts.CURRENT_SESSION='B'; draftElements.chatinput.value='A edited'; drafts.saveChatDraft();
assert.equal(JSON.parse(stored.get('rsci.chat.draft.A')).text,'A edited');
assert.equal(stored.has('rsci.chat.draft.B'),false);
const rendered=[], waits={}, elements={chatlog:{dataset:{},innerHTML:''},sesshint:{}};
const race={CURRENT_SESSION:'',SESSION_OPEN_VERSION:0,SESSION_LOADING:false,CHAT_VIEW:null,
  CHAT_REQUEST_ID:'',localStorage:{setItem(){}},document:{getElementById:id=>elements[id]},
  saveChatDraft(){},detachChatView(){},restoreChatDraft(){},setChatNotice(){},closeSessionRename(){},
  renderContextUsage(){},refreshContextUsage:async()=>{},pollPendingCards(){},applyProjectContext(){},restoreWorkflowCards(){},
  api:(_,body)=>new Promise(resolve=>waits[body.session_id]=resolve),
  addMsg:(role,text)=>{rendered.push(text); return {querySelector:()=>({})};}};
vm.createContext(race);
vm.runInContext(html.slice(html.indexOf('async function openSession('),html.indexOf('async function newSession(')),race);
(async()=>{
  const a=race.openSession('A'), b=race.openSession('B');
  waits.B({ok:true,messages:[{role:'user',content:'B message'}]}); await b;
  waits.A({ok:true,messages:[{role:'user',content:'A message'}]}); await a;
  assert.equal(race.CURRENT_SESSION,'B'); assert.deepEqual(rendered,['B message']);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    result = subprocess.run([node, "-e", program, str(path)], capture_output=True,
                            text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0, result.stderr
