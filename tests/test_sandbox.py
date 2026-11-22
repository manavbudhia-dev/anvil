import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest

from anvil import safety, sandbox
from anvil.sandbox import SandboxSettings, load_settings, wrap
from anvil.tools import ToolError, Tools


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    monkeypatch.delenv("ANVIL_SANDBOX", raising=False)
    monkeypatch.setattr(safety, "POLICY_FILE", tmp_path / "no-policy.json")
    monkeypatch.setattr(sandbox, "CONFIG_FILE", tmp_path / "no-config.json")
    sandbox._probe_cache.clear()
    yield
    sandbox._probe_cache.clear()


def _project(tmp_path, settings=None):
    root = tmp_path / "proj"
    root.mkdir()
    if settings is not None:
        (root / ".anvil").mkdir()
        (root / ".anvil" / "settings.json").write_text(json.dumps({"sandbox": settings}))
    return root


def test_off_by_default(tmp_path):
    root = _project(tmp_path)
    assert not load_settings(root).enabled
    assert wrap("echo hi", root) is None


def test_project_can_turn_it_on_but_not_off(tmp_path):
    root = _project(tmp_path, {"mode": "auto", "network": False})
    s = load_settings(root, config_sandbox={})
    assert s.mode == "auto" and s.network is False

    root2 = tmp_path / "other"
    root2.mkdir()
    (root2 / ".anvil").mkdir()
    (root2 / ".anvil" / "settings.json").write_text(json.dumps({"sandbox": {"mode": "off", "network": True}}))
    s = load_settings(root2, config_sandbox={"mode": "bwrap", "network": False})
    assert s.mode == "bwrap" and s.network is False  # a repo can't loosen the user's sandbox


def test_project_writable_paths_must_stay_inside(tmp_path):
    root = _project(tmp_path, {"mode": "auto", "writable": ["cache", "/", "~"]})
    (root / "cache").mkdir()
    assert load_settings(root, config_sandbox={}).writable == ["cache"]


def test_policy_can_require_it(tmp_path, monkeypatch):
    pol = tmp_path / "policy.json"
    pol.write_text(json.dumps({"sandbox": "required"}))
    monkeypatch.setattr(safety, "POLICY_FILE", pol)
    root = _project(tmp_path, {"mode": "off"})
    s = load_settings(root, config_sandbox={"mode": "off"})
    assert s.required and s.mode == "auto"
    monkeypatch.setattr(sandbox, "candidates", lambda: [])
    with pytest.raises(sandbox.SandboxUnavailable):
        wrap("echo hi", root, s)


def test_required_without_backend_refuses_to_run(tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setenv("ANVIL_SANDBOX", "auto")
    monkeypatch.setattr(sandbox, "candidates", lambda: [])
    assert wrap("echo hi", root) is None  # auto with nothing available: runs unsandboxed
    monkeypatch.setattr(sandbox, "load_settings", lambda r: SandboxSettings(mode="auto", required=True))
    with pytest.raises(ToolError, match="not run"):
        Tools(root)._t_bash("echo hi")


def test_command_lines(tmp_path, monkeypatch):
    root = _project(tmp_path).resolve()
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    s = SandboxSettings(mode="auto", network=False)
    b = sandbox.bwrap_argv("echo hi", root, s)
    assert b[:3] == ["/usr/bin/bwrap", "--ro-bind", "/"] and "--unshare-all" in b and "--share-net" not in b
    assert ["--bind", str(root), str(root)] == b[b.index(str(root)) - 1:b.index(str(root)) + 2]
    assert b[-2:] == ["-c", "echo hi"]
    f = sandbox.firejail_argv("echo hi", root, s)
    assert "--read-only=/" in f and f"--read-write={root}" in f and "--net=none" in f
    d = sandbox.docker_argv("echo hi", root, s)
    assert "--network" in d and "none" in d and d[-1] == "echo hi" and "--cap-drop" in d
    prof = sandbox.sandbox_exec_profile(root, s)
    assert "(deny file-write*)" in prof and os.path.realpath(root) in prof and "network-outbound" in prof


# --- live checks: each runs only where its backend works -------------------------------

def _live(backend, tmp_path, image=None):
    if not sandbox.backend_works(backend):
        pytest.skip(f"{backend} isn't available here")
    root = _project(tmp_path).resolve()
    real_home = Path.home()
    fake_home = real_home / f".anvil-sandbox-test-{uuid.uuid4().hex[:8]}"
    outside = real_home / f"anvil-sandbox-outside-{uuid.uuid4().hex[:8]}"
    try:
        (fake_home / ".ssh").mkdir(parents=True)
        (fake_home / ".ssh" / "id_test").write_text("SECRET-KEY")
        s = SandboxSettings(mode=backend, network=False, image=image or sandbox.DEFAULT_IMAGE)
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("HOME", str(fake_home))
            mp.setattr(Path, "home", classmethod(lambda cls: fake_home))
            argv = wrap(f"echo ok > inside.txt; touch '{outside}'; cat '{fake_home}/.ssh/id_test'; echo done",
                        root, s)
        import subprocess
        r = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=300)
        assert (root / "inside.txt").read_text().strip() == "ok", r.stderr
        assert not outside.exists(), "wrote outside the project"
        assert "SECRET-KEY" not in r.stdout, "secret was readable"
        assert "done" in r.stdout, (r.stdout, r.stderr)
    finally:
        shutil.rmtree(fake_home, ignore_errors=True)
        if outside.exists():
            outside.unlink()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_live_bwrap(tmp_path):
    _live("bwrap", tmp_path)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
def test_live_firejail(tmp_path):
    _live("firejail", tmp_path)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_live_sandbox_exec(tmp_path):
    _live("sandbox-exec", tmp_path)


@pytest.mark.skipif(not os.environ.get("ANVIL_TEST_DOCKER"), reason="set ANVIL_TEST_DOCKER=1 to run (pulls an image)")
def test_live_docker(tmp_path):
    _live("docker", tmp_path, image="alpine:3.20")


def test_bash_tool_uses_the_sandbox(tmp_path, monkeypatch):
    backend = next((b for b in sandbox.candidates() if b not in ("docker", "podman") and sandbox.backend_works(b)), None)
    if not backend:
        pytest.skip("no local sandbox backend here")
    root = _project(tmp_path, {"mode": backend})
    out = Tools(root)._t_bash("echo $ANVIL_SANDBOX; echo hi > made.txt")
    assert backend in out, out
    assert (root / "made.txt").exists()
