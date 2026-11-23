"""Projects and threads: storage, the project chat starting threads, checklists, decisions, results."""

import json
import threading
import time

import pytest
from fakes import text, tool

from anvil import projects as P
from anvil.config import SESSIONS_DIR, Config
from anvil.gui.server import App, start_server
from test_config_and_server import call


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("timed out")


class RoleScript:
    """Replies for the project chat and for threads come from separate scripts, so concurrent
    agents get the right ones whatever order their requests arrive in."""

    def __init__(self, fake):
        self.chat, self.thread = [], []
        self.lock = threading.Lock()
        fake.next_reply = self.next

    def next(self, body):
        system = json.dumps(body.get("system", ""))
        q = self.thread if "You are working on one thread" in system else self.chat
        with self.lock:
            return q.pop(0) if q else text("ok")


@pytest.fixture
def app_server(anthropic, project):
    fake = anthropic()
    cfg = Config(provider="anthropic", base_url=fake.url, api_key="k", auto_compact_tokens=0,
                 cache_keepalive_minutes=0, browser_enabled=False)
    app = App(cfg, project)
    srv = start_server(app, 0)
    yield app, srv, fake
    srv.shutdown()


def test_project_storage_roundtrip(project):
    p = P.Project.create("Shop", str(project), "Use pytest.")
    assert P.find_project("shop").id == p.id
    p.remember("Deploys go through Caddy")
    p.remember("Prefers Hinglish")
    mem = p.read_memory()
    assert "- Deploys go through Caddy" in mem and mem.count("\n- ") == 1
    suffix = P.system_suffix(p, "thread", "Fix login")
    assert "Fix login" in suffix and "Use pytest." in suffix and "Caddy" in suffix
    # unknown fields from a newer version survive a save
    raw = json.loads(p.path.read_text())
    raw["synced_rev"] = 7
    p.path.write_text(json.dumps(raw))
    q = P.Project.load(p.id)
    q.save()
    assert json.loads(p.path.read_text())["synced_rev"] == 7
    with pytest.raises(ValueError):
        P.Project.create("x", str(project / "missing"))
    assert P.Project.load("../etc/passwd") is None
    p.delete()


def test_chat_starts_thread_that_reports_back(app_server, project):
    app, srv, fake = app_server
    rs = RoleScript(fake)
    rs.chat = [tool("start_thread", title="Add a README", task="Write README.md describing the project."),
               text("Started a thread for the README."),
               tool("list_threads"), text("All done.")]
    rs.thread = [tool("update_checklist", items=[{"text": "Write README", "state": "doing"}]),
                 tool("write_file", path="README.md", content="# Demo\n"),
                 tool("update_checklist", items=[{"text": "Write README", "state": "done"}]),
                 tool("set_status", status="done", summary="README.md written."),
                 text("README.md written.")]

    code, d = call(srv, app, "POST", "/api/projects", {"name": "Demo", "root": str(project), "instructions": "Be brief."})
    assert code == 200
    pid = d["project"]["id"]
    chat = call(srv, app, "POST", f"/api/projects/{pid}/chat", {})[1]
    cid = chat["meta"]["id"]
    assert chat["meta"]["kind"] == "chat"
    call(srv, app, "POST", f"/api/sessions/{cid}/mode", {"mode": "edits"})  # threads inherit it
    assert call(srv, app, "POST", f"/api/sessions/{cid}/send", {"text": "add a readme"})[0] == 200

    def thread_row():
        rows = call(srv, app, "GET", "/api/state")[1]["sessions"]
        return next((r for r in rows if r.get("project_id") == pid and r.get("kind") == "thread"
                     and r["status"] == "done"), None)
    row = wait_for(thread_row)
    assert row["title"] == "Add a README"
    assert row["checklist"] == [{"text": "Write README", "state": "done"}]
    assert row["summary"] == "README.md written."
    assert (project / "README.md").read_text() == "# Demo\n"

    # The thread's result reaches the coordinator with the user's next message, then is cleared.
    wait_for(lambda: app.sessions[cid].inbox and not app.sessions[cid].running)
    call(srv, app, "POST", f"/api/sessions/{cid}/send", {"text": "status?"})
    wait_for(lambda: not app.sessions[cid].running and len(app.sessions[cid].agent.messages) > 4)
    items = call(srv, app, "GET", f"/api/sessions/{cid}")[1]["items"]
    upd = [i for i in items if i["kind"] == "updates"]
    assert upd and "Add a README" in upd[0]["text"] and "README.md written." in upd[0]["text"]
    assert [i["text"] for i in items if i["kind"] == "user"] == ["add a readme", "status?"]
    assert not app.sessions[cid].inbox

    # Saved with its project fields, and reloaded as a thread after a restart.
    tid = row["id"]
    wait_for(lambda: (SESSIONS_DIR / f"{tid}.json").exists())
    saved = json.loads((SESSIONS_DIR / f"{tid}.json").read_text())
    assert saved["kind"] == "thread" and saved["parent"] == cid and saved["mode"] == "edits"
    app.sessions.pop(tid)
    again = app.get_session(tid)
    assert again.kind == "thread" and again.agent.hooks is not None and again.status == "done"


def test_decision_card_blocks_until_answered(app_server, project):
    app, srv, fake = app_server
    rs = RoleScript(fake)
    rs.thread = [tool("ask_user", question="Which database?", recommended=1,
                      options=[{"label": "SQLite", "detail": "simple"}, {"label": "Postgres"}]),
                 text("Going with the choice.")]
    pid = call(srv, app, "POST", "/api/projects", {"name": "DB", "root": str(project)})[1]["project"]["id"]
    tid = call(srv, app, "POST", f"/api/projects/{pid}/threads", {"task": "Set up the database"})[1]["meta"]["id"]

    pending = wait_for(lambda: call(srv, app, "GET", f"/api/sessions/{tid}")[1]["pending"])
    assert pending[0]["type"] == "decision" and pending[0]["recommended"] == 1
    assert [o["label"] for o in pending[0]["options"]] == ["SQLite", "Postgres"]
    assert app.sessions[tid].status == "needs_you"
    call(srv, app, "POST", f"/api/sessions/{tid}/approve", {"req_id": pending[0]["req_id"], "decision": "Postgres"})
    wait_for(lambda: app.sessions[tid].status == "done")
    items = call(srv, app, "GET", f"/api/sessions/{tid}")[1]["items"]
    ask = next(i for i in items if i["kind"] == "tool" and i["name"] == "ask_user")
    assert "Postgres" in ask["result"]


def test_steer_while_busy_is_queued(app_server, project):
    app, srv, fake = app_server
    rs = RoleScript(fake)
    gate = threading.Event()
    orig = rs.next

    def slow(body):
        if "You are working on one thread" in json.dumps(body.get("system", "")):
            gate.wait(5)
        return orig(body)
    fake.next_reply = slow
    rs.thread = [text("first"), text("second")]
    pid = call(srv, app, "POST", "/api/projects", {"name": "Q", "root": str(project)})[1]["project"]["id"]
    tid = call(srv, app, "POST", f"/api/projects/{pid}/threads", {"task": "do it"})[1]["meta"]["id"]
    wait_for(lambda: app.sessions[tid].running)
    assert call(srv, app, "POST", f"/api/sessions/{tid}/send", {"text": "also this", "queue": True})[0] == 200
    assert app.sessions[tid].queued == ["also this"]
    gate.set()
    wait_for(lambda: not app.sessions[tid].running and not app.sessions[tid].queued
             and len(app.sessions[tid].agent.messages) == 4)
    users = [i["text"] for i in call(srv, app, "GET", f"/api/sessions/{tid}")[1]["items"] if i["kind"] == "user"]
    assert users == ["do it", "also this"]


def test_update_and_delete_project(app_server, project):
    app, srv, fake = app_server
    RoleScript(fake).thread = [text("done")]
    pid = call(srv, app, "POST", "/api/projects", {"name": "Old", "root": str(project)})[1]["project"]["id"]
    d = call(srv, app, "POST", f"/api/projects/{pid}", {"name": "New", "instructions": "x", "memory": "- fact\n"})[1]
    assert d["project"]["name"] == "New" and d["project"]["memory"] == "- fact\n"
    tid = call(srv, app, "POST", f"/api/projects/{pid}/threads", {"task": "hi"})[1]["meta"]["id"]
    wait_for(lambda: (SESSIONS_DIR / f"{tid}.json").exists() and not app.sessions[tid].running)
    assert call(srv, app, "DELETE", f"/api/projects/{pid}")[0] == 200
    assert not (SESSIONS_DIR / f"{tid}.json").exists()
    assert P.Project.load(pid) is None
    assert all(p["id"] != pid for p in call(srv, app, "GET", "/api/state")[1]["projects"])


def test_terminal_project_chat_shares_the_desktop_session(anthropic, project, monkeypatch):
    from anvil import cli
    from conftest import RecordingUI
    from anvil.agent import Agent
    fake = anthropic()
    fake.script = [tool("remember", note="Tests use pytest"), text("Noted.")]
    p = P.Project.create("Term", str(project), "Reply briefly.")
    cfg = Config(provider="anthropic", base_url=fake.url, api_key="k", auto_compact_tokens=0,
                 cache_keepalive_minutes=0, browser_enabled=False)
    a = Agent(cfg, project, RecordingUI())
    a.session_id = p.chat_id
    assert not cli.load_session(a, p.chat_id)
    cli.attach_terminal_project(a, p, "chat")
    assert "Reply briefly." in a.system and "start_thread" not in [t["name"] for t in a._tool_defs()]
    cli.run_turn(a, "remember that tests use pytest")
    assert "Tests use pytest" in p.read_memory()
    cli.save_session(a)
    saved = json.loads((SESSIONS_DIR / f"{p.chat_id}.json").read_text())
    assert saved["project_id"] == p.id and saved["kind"] == "chat"
    b = Agent(cfg, project, RecordingUI())
    assert cli.load_session(b, p.chat_id) and b.hooks is not None and b.hooks.kind == "chat"
    assert "Tests use pytest" in b.system  # memory is part of the next session's prompt
    b.stop_keepalive(); a.stop_keepalive()
