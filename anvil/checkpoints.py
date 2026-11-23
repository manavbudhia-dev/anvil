"""Undo for agent turns.

Before the first change in a turn (a file edit or a shell command), Anvil records how the project
looked, so `/undo` can put it back.

- Snapshot mode: the project folder is copied into a private "shadow" git repository under
  ~/.config/anvil/checkpoints (never the project's own .git: no refs, index or stash entries are
  touched there). It sees every change, including ones made by shell commands, and stores each
  file once however many checkpoints share it. Used when the project is a git repository (so its
  .gitignore keeps build output out) or a small folder, and git is installed.
- File mode: otherwise (no git, or a huge folder such as a home directory) only files changed
  with write_file/edit_file are backed up; shell commands can't be undone there.

write_file/edit_file targets are also backed up one by one in snapshot mode, which covers files
outside the project and git-ignored files.
"""
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .config import CONFIG_DIR
from .fsutil import atomic_write_text

CHECKPOINT_DIR = CONFIG_DIR / "checkpoints"
MAX_CHECKPOINTS = 50
MAX_BACKUP_BYTES = 20 * 1024 * 1024
# A folder that isn't a git repository is snapshotted only when it's at most this big.
SMALL_TREE_FILES = 5000
SMALL_TREE_BYTES = 200 * 1024 * 1024
# Never snapshot or restore these, whatever .gitignore says.
EXCLUDES = ["node_modules/", "__pycache__/", ".venv/", "venv/", ".mypy_cache/", ".pytest_cache/",
            ".tox/", ".DS_Store", "*.pyc"]
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".tox"}
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


class CheckpointError(Exception):
    pass


def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _small_tree(root: Path) -> bool:
    files = size = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for f in filenames:
            files += 1
            try:
                size += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
            if files > SMALL_TREE_FILES or size > SMALL_TREE_BYTES:
                return False
    return True


def _is_git_repo(root: Path) -> bool:
    try:
        return subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root, capture_output=True,
                              text=True, stdin=subprocess.DEVNULL, env=_git_env(), timeout=10,
                              **NO_WINDOW).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class Shadow:
    """A private git repository whose work tree is the project folder."""

    def __init__(self, root: Path):
        self.root = root
        key = hashlib.sha256(str(root).encode()).hexdigest()[:16]
        self.git_dir = CHECKPOINT_DIR / "repos" / f"{key}.git"
        with _LOCKS_GUARD:  # desktop-app sessions in the same folder share one shadow index
            self.lock = _LOCKS.setdefault(key, threading.Lock())

    def git(self, *args: str, input: str | None = None, check: bool = True, timeout: int = 120) -> str:
        cmd = ["git", f"--git-dir={self.git_dir}", f"--work-tree={self.root}",
               "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "core.quotepath=false", *args]
        try:
            p = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True, input=input,
                               stdin=None if input is not None else subprocess.DEVNULL,
                               env=_git_env(), timeout=timeout, encoding="utf-8", errors="replace", **NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise CheckpointError(f"git failed: {e}") from e
        if check and p.returncode != 0:
            raise CheckpointError(f"git {args[0]} failed: {(p.stderr or p.stdout).strip()[:500]}")
        return p.stdout

    def ensure(self) -> None:
        if (self.git_dir / "HEAD").exists():
            return
        self.git_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "--bare", "-q", str(self.git_dir)], capture_output=True, check=True,
                       stdin=subprocess.DEVNULL, env=_git_env(), timeout=30, **NO_WINDOW)
        (self.git_dir / "info").mkdir(exist_ok=True)
        (self.git_dir / "info" / "exclude").write_text("\n".join(EXCLUDES) + "\n")
        (self.git_dir / "anvil-root").write_text(str(self.root))

    def snapshot(self) -> str:
        """Record the work tree and return its git tree id."""
        with self.lock:
            self.ensure()
            self.git("add", "-A", "--ignore-errors", "--", ".", check=False, timeout=300)
            return self.git("write-tree").strip()

    def changed(self, old: str, new: str) -> list[tuple[str, str]]:
        """(status, path) for paths that differ between two trees; status is A, D or M."""
        if old == new:
            return []
        out = self.git("diff-tree", "-r", "-z", "--no-renames", "--name-status", old, new)
        parts = out.split("\0")
        return [(parts[i][0], parts[i + 1]) for i in range(0, len(parts) - 1, 2) if parts[i]]

    def restore(self, tree: str) -> list[str]:
        """Make the work tree match `tree`. Returns the paths that changed."""
        current = self.snapshot()
        changes = self.changed(tree, current)
        for status, path in changes:
            if status == "A":  # created after the checkpoint
                p = self.root / path
                try:
                    if p.is_symlink() or p.is_file():
                        p.unlink()
                except OSError:
                    pass
                _prune_empty_dirs(p.parent, self.root)
        keep = [path for status, path in changes if status != "A"]
        if keep:
            with self.lock:
                self.git("checkout", tree, "--pathspec-from-file=-", "--pathspec-file-nul",
                         input="\0".join(keep), timeout=300)
        return [path for _, path in changes]

    def keep(self, ref: str, tree: str) -> None:
        self.git("update-ref", ref, tree)

    def drop(self, ref: str) -> None:
        self.git("update-ref", "-d", ref, check=False)


def _prune_empty_dirs(d: Path, root: Path) -> None:
    while d != root and root in d.parents:
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent


class Checkpoints:
    """The undo history of one conversation. Stored per session id, so it survives /save, /load,
    --resume and desktop-app restarts."""

    def __init__(self, agent):
        self.agent = agent
        self._shadow: Shadow | None = None
        self._mode: str | None = None
        self._turn: dict | None = None   # the turn in progress
        self._opened = False             # whether the turn has recorded its "before" state yet
        self._items: list[dict] | None = None
        self._loaded_for = ""

    # --- storage -----------------------------------------------------------

    @property
    def sid(self) -> str:
        # A conversation without a session id (embedded use, tests) keeps its history in memory only.
        return self.agent.session_id or f"unsaved-{os.getpid()}-{id(self)}"

    @property
    def root(self) -> Path:
        return self.agent.root

    def _dir(self) -> Path:
        return CHECKPOINT_DIR / "sessions" / _safe(self.sid)

    def items(self) -> list[dict]:
        if self._items is None or self._loaded_for != self.sid:
            self._loaded_for = self.sid
            try:
                self._items = json.loads((self._dir() / "index.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._items = []
        return self._items

    def _save(self) -> None:
        if not self.agent.session_id:
            return
        atomic_write_text(self._dir() / "index.json", json.dumps(self.items(), indent=1), private=True)

    def mode(self) -> str:
        """'snapshot' or 'files' (see the module docstring)."""
        if self._mode is None:
            ok = shutil.which("git") is not None and (_is_git_repo(self.root) or _small_tree(self.root))
            self._mode = "snapshot" if ok else "files"
        return self._mode

    def shadow(self) -> Shadow:
        if self._shadow is None:
            self._shadow = Shadow(self.root)
        return self._shadow

    # --- recording a turn --------------------------------------------------

    def begin_turn(self, prompt: str, message_index: int) -> None:
        n = (self.items()[-1]["n"] + 1) if self.items() else 1
        msgs = self.agent.messages
        # After an interrupted turn the new prompt is merged into the last (user) message.
        tail = len(msgs[-1]["content"]) if msgs and msgs[-1]["role"] == "user" and isinstance(msgs[-1]["content"], list) else None
        self._turn = {"n": n, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "prompt": prompt.strip()[:200],
                      "msg_index": message_index, "msg_tail": tail, "tree": None, "files": {}, "changed": []}
        self._opened = False

    def _open(self) -> None:
        if self._turn is None or self._opened:
            return
        self._opened = True
        if self.mode() == "snapshot":
            try:
                self._turn["tree"] = self.shadow().snapshot()
            except (CheckpointError, OSError, subprocess.SubprocessError) as e:
                self._mode = "files"
                self.agent.ui.notice(f"Couldn't snapshot the project for /undo ({e}); "
                                     "only file edits can be undone from now on.")

    def before_bash(self) -> None:
        self._open()

    def before_edit(self, path: Path) -> None:
        """Back up a file that write_file/edit_file is about to change (first change in a turn only)."""
        self._open()
        if self._turn is None:
            return
        key = str(path)
        if key in self._turn["files"]:
            return
        if not path.exists():
            self._turn["files"][key] = None  # undo deletes it
            return
        try:
            if not path.is_file() or path.stat().st_size > MAX_BACKUP_BYTES:
                return
            d = self._dir() / f"turn-{self._turn['n']}"
            d.mkdir(parents=True, exist_ok=True)
            name = f"{len(self._turn['files'])}-{_safe(path.name)}"
            shutil.copy2(path, d / name)
            self._turn["files"][key] = name
        except OSError:
            pass

    def end_turn(self) -> dict | None:
        """Keep the checkpoint if the turn changed anything."""
        t, self._turn = self._turn, None
        if t is None or not self._opened:
            return None
        changed = set()
        if t["tree"]:
            try:
                after = self.shadow().snapshot()
                changed.update(p for _, p in self.shadow().changed(t["tree"], after))
            except CheckpointError:
                pass
        for key in t["files"]:
            changed.add(self._rel(key))
        if not changed:
            shutil.rmtree(self._dir() / f"turn-{t['n']}", ignore_errors=True)
            return None
        t["changed"] = sorted(changed)
        if t["tree"]:
            self.shadow().keep(self._ref(t["n"]), t["tree"])
        items = self.items()
        items.append(t)
        while len(items) > MAX_CHECKPOINTS:
            self._forget(items.pop(0))
        self._save()
        return t

    def chat_reset(self) -> None:
        """The conversation was replaced (compacted or cleared): checkpoints can still restore
        files but no longer rewind the chat."""
        for c in self.items():
            c["msg_index"] = None
        if self.items():
            self._save()

    # --- undo ----------------------------------------------------------------

    def preview(self, n: int | None = None) -> list[str]:
        """Paths that undoing to checkpoint n (default: the latest) would change right now."""
        target, later = self._select(n)
        paths = set()
        for c in later:
            paths.update(self._rel(k) for k in c["files"])
        if target.get("tree") and self.mode() == "snapshot":
            sh = self.shadow()
            paths.update(p for _, p in sh.changed(target["tree"], sh.snapshot()))
        return sorted(paths)

    def undo(self, n: int | None = None, rewind_chat: bool = True) -> dict:
        """Restore the project to how it was before turn n (default: the latest turn), dropping
        that checkpoint and every later one. Returns what changed."""
        target, later = self._select(n)
        restored = set()
        # Per-file backups, newest turn first, so the oldest copy of each file wins...
        for c in reversed(later):
            d = self._dir() / f"turn-{c['n']}"
            for key, name in c["files"].items():
                p = Path(key)
                try:
                    if name is None:
                        if p.is_file() or p.is_symlink():
                            p.unlink()
                    elif (d / name).exists():
                        p.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(d / name, p)
                    restored.add(self._rel(key))
                except OSError as e:
                    raise CheckpointError(f"couldn't restore {key}: {e}") from e
        # ...then the snapshot, which also undoes shell commands and removes files created later.
        if target.get("tree"):
            restored.update(self.shadow().restore(target["tree"]))
        chat = False
        idx, msgs = target.get("msg_index"), self.agent.messages
        if rewind_chat and idx is not None and idx <= len(msgs):
            del msgs[idx:]
            tail = target.get("msg_tail")
            if tail is not None and msgs and isinstance(msgs[-1]["content"], list):
                del msgs[-1]["content"][tail:]
            chat = True
        elif restored and msgs:
            self.agent._append_user([{"type": "text", "text": (
                "[Anvil note: the user undid the file changes made since the request "
                f"\"{target['prompt'][:80]}\". These files are back to their earlier state: "
                + ", ".join(sorted(restored)[:30]) + ". Re-read files before editing them.]")}])
        items = self.items()
        del items[items.index(target):]
        for c in later:
            self._forget(c)
        self._save()
        return {"n": target["n"], "prompt": target["prompt"], "files": sorted(restored), "chat": chat}

    def forget_all(self) -> None:
        for c in self.items():
            self._forget(c)
        shutil.rmtree(self._dir(), ignore_errors=True)
        self._items = []

    # --- helpers -------------------------------------------------------------

    def _select(self, n):
        items = self.items()
        if not items:
            raise CheckpointError("nothing to undo yet")
        if n is None:
            return items[-1], items[-1:]
        for i, c in enumerate(items):
            if c["n"] == n:
                return c, items[i:]
        raise CheckpointError(f"no checkpoint #{n}; see /checkpoints")

    def _forget(self, c: dict) -> None:
        shutil.rmtree(self._dir() / f"turn-{c['n']}", ignore_errors=True)
        if c.get("tree"):
            try:
                self.shadow().drop(self._ref(c["n"]))
            except CheckpointError:
                pass

    def _ref(self, n: int) -> str:
        return f"refs/anvil/{_safe(self.sid)}/{n}"

    def _rel(self, key: str) -> str:
        try:
            return Path(key).resolve().relative_to(self.root).as_posix()
        except ValueError:
            return key


def forget_session(sid: str, root: str = "") -> None:
    """Delete a conversation's checkpoints (when the session itself is deleted)."""
    shutil.rmtree(CHECKPOINT_DIR / "sessions" / _safe(sid or "default"), ignore_errors=True)
    if root and shutil.which("git"):
        sh = Shadow(Path(root))
        if (sh.git_dir / "HEAD").exists():
            try:
                refs = sh.git("for-each-ref", "--format=%(refname)", f"refs/anvil/{_safe(sid)}/", check=False)
                for ref in refs.split():
                    sh.drop(ref)
            except CheckpointError:
                pass


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)[:100] or "_"
