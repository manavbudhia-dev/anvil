import subprocess

import pytest

from anvil import skills


def test_builtin_skills_exist(project):
    names = skills.discover(project)
    assert {"deploy-setup", "review", "commit", "caddy", "browse"} <= set(names)


def test_project_overrides_builtin(project):
    d = project / ".anvil" / "skills" / "review"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\ndescription: Our own review\n---\nDo it our way.\n")
    s = skills.discover(project)["review"]
    assert s.source == "project" and s.description == "Our own review"


def test_expand_and_display(project):
    found = skills.discover(project)
    e = skills.expand_invocation("/review src/", found)
    assert e.startswith("The user invoked the /review skill.") and e.endswith("User request: src/")
    assert skills.display_text(e) == "/review src/"
    assert skills.display_text(skills.expand_invocation("/review", found)) == "/review"
    assert skills.expand_invocation("/nonexistent", found) is None


@pytest.mark.parametrize("url", ["--upload-pack=touch /tmp/pwned", "https://x/..", "", "-oProxyCommand=x"])
def test_add_repo_rejects_bad_urls(url):
    with pytest.raises(RuntimeError):
        skills.add_repo(url)


def test_add_and_sync_skills_repo(tmp_path, project):
    repo = tmp_path / "team-skills"
    (repo / "fastapi").mkdir(parents=True)
    (repo / "fastapi" / "SKILL.md").write_text("---\ndescription: FastAPI v1\n---\nUse routers.\n")
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(git + ["-C", str(repo), "add", "-A"], check=True)
    subprocess.run(git + ["-C", str(repo), "commit", "-qm", "v1"], check=True)
    dest = skills.add_repo(str(repo))
    try:
        assert skills.discover(project)["fastapi"].description == "FastAPI v1"
        (repo / "fastapi" / "SKILL.md").write_text("---\ndescription: FastAPI v2\n---\nUse routers.\n")
        subprocess.run(git + ["-C", str(repo), "commit", "-qam", "v2"], check=True)
        assert any("ok" in line for line in skills.sync_dirs(project))
        assert skills.discover(project)["fastapi"].description == "FastAPI v2"
    finally:
        import shutil
        shutil.rmtree(dest, ignore_errors=True)
