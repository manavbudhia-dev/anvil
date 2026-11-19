import os
import subprocess
import sys
import time

import pytest

from anvil.tools import ToolError, Tools, validate_input

posix_only = pytest.mark.skipif(os.name == "nt", reason="uses POSIX shell syntax")


@pytest.fixture
def t(project):
    return Tools(project)


def test_write_read_edit_roundtrip(t, project):
    assert "Created" in t.run("write_file", {"path": "a.py", "content": "x = 1\ny = 2\n"})
    assert "     1\tx = 1" in t.run("read_file", {"path": "a.py"})
    assert "Edited" in t.run("edit_file", {"path": "a.py", "old_string": "y = 2", "new_string": "y = 3"})
    assert (project / "a.py").read_text() == "x = 1\ny = 3\n"


def test_edit_errors(t, project):
    (project / "b.txt").write_text("same\nsame\n")
    with pytest.raises(ToolError, match="occurs 2 times"):
        t.run("edit_file", {"path": "b.txt", "old_string": "same", "new_string": "x"})
    with pytest.raises(ToolError, match="not found"):
        t.run("edit_file", {"path": "b.txt", "old_string": "nope", "new_string": "x"})


def test_grep_and_glob(t, project):
    (project / "src").mkdir()
    (project / "src" / "m.py").write_text("def hello():\n    pass\n")
    assert "src" in t.run("glob", {"pattern": "**/*.py"})
    assert "m.py:1:def hello" in t.run("grep", {"pattern": "def hello"}).replace(os.sep, "/")


def test_validate_input():
    assert validate_input("bash", {"cmd": "x"}) == "missing required field 'command'"
    assert "must be string" in validate_input("read_file", {"path": 3})
    assert "must be one of" in validate_input("browser_scroll", {"direction": "sideways"})
    assert validate_input("nope", {}) == "unknown tool 'nope'"


def test_config_files_are_backed_up_before_edits(t, project):
    (project / "app.conf").write_text("v1\n")
    out = t.run("edit_file", {"path": "app.conf", "old_string": "v1", "new_string": "v2"})
    backup = out.split("backup of the previous version: ")[1].rstrip(")")
    assert open(backup).read() == "v1\n"
    (project / "main.py").write_text("print(1)\n")
    assert "backup" not in t.run("edit_file", {"path": "main.py", "old_string": "1", "new_string": "2"})


@posix_only
def test_background_process_does_not_block(t):
    # Regression: with pipes, `cmd &` blocked until the timeout and was reported as timed out.
    start = time.time()
    out = t.run("bash", {"command": "sleep 30 & echo started", "timeout": 20})
    assert time.time() - start < 5
    assert "started" in out and "[exit code 0]" in out
    subprocess.run("pkill -f 'sleep 3[0]'", shell=True)


@posix_only
def test_timeout_kills_children(t):
    out = t.run("bash", {"command": 'bash -c "sleep 51" & sleep 52', "timeout": 2})
    assert "timed out after 2s" in out
    left = subprocess.run("pgrep -f 'sleep 5[12]'", shell=True, capture_output=True, text=True).stdout.split()
    assert left == []


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree handling")
def test_windows_timeout_kills_tree(t):
    out = t.run("bash", {"command": "ping -n 30 127.0.0.1 > nul", "timeout": 2})
    assert "timed out after 2s" in out
    tasks = subprocess.run(["tasklist", "/FI", "IMAGENAME eq PING.EXE"], capture_output=True, text=True).stdout
    assert "PING.EXE" not in tasks.upper()


def test_exit_code_and_stderr(t):
    cmd = "echo out && echo err 1>&2 && exit 3"
    out = t.run("bash", {"command": cmd})
    assert "out" in out and "[stderr]" in out and "err" in out and "[exit code 3]" in out


def test_python_runs(t):
    exe = sys.executable.replace("\\", "/")
    assert "42" in t.run("bash", {"command": f'"{exe}" -c "print(42)"'})
