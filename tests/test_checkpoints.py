import shutil
import subprocess

import pytest
from fakes import text, tool, tools

from anvil import checkpoints as cp

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git_init(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


@needs_git
def test_undo_restores_edits_and_bash_changes_and_rewinds_chat(anthropic, make_agent, project):
    _git_init(project)
    (project / ".gitignore").write_text("build/\n")
    (project / "a.txt").write_text("one\n")
    (project / "keep.txt").write_text("keep\n")
    fake = anthropic()
    fake.script = [
        tools(("edit_file", {"path": "a.txt", "old_string": "one", "new_string": "two"}),
              ("bash", {"command": "echo made > new.txt && rm keep.txt && mkdir -p build && echo x > build/out"})),
        text("done"),
    ]
    a = make_agent(fake)
    a.auto_approve_all = True
    a.session_id = "s1"
    a.send("change things")
    assert (project / "a.txt").read_text() == "two\n" and (project / "new.txt").exists()
    assert not (project / "keep.txt").exists()
    [c] = a.checkpoints.items()
    assert c["changed"] == ["a.txt", "keep.txt", "new.txt"]  # build/ is git-ignored
    assert set(a.checkpoints.preview()) == {"a.txt", "keep.txt", "new.txt"}

    res = a.checkpoints.undo()
    assert (project / "a.txt").read_text() == "one\n"
    assert (project / "keep.txt").read_text() == "keep\n"
    assert not (project / "new.txt").exists()
    assert (project / "build" / "out").exists()  # ignored files are left alone
    assert res["chat"] and a.messages == []
    assert a.checkpoints.items() == []
    # the project's own repository is untouched: no refs, no index, no stash
    out = subprocess.run(["git", "for-each-ref"], cwd=project, capture_output=True, text=True).stdout
    assert out == "" and not (project / ".git" / "index").exists()


@needs_git
def test_undo_to_an_older_checkpoint_and_turns_without_changes_are_skipped(anthropic, make_agent, project):
    (project / "f.txt").write_text("v1\n")
    fake = anthropic()
    fake.script = [
        tool("write_file", path="f.txt", content="v2\n"), text("ok"),
        tool("bash", command="echo read-only"), text("ok"),
        tool("bash", command="echo v3 > f.txt && echo n > g.txt"), text("ok"),
    ]
    a = make_agent(fake)
    a.auto_approve_all = True
    a.session_id = "s2"
    for prompt in ("first", "look", "second"):
        a.send(prompt)
    items = a.checkpoints.items()
    assert [c["prompt"] for c in items] == ["first", "second"]
    assert a.checkpoints.mode() == "snapshot"  # a small folder without git still gets snapshots
    a.checkpoints.undo(items[0]["n"])
    assert (project / "f.txt").read_text() == "v1\n" and not (project / "g.txt").exists()
    assert a.checkpoints.items() == [] and a.messages == []


def test_file_mode_backs_up_edits(anthropic, make_agent, project, monkeypatch):
    monkeypatch.setattr(cp, "SMALL_TREE_FILES", 0)  # pretend the folder is huge and not a repo
    (project / "x.txt").write_text("old")
    fake = anthropic()
    fake.script = [tools(("write_file", {"path": "x.txt", "content": "new"}),
                         ("write_file", {"path": "made.txt", "content": "m"})), text("ok")]
    a = make_agent(fake)
    a.auto_approve_all = True
    a.session_id = "s3"
    a.send("edit")
    assert a.checkpoints.mode() == "files"
    res = a.checkpoints.undo(rewind_chat=False)
    assert (project / "x.txt").read_text() == "old" and not (project / "made.txt").exists()
    assert res["files"] == ["made.txt", "x.txt"] and not res["chat"]
    assert "undid the file changes" in a.messages[-1]["content"][-1]["text"]


def test_checkpoints_survive_reload_and_compaction_keeps_file_undo(anthropic, make_agent, project, monkeypatch):
    monkeypatch.setattr(cp, "SMALL_TREE_FILES", 0)
    fake = anthropic()
    fake.script = [tool("write_file", path="y.txt", content="y"), text("ok")]
    a = make_agent(fake)
    a.auto_approve_all = True
    a.session_id = "s4"
    a.send("make y")
    a.reset()  # same as after /compact
    b = make_agent(anthropic())
    b.session_id = "s4"
    [c] = b.checkpoints.items()
    assert c["msg_index"] is None
    b.checkpoints.undo()
    assert not (project / "y.txt").exists()


def test_nothing_to_undo(anthropic, make_agent):
    a = make_agent(anthropic())
    with pytest.raises(cp.CheckpointError):
        a.checkpoints.undo()


def test_desktop_app_undo_endpoint(anthropic, project):
    import json
    import time
    import urllib.request

    from anvil.config import Config, SESSIONS_DIR
    from anvil.gui.server import App, start_server

    fake = anthropic()
    fake.script = [tool("write_file", path="w.txt", content="w"), text("wrote it")]
    cfg = Config(provider="anthropic", base_url=fake.url, api_key="k", auto_compact_tokens=0,
                 cache_keepalive_minutes=0, browser_enabled=False)
    app = App(cfg, project)
    srv = start_server(app, 0)
    try:
        def call(method, path, body=None):
            req = urllib.request.Request(f"http://127.0.0.1:{srv.server_address[1]}{path}", method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Content-Type": "application/json", "X-Anvil-Token": app.token})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())

        sid = call("POST", "/api/sessions", {"root": str(project)})["meta"]["id"]
        call("POST", f"/api/sessions/{sid}/mode", {"mode": "auto"})
        call("POST", f"/api/sessions/{sid}/send", {"text": "write w"})
        for _ in range(100):
            if not app.sessions[sid].running and (SESSIONS_DIR / f"{sid}.json").exists():
                break
            time.sleep(0.05)
        cps = call("GET", f"/api/sessions/{sid}/checkpoints")["checkpoints"]
        assert [c["files"] for c in cps] == [["w.txt"]]
        d = call("POST", f"/api/sessions/{sid}/undo", {"n": cps[0]["n"]})
        assert d["undo"]["files"] == ["w.txt"] and d["items"] == [] and d["checkpoints"] == []
        assert not (project / "w.txt").exists() and not (SESSIONS_DIR / f"{sid}.json").exists()
    finally:
        srv.shutdown()


def test_cli_undo_and_checkpoints_commands(anthropic, make_agent, project, monkeypatch):
    from anvil import cli

    fake = anthropic()
    fake.script = [tool("write_file", path="c.txt", content="c"), text("ok")]
    a = make_agent(fake)
    a.auto_approve_all = True
    a.session_id = "cli1"
    a.send("make c")
    printed = []
    monkeypatch.setattr(cli.console, "print", lambda *x, **k: printed.append(" ".join(map(str, x))))
    cli.handle_command(a, "/checkpoints")
    assert any("make c" in p for p in printed)
    monkeypatch.setattr(cli.console, "input", lambda *_: "n")
    cli.handle_command(a, "/undo")
    assert (project / "c.txt").exists()
    monkeypatch.setattr(cli.console, "input", lambda *_: "y")
    monkeypatch.setattr(cli, "save_session", lambda *a, **k: None)
    cli.handle_command(a, "/undo")
    assert not (project / "c.txt").exists() and a.messages == []
