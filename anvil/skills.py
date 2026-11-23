"""Skills: reusable instructions the agent loads on demand.

A skill is either a folder containing SKILL.md (plus any helper files) or a single
<name>.md file. SKILL.md may start with front matter:

    ---
    name: deploy-setup
    description: One line saying what the skill does and when to use it.
    ---
    Instructions...

Search order (later wins on name clashes): built-in, plugins, personal, project.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import CONFIG_DIR

BUILTIN_DIR = Path(__file__).parent / "builtin_skills"
PERSONAL_DIR = CONFIG_DIR / "skills"
PROJECT_SUBDIR = Path(".anvil") / "skills"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_SKILL_CHARS = 50_000


@dataclass
class Skill:
    name: str
    description: str
    path: Path          # the SKILL.md or <name>.md file
    source: str         # built-in | personal | project

    @property
    def folder(self) -> Path | None:
        return self.path.parent if self.path.name == "SKILL.md" else None

    def body(self) -> str:
        return _split_front_matter(self.path.read_text(errors="replace"))[1][:MAX_SKILL_CHARS]

    def files(self) -> list[Path]:
        if not self.folder:
            return []
        return sorted(p for p in self.folder.rglob("*") if p.is_file() and p.name != "SKILL.md"
                      and not any(part.startswith(".") for part in p.relative_to(self.folder).parts))


def _split_front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            meta = {}
            for line in lines[1:i]:
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip().lower()] = v.strip().strip('"').strip("'")
            return meta, "\n".join(lines[i + 1:]).lstrip("\n")
    return {}, text


def _first_line(body: str) -> str:
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:200]
    return ""


def _scan(base: Path, source: str) -> list[Skill]:
    if not base.is_dir():
        return []
    found = []
    candidates = list(base.glob("*/SKILL.md")) + list(base.glob("*/*/SKILL.md")) + list(base.glob("*.md"))
    for path in sorted(candidates):
        if path.name.lower() == "readme.md":
            continue
        try:
            meta, body = _split_front_matter(path.read_text(errors="replace"))
        except OSError:
            continue
        default = path.parent.name if path.name == "SKILL.md" else path.stem
        name = (meta.get("name") or default).lower()
        if not NAME_RE.match(name):
            continue
        found.append(Skill(name, meta.get("description") or _first_line(body), path, source))
    return found


def discover(root: Path | None) -> dict[str, Skill]:
    skills: dict[str, Skill] = {}
    from .plugins import skill_dirs

    sources = [(BUILTIN_DIR, "built-in"), *skill_dirs(), (PERSONAL_DIR, "personal")]
    if root:
        sources.append((root / PROJECT_SUBDIR, "project"))
    for base, source in sources:
        for s in _scan(base, source):
            skills[s.name] = s
    return dict(sorted(skills.items()))


def render_for_model(skill: Skill) -> str:
    """What the load_skill tool returns."""
    parts = [f'<skill name="{skill.name}" source="{skill.source}" path="{skill.path}">', skill.body(), "</skill>"]
    files = skill.files()
    if files:
        parts.append("\nFiles bundled with this skill (read them with read_file when needed):")
        parts += [f"- {p}" for p in files[:100]]
    return "\n".join(parts)


def expand_invocation(text: str, skills: dict[str, Skill]) -> str | None:
    """Turn '/<skill> args' typed by the user into a prompt that carries the skill."""
    m = re.match(r"^/([a-z0-9][a-z0-9-]*)(?:\s+(.*))?$", text.strip(), re.S)
    if not m or m.group(1) not in skills:
        return None
    skill, args = skills[m.group(1)], (m.group(2) or "").strip()
    task = args or "Follow this skill's instructions for the current project."
    return f"The user invoked the /{skill.name} skill.\n\n{render_for_model(skill)}\n\nUser request: {task}"


def display_text(text: str) -> str:
    """Show an expanded skill invocation the way the user typed it."""
    m = re.match(r"^The user invoked the /([a-z0-9-]+) skill\.", text)
    if not m:
        return text
    task = text.rsplit("\n\nUser request: ", 1)[-1]
    default = task.startswith("Follow this skill's instructions")
    return f"/{m.group(1)}" + ("" if default else f" {task}")


def sync_dirs(root: Path | None) -> list[str]:
    """git pull every skills folder that is a git checkout. Returns human-readable results."""
    dirs = [PERSONAL_DIR] + ([p for p in PERSONAL_DIR.iterdir() if p.is_dir()] if PERSONAL_DIR.is_dir() else [])
    if root:
        dirs.append(root / PROJECT_SUBDIR)
    results = []
    for d in dirs:
        if not (d / ".git").exists():
            continue
        proc = subprocess.run(["git", "-C", str(d), "pull", "--ff-only"], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=120)
        out = (proc.stdout or proc.stderr).strip().splitlines()
        results.append(f"{d}: {'ok' if proc.returncode == 0 else 'failed'} ({out[-1] if out else ''})")
    return results or ["No skills folders are git repositories. Add one with: anvil --add-skills <git-url>"]


def add_repo(url: str) -> Path:
    """Clone a skills repository into the personal skills folder."""
    url = url.strip()
    if not url or url.startswith("-") or any(c in url for c in "\n\r\0"):
        raise RuntimeError("that doesn't look like a git URL")
    PERSONAL_DIR.mkdir(parents=True, exist_ok=True)
    last = re.split(r"[/\\]", url.rstrip("/\\"))[-1]  # URL, scp-style git@host:repo, or a Windows path
    name = re.sub(r"\.git$", "", last.split(":")[-1])  # "host:repo.git" (scp form without a slash)
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise RuntimeError("can't work out a folder name from that URL")
    dest = PERSONAL_DIR / name
    if dest.exists():
        raise RuntimeError(f"{dest} already exists; use /skills sync to update it")
    proc = subprocess.run(["git", "clone", "--depth", "1", "--", url, str(dest)], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git clone failed")
    return dest
