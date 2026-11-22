import json

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from anvil import cli, safety
from anvil.agent import build_system_prompt
from anvil.cloud.server import clean_settings
from anvil.simple import ask_key, bash_summary, clip, is_exit_word, next_mode, parse_answer
from anvil.tools import Tools, validate_input


def _run(a, name, **args):
    return a._run_tool({"id": "t", "name": name, "input": args})


def test_next_mode_cycles_and_respects_the_ceiling():
    assert [next_mode(m, "auto") for m in ("ask", "edits", "auto")] == ["edits", "auto", "ask"]
    assert next_mode("ask", "edits") == "edits" and next_mode("edits", "edits") == "ask"
    assert next_mode("ask", "ask") == "ask"


def test_parse_answer_understands_short_and_hinglish_answers():
    keys = {"y": "yes", "a": "always", "n": "no"}
    assert parse_answer("", keys) == "yes"
    assert parse_answer("A", keys) == "always"
    assert parse_answer("haan", keys) == "yes" and parse_answer("nahi", keys) == "no"
    assert parse_answer("use pytest -x instead", keys) == "use pytest -x instead"


def _press(text, keys=None):
    keys = keys or {"y": "yes", "a": "always", "n": "no"}
    with create_pipe_input() as inp:
        inp.send_text(text)
        from prompt_toolkit.application import create_app_session
        with create_app_session(input=inp, output=DummyOutput()):
            return ask_key("❯ ", keys)


def test_single_key_answers_without_enter():
    assert _press("a") == "always"
    assert _press("y") == "yes"
    assert _press("N") == "no"
    assert _press("\r") == "yes"


def test_typing_text_gives_instructions_instead():
    # "d" starts the line, so later "a"/"n"/"y" are plain letters
    assert _press("dont install, any other way?\r") == "dont install, any other way?"


def test_bash_summary_and_clip():
    assert bash_summary("hinglish", "line\n[exit code 0]") == ("ho gaya", "line", False)
    assert bash_summary("english", "boom\n[exit code 2]") == ("failed (exit code 2)", "boom", True)
    shown, more = clip("\n".join(str(i) for i in range(10)), 3)
    assert shown == "0\n1\n2" and more == 7
    assert clip("x" * 500, 1, 50)[0].endswith("…")


def test_hinglish_setting_changes_the_system_prompt(anthropic, make_agent, project):
    assert "Hinglish" not in build_system_prompt(project)
    a = make_agent(anthropic(), language="hinglish")
    assert "Hinglish" in a.system
    a.config.language = "english"
    a.reset_system()
    assert "Hinglish" not in a.system


def test_bash_description_is_accepted_and_not_passed_to_the_shell(project):
    args = {"command": "echo hi", "description": "Say hi"}
    assert validate_input("bash", args) is None
    assert "hi" in Tools(project).run("bash", args)


def test_always_explains_itself_and_covers_both_edit_tools(anthropic, make_agent, project):
    a = make_agent(anthropic(), answers=["always"])
    _run(a, "write_file", path="x.txt", content="x")
    _run(a, "write_file", path="y.txt", content="y")
    _run(a, "bash", command="echo 1")  # bash still asks: "always" for edits doesn't cover commands
    assert [x[0] for x in a.ui.asked] == ["write_file", "bash"]
    assert any("edits" in n for n in a.ui.notices)


def test_always_under_a_strict_limit_says_why_it_keeps_asking(anthropic, make_agent, tmp_path, monkeypatch):
    pol = tmp_path / "p.json"
    pol.write_text(json.dumps({"permission_ceiling": "edits"}))
    monkeypatch.setattr(safety, "POLICY_FILE", pol)
    a = make_agent(anthropic(), answers=["always", "yes"], language="hinglish")
    _run(a, "bash", command="echo a")
    _run(a, "bash", command="echo b")
    assert len(a.ui.asked) == 2
    assert any("permission limit 'edits'" in n for n in a.ui.notices)


def test_set_mode_caps_at_the_ceiling(anthropic, make_agent, tmp_path, monkeypatch):
    a = make_agent(anthropic())
    assert a.set_mode("auto") == "auto" and a.mode == "auto"
    pol = tmp_path / "p.json"
    pol.write_text(json.dumps({"permission_ceiling": "edits"}))
    monkeypatch.setattr(safety, "POLICY_FILE", pol)
    assert a.set_mode("auto") == "edits" and a.accept_edits and not a.auto_approve_all


def test_clean_settings_keeps_only_known_languages():
    assert clean_settings({"language": "hinglish"})["language"] == "hinglish"
    assert clean_settings({"language": "klingon"})["language"] == "english"


def test_terminal_confirm_is_short_and_in_hinglish(monkeypatch):
    from anvil.config import Config
    ui = cli.TerminalUI(config=Config(language="hinglish"))
    answers = iter(["v", "a"])
    monkeypatch.setattr(cli.console, "input", lambda *a, **k: next(answers))
    long_cmd = "\n".join(f"echo {i}" for i in range(20))
    with cli.console.capture() as cap:
        got = ui.confirm("bash", {"command": long_cmd, "description": "Numbers print kar raha hoon"}, "$ " + long_cmd)
    out = cap.get()
    assert got == "always"
    assert "Ye command chalaun?" in out and "Numbers print kar raha hoon" in out
    assert "poora dekhne ke liye v dabao" in out  # first view is collapsed
    assert "echo 19" in out  # after v, the whole command is shown


def test_terminal_bash_result_is_summarized(monkeypatch):
    from anvil.config import Config
    ui = cli.TerminalUI(config=Config(language="hinglish"))
    out_lines = "\n".join(f"line {i}" for i in range(10))
    with cli.console.capture() as cap:
        ui.tool_result("bash", {}, out_lines + "\n[exit code 0]", False)
    out = cap.get()
    assert "✓ ho gaya" in out and "line 9" in out and "line 6" not in out and "upar 7 lines aur" in out


def test_lang_and_auto_commands(anthropic, make_agent):
    a = make_agent(anthropic())
    cli.handle_command(a, "/lang hinglish")
    assert a.config.language == "hinglish" and "Hinglish" in a.system
    cli.handle_command(a, "/auto auto")
    assert a.mode == "auto"
    cli.handle_command(a, "/auto")
    assert a.mode == "ask"


class _Prompts:
    """Stands in for the prompt session: returns lines, or raises what it's given."""

    def __init__(self, items):
        self.items = list(items)

    def prompt(self, *a, **k):
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _interactive(monkeypatch, a, items):
    sent = []
    prompts = _Prompts(items)
    monkeypatch.setattr(cli, "make_prompt_session", lambda agent=None: prompts)
    monkeypatch.setattr(cli, "run_turn", lambda agent, text: sent.append(text))
    with cli.console.capture() as cap:
        cli.interactive(a)
    return sent, prompts.items, cap.get()


def test_exit_words_quit_instead_of_going_to_the_agent(anthropic, make_agent, monkeypatch):
    for word in ("exit", "Quit", "bye", "band karo"):
        sent, left, _ = _interactive(monkeypatch, make_agent(anthropic()), ["hello", word, "never read"])
        assert sent == ["hello"] and left == ["never read"]
    assert not is_exit_word("exit the loop early in main.py")


def test_ctrl_c_twice_quits_and_once_shows_a_hint(anthropic, make_agent, monkeypatch):
    a = make_agent(anthropic(), language="hinglish")
    sent, left, out = _interactive(monkeypatch, a, [KeyboardInterrupt(), KeyboardInterrupt(), "never read"])
    assert left == ["never read"] and "Dobara Ctrl+C" in out
    monkeypatch.setattr(cli, "CTRL_C_QUIT_SECONDS", 0)  # too slow: the second press is a new first press
    sent, left, _ = _interactive(monkeypatch, a, [KeyboardInterrupt(), KeyboardInterrupt(), "hi", EOFError()])
    assert sent == ["hi"] and left == []
