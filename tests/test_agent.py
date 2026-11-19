import json
import os

from fakes import text, tool, tools

from anvil import safety


def test_tool_loop_runs_tools_and_sends_results(anthropic, make_agent, project):
    fake = anthropic()
    fake.script = [tools(("write_file", {"path": "hello.py", "content": "print('hi')\n"}),
                         ("read_file", {"path": "hello.py"})),
                   text("Created hello.py.")]
    a = make_agent(fake)
    a.send("make hello.py")
    assert (project / "hello.py").read_text() == "print('hi')\n"
    second = fake.requests[1]["body"]["messages"]
    results = second[-1]["content"]
    assert [r["type"] for r in results] == ["tool_result", "tool_result"]
    assert "print('hi')" in results[1]["content"]
    assert a.messages[-1]["content"][0]["text"] == "Created hello.py."


def test_invalid_tool_input_is_reported_to_the_model(anthropic, make_agent):
    fake = anthropic()
    fake.script = [tool("read_file", wrong=1), text("ok")]
    a = make_agent(fake)
    a.send("go")
    res = fake.requests[1]["body"]["messages"][-1]["content"][0]
    assert res["is_error"] and "INVALID_JSON" in res["content"]


def _run(a, name, **args):
    return a._run_tool({"id": "t", "name": name, "input": args})


def test_ask_mode_asks_for_commands_and_always_allow_sticks(anthropic, make_agent):
    a = make_agent(anthropic(), answers=["always"])
    _run(a, "bash", command="echo one")
    _run(a, "bash", command="echo two")
    assert [x[0] for x in a.ui.asked] == ["bash"]


def test_destructive_commands_always_ask_even_in_auto_mode(anthropic, make_agent, project):
    a = make_agent(anthropic(), answers=["always", "always"])
    a.auto_approve_all = True
    (project / "tmpdir").mkdir()
    _run(a, "bash", command="echo safe")
    _run(a, "bash", command="rm -rf tmpdir")
    _run(a, "bash", command="rm -rf tmpdir2")  # "always" must not stick for destructive calls
    assert len(a.ui.asked) == 2 and all(d for _, _, d in a.ui.asked)


def test_declined_call_is_not_run(anthropic, make_agent, project):
    a = make_agent(anthropic(), answers=["no"])
    res = _run(a, "write_file", path="x.txt", content="x")
    assert res["is_error"] and not (project / "x.txt").exists()


def test_policy_ceiling_overrides_auto_mode(anthropic, make_agent, tmp_path, monkeypatch):
    pol = tmp_path / "p.json"
    pol.write_text(json.dumps({"permission_ceiling": "ask"}))
    monkeypatch.setattr(safety, "POLICY_FILE", pol)
    a = make_agent(anthropic(), answers=["always", "yes"])
    a.auto_approve_all = True
    _run(a, "bash", command="echo a")
    _run(a, "bash", command="echo b")
    assert len(a.ui.asked) == 2  # neither auto mode nor "always" applies under an "ask" policy


def test_writes_outside_project_always_ask(anthropic, make_agent, tmp_path):
    a = make_agent(anthropic())
    a.auto_approve_all = True
    _run(a, "write_file", path=str(tmp_path / "outside.txt"), content="x")
    assert a.ui.asked and a.ui.asked[0][0] == "write_file"


def test_skill_invocation_reaches_the_model(anthropic, make_agent, project):
    skill = project / ".anvil" / "skills" / "release"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\ndescription: Release checklist\n---\nBump version and tag.\n")
    fake = anthropic()
    a = make_agent(fake)
    assert "release: Release checklist" in a.system
    a.send(a.expand_skill("/release v1.2.0"))
    first = fake.requests[0]["body"]["messages"][0]["content"][0]["text"]
    assert '<skill name="release"' in first and first.endswith("User request: v1.2.0")


def test_auto_compact_replaces_history_with_summary(anthropic, make_agent):
    fake = anthropic()
    a = make_agent(fake, auto_compact_tokens=5000)
    a.send("one")
    a.send("two")
    assert any("summarizing the conversation" in n for n in a.ui.notices)
    assert len(a.messages) == 2 and a.messages[0]["content"][0]["text"].startswith("Summary")


def test_bash_runs_in_project_dir(anthropic, make_agent, project):
    a = make_agent(anthropic())
    a.auto_approve_all = True
    res = _run(a, "bash", command="cd" if os.name == "nt" else "pwd")
    assert os.path.normcase(str(project)) in os.path.normcase(res["content"])
