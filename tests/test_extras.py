import json
import socket
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fakes import text, tool

import mcp_fake
from anvil import artifacts, connectors, mcp_oauth, plugins, schedules
from anvil.mcp import HttpTransport, MCPClient, StdioTransport
from anvil.search import search

FAKE = str(Path(mcp_fake.__file__))


@pytest.fixture(autouse=True)
def clean_state():
    def wipe():
        connectors.manager().close_all()
        for f in (connectors.CONNECTORS_FILE, connectors.SECRETS_FILE, schedules.SCHEDULES_FILE, plugins.STATE_FILE):
            if f.exists():
                f.unlink()
        import shutil
        for d in (artifacts.ARTIFACTS_DIR, plugins.PLUGINS_DIR):
            shutil.rmtree(d, ignore_errors=True)
    wipe()
    yield
    wipe()


# --- cron & schedules ---------------------------------------------------------------

@pytest.mark.parametrize("expr,after,expected", [
    ("0 9 * * *", "2026-11-22 08:30", "2026-11-22 09:00"),
    ("0 9 * * *", "2026-11-22 09:00", "2026-11-23 09:00"),
    ("0 9 * * 1-5", "2026-11-21 10:00", "2026-11-23 09:00"),   # Saturday -> Monday
    ("*/15 * * * *", "2026-11-22 10:07", "2026-11-22 10:15"),
    ("30 2 1 * *", "2026-11-22 00:00", "2026-12-01 02:30"),
    ("0 0 * * 7", "2026-11-22 01:00", "2026-11-29 00:00"),     # 7 = Sunday
    ("0 12 * * mon,fri", "2026-11-24 13:00", "2026-11-27 12:00"),
    ("@weekly", "2026-11-22 01:00", "2026-11-29 00:00"),
])
def test_cron_next(expr, after, expected):
    fmt = "%Y-%m-%d %H:%M"
    assert schedules.Cron(expr).next_after(datetime.strptime(after, fmt)).strftime(fmt) == expected


@pytest.mark.parametrize("bad", ["", "* * *", "61 * * * *", "* * * 13 *", "*/0 * * * *"])
def test_cron_rejects_bad(bad):
    with pytest.raises(ValueError):
        schedules.Cron(bad)


def test_schedule_fires_once_and_moves_forward(project):
    s = schedules.save({"prompt": "run tests", "root": str(project), "cron": "*/5 * * * *"})
    assert s["mode"] == "edits" and s["next_run"]
    ran = []
    sch = schedules.Scheduler(lambda x: ran.append(x["id"]) or "sess-1")
    due = datetime.fromisoformat(s["next_run"])
    assert sch.tick(due) == [s["id"]]
    assert sch.tick(due) == []  # already moved forward
    after = schedules.get(s["id"])
    assert after["last_session"] == "sess-1" and datetime.fromisoformat(after["next_run"]) > due
    assert ran == [s["id"]]


def test_schedule_validates_folder_and_prompt(tmp_path):
    with pytest.raises(ValueError):
        schedules.save({"prompt": "", "root": str(tmp_path)})
    with pytest.raises(ValueError):
        schedules.save({"prompt": "x", "root": str(tmp_path / "missing")})


def test_scheduler_lock_blocks_second_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(schedules, "LOCK_FILE", tmp_path / "scheduler.lock")
    assert schedules._take_lock("a")
    assert not schedules._take_lock("b")
    schedules._release_lock("a")
    assert schedules._take_lock("b")
    schedules._release_lock("b")


# --- MCP client -----------------------------------------------------------------------

def test_stdio_client_lists_and_calls_tools():
    c = MCPClient(StdioTransport(sys.executable, [FAKE]))
    try:
        c.connect()
        assert [t["name"] for t in c.list_tools()] == ["echo", "send_message"]
        assert c.call_tool("echo", {"text": "hi"})["content"][0]["text"] == "echo: hi"
    finally:
        c.close()


@pytest.mark.parametrize("sse", [False, True])
def test_http_client_json_and_sse(sse):
    srv = mcp_fake.HttpServer(sse=sse)
    c = MCPClient(HttpTransport(srv.url + "/mcp"))
    try:
        c.connect()
        assert c.transport.session_id == "s1"
        assert c.call_tool("echo", {"text": "yo"})["content"][0]["text"] == "echo: yo"
    finally:
        c.close()
        srv.stop()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_oauth_flow_discovers_registers_and_gets_token():
    srv = mcp_fake.HttpServer(oauth=True)
    try:
        port = _free_port()
        flow = mcp_oauth.Flow(srv.url + "/mcp", port=port)
        url = flow.start()
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        assert url.startswith(srv.url + "/authorize") and q["code_challenge_method"] == "S256"
        assert q["client_id"] == "client-1" and q["resource"] == srv.url + "/mcp"
        assert srv.registered[0]["redirect_uris"] == [f"http://localhost:{port}/callback"]
        srv.codes["abc"] = True
        urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?code=abc&state={q['state']}").read()
        bundle = flow.wait(10)
        assert bundle["access_token"] == "tok-abc" and bundle["refresh_token"] == "r1"
    finally:
        srv.stop()


def test_oauth_callback_rejects_wrong_state():
    srv = mcp_fake.HttpServer(oauth=True)
    try:
        port = _free_port()
        flow = mcp_oauth.Flow(srv.url + "/mcp", port=port)
        flow.start()
        urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?code=abc&state=evil").read()
        with pytest.raises(mcp_oauth.OAuthError):
            flow.wait(5)
    finally:
        srv.stop()


# --- connectors in the agent ------------------------------------------------------------

def _add_fake_stdio():
    return connectors.save_connector({"preset": "custom-stdio", "name": "Fake", "command": f'"{sys.executable}" "{FAKE}"'})


def test_secrets_are_kept_out_of_connectors_json():
    connectors.save_connector({"preset": "github", "values": {"token": "ghp_secret"}})
    assert "ghp_secret" not in connectors.CONNECTORS_FILE.read_text()
    assert "ghp_secret" in connectors.SECRETS_FILE.read_text()
    view = connectors.public_view(connectors.get("github"))
    assert view["token_set"] and "ghp_secret" not in json.dumps(view)


def test_preset_requires_fields():
    with pytest.raises(ValueError):
        connectors.save_connector({"preset": "google-workspace", "values": {}})
    c = connectors.save_connector({"preset": "google-workspace", "values": {
        "GOOGLE_OAUTH_CLIENT_ID": "id", "GOOGLE_OAUTH_CLIENT_SECRET": "sec"}})
    assert c["args"] == ["workspace-mcp", "--tools", "gmail", "drive", "calendar"]
    assert c["env"] == {"GOOGLE_OAUTH_CLIENT_ID": "id"}


def test_agent_uses_connector_tools(anthropic, make_agent):
    c = _add_fake_stdio()
    fake = anthropic()
    echo, send = connectors.tool_name(c["id"], "echo"), connectors.tool_name(c["id"], "send_message")
    fake.script = [tool(echo, text="hello"), tool(send, to="bob"), text("done")]
    a = make_agent(fake, answers=["no"])
    a.send("go")
    names = [t["name"] for t in fake.requests[0]["body"]["tools"]]
    assert echo in names and send in names and "save_artifact" in names
    first = fake.requests[1]["body"]["messages"][-1]["content"][0]
    assert first["content"] == "echo: hello" and not first.get("is_error")
    assert [x[0] for x in a.ui.asked] == [send]  # read-only tool ran without asking
    second = fake.requests[2]["body"]["messages"][-1]["content"][0]
    assert second["is_error"] and "declined" in second["content"]


def test_auto_mode_runs_connector_tools(anthropic, make_agent):
    c = _add_fake_stdio()
    fake = anthropic()
    fake.script = [tool(connectors.tool_name(c["id"], "send_message"), to="x"), text("ok")]
    a = make_agent(fake)
    a.auto_approve_all = True
    a.send("go")
    assert a.ui.asked == []
    assert fake.requests[1]["body"]["messages"][-1]["content"][0]["content"] == "sent to x"


def test_broken_connector_does_not_break_the_agent(anthropic, make_agent):
    connectors.save_connector({"preset": "custom-stdio", "name": "Broken", "command": "definitely-not-a-command-xyz"})
    fake = anthropic()
    fake.script = [text("hi")]
    a = make_agent(fake)
    a.send("go")
    assert connectors.manager().status["broken"]["state"] == "error"


# --- artifacts & search ---------------------------------------------------------------

def test_save_artifact_tool_and_update(anthropic, make_agent):
    fake = anthropic()
    fake.script = [tool("save_artifact", title="Report", kind="markdown", content="# Sales up"), text("saved")]
    a = make_agent(fake)
    a.session_id = "s-1"
    a.send("make a report")
    [art] = artifacts.list_all()
    assert art["title"] == "Report" and art["session_id"] == "s-1" and a.ui.asked == []
    again = artifacts.save("Report v2", "markdown", "# Sales way up", artifact_id=art["id"])
    assert again["version"] == 2 and again["created_at"] == art["created_at"]
    assert search("sales way")[0]["id"] == art["id"]


def test_artifact_rejects_bad_ids():
    with pytest.raises(ValueError):
        artifacts.get("../../etc/passwd") or artifacts._path("../x")


def test_search_finds_sessions(project):
    from anvil.config import SESSIONS_DIR
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    (SESSIONS_DIR / "20260101-000000-abcd.json").write_text(json.dumps({
        "title": "Fix login", "root": str(project), "saved_at": "2026-01-01T00:00:00",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "the OAuth redirect is broken"}]}]}))
    try:
        r = search("oauth redirect")
        assert r and r[0]["type"] == "session" and "redirect" in r[0]["snippet"]
        assert search("nothing-matches-this") == []
    finally:
        (SESSIONS_DIR / "20260101-000000-abcd.json").unlink()


# --- plugins ---------------------------------------------------------------------------

def _make_plugin(tmp_path, claude_style=False):
    p = tmp_path / "myplug"
    (p / "skills" / "deploy").mkdir(parents=True)
    (p / "skills" / "deploy" / "SKILL.md").write_text("---\nname: deploy\ndescription: Ship it\n---\nDo the deploy.")
    (p / "commands").mkdir()
    (p / "commands" / "standup.md").write_text("---\ndescription: Write a standup\n---\nSummarize git log.")
    manifest = p / (".claude-plugin/plugin.json" if claude_style else "plugin.json")
    manifest.parent.mkdir(exist_ok=True)
    manifest.write_text(json.dumps({"name": "myplug", "version": "1.0.0", "description": "Test plugin"}))
    (p / ".mcp.json").write_text(json.dumps({"mcpServers": {"fake": {"command": sys.executable, "args": [FAKE],
                                                                      "env": {"ROOT": "${CLAUDE_PLUGIN_ROOT}"}}}}))
    return p


@pytest.mark.parametrize("claude_style", [False, True])
def test_plugin_install_brings_skills_commands_and_connectors(tmp_path, project, claude_style):
    from anvil.skills import discover
    info = plugins.install(str(_make_plugin(tmp_path, claude_style)))
    assert info["skills"] == ["deploy"] and info["commands"] == ["standup"] and info["connectors"] == ["fake"]
    sk = discover(project)
    assert sk["deploy"].source == "plugin:myplug" and "standup" in sk
    [conn] = [c for c in connectors.all_connectors() if c["source"] == "plugin:myplug"]
    assert conn["env"]["ROOT"] == str(plugins.PLUGINS_DIR / "myplug")
    assert not conn["enabled"]  # plugin connectors start switched off
    connectors.set_enabled(conn["id"], True)
    assert connectors.manager().connect(conn["id"]) is not None
    plugins.set_enabled("myplug", False)
    assert "deploy" not in discover(project)
    assert not [c for c in connectors.all_connectors() if c["source"] == "plugin:myplug"]
    plugins.remove("myplug")
    assert plugins.installed() == []


def test_marketplace_repo_needs_a_plugin_name(tmp_path):
    repo = tmp_path / "market"
    (repo / ".claude-plugin").mkdir(parents=True)
    _make_plugin(repo / "plugins")
    (repo / "plugins" / "myplug").rename(repo / "plugins" / "one")
    (repo / ".claude-plugin" / "marketplace.json").write_text(json.dumps({"plugins": [{"name": "one", "source": "./plugins/one"}]}))
    with pytest.raises(RuntimeError, match="choose one"):
        plugins.install(str(repo))
    assert plugins.install(str(repo), "one")["name"] == "myplug"
