"""deploy.sh and anvil-caddy-apply, run for real against temporary git repos and a stand-in caddy."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "anvil" / "builtin_skills" / "deploy-setup" / "deploy.sh"
HELPER = ROOT / "server" / "anvil-caddy-apply"

pytestmark = pytest.mark.skipif(os.name == "nt" or not shutil.which("bash") or not shutil.which("git"),
                                reason="needs bash and git (Linux/macOS)")
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}


def sh(cmd, cwd, check=True):
    return subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, env=GIT_ENV, check=check)


@pytest.mark.parametrize("script", [DEPLOY, HELPER, ROOT / "install.sh", ROOT / "server" / "setup-agent-user.sh"])
def test_scripts_parse(script):
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0


@pytest.fixture
def repos(tmp_path):
    sh("git init -q --bare -b main origin.git && git clone -q origin.git dev", tmp_path)
    dev = tmp_path / "dev"
    shutil.copy(DEPLOY, dev / "deploy.sh")
    (dev / "deploy.env.example").write_text(
        'BUILD_CMD="./build.sh"\nRESTART="nohup sleep 300 >/dev/null 2>&1 & echo running \\$(git rev-parse --short HEAD) > ../running.txt"\n'
        'HEALTHCHECK_CMD="grep -q ok health.txt"\nHEALTHCHECK_TIMEOUT=3\n')
    (dev / "build.sh").write_text("#!/bin/sh\necho building\n")
    (dev / "health.txt").write_text("ok\n")
    (dev / ".gitignore").write_text("deploy.env\n.deploy.log\n.deploy.lock\n")
    sh("chmod +x deploy.sh build.sh && git add -A && git commit -qm v1 && git push -q origin main", dev)
    sh("git clone -q origin.git server && cp server/deploy.env.example server/deploy.env", tmp_path)
    yield tmp_path
    subprocess.run("pkill -f 'sleep 30[0]'", shell=True)


def commit(root, msg, **files):
    for name, body in files.items():
        (root / "dev" / name).write_text(body)
    sh(f"git commit -qam {msg} && git push -q origin main", root / "dev")


def deploy(root):
    return sh("./deploy.sh", root / "server", check=False)


def test_deploys_rolls_back_and_is_not_blocked_by_background_processes(repos):
    (repos / "dev" / "app.txt").write_text("v2")
    sh("git add app.txt && git commit -qm v2 && git push -q origin main", repos / "dev")
    r = deploy(repos)
    assert r.returncode == 0 and "deployed" in r.stderr
    # Each restart leaves a background process; later deploys must not see a stale lock.
    for v in ("v3", "v4"):
        commit(repos, v, **{"app.txt": v})
        assert deploy(repos).returncode == 0
    good = (repos / "running.txt").read_text()
    commit(repos, "broken", **{"build.sh": "#!/bin/sh\nexit 3\n"})
    r = deploy(repos)
    assert r.returncode == 1 and "rolled back" in r.stderr
    assert (repos / "running.txt").read_text() == good
    commit(repos, "fixed", **{"build.sh": "#!/bin/sh\necho ok\n", "health.txt": "bad\n"})
    r = deploy(repos)
    assert r.returncode == 1 and "rolled back" in r.stderr  # failed health check also rolls back


def test_refuses_uncommitted_changes(repos):
    (repos / "server" / "health.txt").write_text("hacked\n")
    r = deploy(repos)
    assert r.returncode == 1 and "uncommitted changes" in r.stderr


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="the helper must run as root")
def test_caddy_helper_custom_path_and_serialized_runs(tmp_path):
    bin_dir, etc = tmp_path / "bin", tmp_path / "etc"
    bin_dir.mkdir()
    etc.mkdir()
    (bin_dir / "caddy").write_text('#!/bin/sh\ncase "$1" in validate) sleep 1; grep -q bad "$3" && exit 1; exit 0;; reload) exit 0;; esac\n')
    (bin_dir / "caddy").chmod(0o755)
    caddyfile = etc / "Caddyfile"
    caddyfile.write_text("original\n")
    helper = tmp_path / "helper"
    helper.write_text(HELPER.read_text().replace("@CADDYFILE@", str(caddyfile)))
    helper.chmod(0o755)
    a, b, bad = tmp_path / "a.caddy", tmp_path / "b.caddy", tmp_path / "bad.caddy"
    a.write_text("config A\n" * 200)
    b.write_text("config B\n" * 200)
    bad.write_text("bad config\n")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    env.pop("SUDO_USER", None)
    procs = [subprocess.Popen([str(helper), "apply", str(p)], env=env, stderr=subprocess.PIPE) for p in (a, b)]
    [p.wait(60) for p in procs]
    live = caddyfile.read_text()
    assert live in (a.read_text(), b.read_text())  # one complete config, never a mix
    backups = sorted((etc / "anvil-backups").iterdir())
    assert len(backups) == 2 and backups[0].read_text() == "original\n"
    r = subprocess.run([str(helper), "apply", str(bad)], env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "invalid config" in r.stderr and caddyfile.read_text() == live
    assert not list(etc.glob(".Caddyfile.anvil-candidate"))
