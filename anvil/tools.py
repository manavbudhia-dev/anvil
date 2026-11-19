import difflib
import fnmatch
import os
import re
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

MAX_OUTPUT_CHARS = 30_000
# Keep child processes from flashing console windows when running as a windowed .exe.
NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache", ".pytest_cache"}


def _kill_tree(proc: subprocess.Popen) -> None:
    """Stop a command and everything it started."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, **NO_WINDOW)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def shell_executable() -> str | None:
    """bash where available (Linux, macOS, Git Bash on Windows); otherwise the OS default shell."""
    return shutil.which("bash") if os.name != "nt" else None


def shell_name() -> str:
    return "bash" if shell_executable() else ("cmd.exe" if os.name == "nt" else "sh")


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


TOOL_DEFS = [
    {
        "name": "read_file",
        "description": "Read a text file. Returns lines prefixed with line numbers. Use offset/limit for large files.",
        "input_schema": _schema({
            "path": {"type": "string", "description": "File path, relative to the project root or absolute"},
            "offset": {"type": "integer", "description": "1-based line to start at"},
            "limit": {"type": "integer", "description": "Max lines to return (default 2000)"},
        }, ["path"]),
    },
    {
        "name": "write_file",
        "description": "Create a new file or fully overwrite an existing one. Prefer edit_file for changes to existing files.",
        "input_schema": _schema({
            "path": {"type": "string"},
            "content": {"type": "string", "description": "Full file contents"},
        }, ["path", "content"]),
    },
    {
        "name": "edit_file",
        "description": "Replace an exact string in a file. old_string must match exactly (including whitespace) and be unique unless replace_all is true. Read the file first.",
        "input_schema": _schema({
            "path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
        }, ["path", "old_string", "new_string"]),
    },
    {
        "name": "bash",
        "description": "Run a shell command in the project root and return stdout/stderr and exit code. Use for builds, tests, git, installing packages. Avoid interactive commands.",
        "input_schema": _schema({
            "command": {"type": "string"},
            "timeout": {"type": "integer", "description": "Seconds before the command is killed (default 120, max 600)"},
        }, ["command"]),
    },
    {
        "name": "glob",
        "description": "Find files by glob pattern, e.g. '**/*.py' or 'src/**/test_*.ts'.",
        "input_schema": _schema({
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "Directory to search (default project root)"},
        }, ["pattern"]),
    },
    {
        "name": "grep",
        "description": "Search file contents with a regular expression. Returns matching lines as path:line:text.",
        "input_schema": _schema({
            "pattern": {"type": "string"},
            "path": {"type": "string", "description": "File or directory (default project root)"},
            "glob": {"type": "string", "description": "Only search files matching this glob, e.g. '*.py'"},
            "ignore_case": {"type": "boolean"},
        }, ["pattern"]),
    },
    {
        "name": "load_skill",
        "description": "Load a skill's full instructions by name. Skills are listed in the system prompt; load one when the task matches its description, then follow it.",
        "input_schema": _schema({"name": {"type": "string"}}, ["name"]),
    },
    {
        "name": "list_dir",
        "description": "List entries in a directory (directories end with /).",
        "input_schema": _schema({"path": {"type": "string"}}, []),
    },
]

for _t in TOOL_DEFS:
    _t["eager_input_streaming"] = True

TOOL_NAMES = {t["name"] for t in TOOL_DEFS}
MUTATING_TOOLS = {"write_file", "edit_file", "bash"}

_TYPES = {"string": str, "integer": int, "boolean": bool}


def validate_input(name: str, args) -> str | None:
    """Return an error string if args don't satisfy the tool's schema."""
    from .browser import BROWSER_TOOL_DEFS

    tool = next((t for t in TOOL_DEFS + BROWSER_TOOL_DEFS if t["name"] == name), None)
    if tool is None:
        return f"unknown tool {name!r}"
    if not isinstance(args, dict):
        return "input is not a JSON object"
    schema = tool["input_schema"]
    for req in schema["required"]:
        if req not in args:
            return f"missing required field {req!r}"
    for key, val in args.items():
        spec = schema["properties"].get(key)
        if spec is None:
            return f"unexpected field {key!r}"
        expected = _TYPES[spec["type"]]
        if not isinstance(val, expected) or (expected is int and isinstance(val, bool)):
            return f"field {key!r} must be {spec['type']}"
        if "enum" in spec and val not in spec["enum"]:
            return f"field {key!r} must be one of {', '.join(spec['enum'])}"
    return None


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return f"{text[:half]}\n\n... [{len(text) - MAX_OUTPUT_CHARS} chars truncated] ...\n\n{text[-half:]}"


class ToolError(Exception):
    pass


class Tools:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def resolve(self, p: str | None) -> Path:
        path = Path(os.path.expanduser(p or "."))
        if not path.is_absolute():
            path = self.root / path
        return path.resolve()

    def is_inside_root(self, p: str | None) -> bool:
        return self.resolve(p).is_relative_to(self.root)

    def preview(self, name: str, args: dict) -> str:
        """Human-readable preview of what a mutating tool will do."""
        if name == "bash":
            return f"$ {args['command']}"
        if name == "write_file":
            path = self.resolve(args["path"])
            old = path.read_text(errors="replace") if path.is_file() else ""
            return _diff(old, args["content"], self._rel(path)) or f"(no changes to {self._rel(path)})"
        if name == "edit_file":
            path = self.resolve(args["path"])
            try:
                old = path.read_text(errors="replace")
                new = self._apply_edit(old, args)
            except (OSError, ToolError) as e:
                return f"edit {self._rel(path)}: {e}"
            return _diff(old, new, self._rel(path))
        return ""

    def run(self, name: str, args: dict) -> str:
        fn = getattr(self, f"_t_{name}", None)
        if fn is None:
            raise ToolError(f"unknown tool {name}")
        return _truncate(fn(**args))

    # --- implementations ---------------------------------------------------

    def _t_read_file(self, path: str, offset: int = 1, limit: int = 2000) -> str:
        p = self.resolve(path)
        if not p.exists():
            raise ToolError(f"file not found: {p}")
        if p.is_dir():
            raise ToolError(f"{p} is a directory; use list_dir")
        lines = p.read_text(errors="replace").splitlines()
        start = max(offset, 1) - 1
        chunk = lines[start:start + max(limit, 1)]
        if not chunk:
            return f"(file has {len(lines)} lines; nothing at offset {offset})"
        body = "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(chunk, start + 1))
        if start + len(chunk) < len(lines):
            body += f"\n... ({len(lines) - start - len(chunk)} more lines; use offset to read more)"
        return body

    def _backup(self, p: Path) -> str:
        from .safety import backup_file, should_back_up

        if should_back_up(p, p.is_relative_to(self.root)):
            return f" (backup of the previous version: {backup_file(p)})"
        return ""

    def _t_write_file(self, path: str, content: str) -> str:
        p = self.resolve(path)
        existed = p.exists()
        note = self._backup(p) if existed else ""
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"{'Overwrote' if existed else 'Created'} {self._rel(p)} ({len(content.splitlines())} lines){note}"

    @staticmethod
    def _apply_edit(text: str, args: dict) -> str:
        old, new = args["old_string"], args["new_string"]
        if old == new:
            raise ToolError("old_string and new_string are identical")
        count = text.count(old)
        if count == 0:
            raise ToolError("old_string not found in file (it must match exactly, including whitespace)")
        if count > 1 and not args.get("replace_all"):
            raise ToolError(f"old_string occurs {count} times; add surrounding context to make it unique, or set replace_all")
        return text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)

    def _t_edit_file(self, path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
        p = self.resolve(path)
        if not p.is_file():
            raise ToolError(f"file not found: {p}")
        text = p.read_text()
        new = self._apply_edit(text, {"old_string": old_string, "new_string": new_string, "replace_all": replace_all})
        note = self._backup(p)
        p.write_text(new)
        return f"Edited {self._rel(p)}{note}"

    def _t_bash(self, command: str, timeout: int = 120) -> str:
        timeout = min(max(timeout, 1), 600)
        # Output goes to temp files, not pipes: with pipes, a background process (`npm run dev &`)
        # keeps the pipe open and the call blocks until the timeout although the shell has exited.
        # The command gets its own process group so a timeout or Ctrl+C can stop its children too.
        if os.name == "nt":
            group = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
        else:
            group = {"start_new_session": True}
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(command, shell=True, cwd=self.root, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err, executable=shell_executable(), **group)
            timed_out = False
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_tree(proc)
                code = None
            except BaseException:  # Ctrl+C / Stop: don't leave the command running
                _kill_tree(proc)
                raise
            out.seek(0)
            err.seek(0)
            stdout = out.read().decode(errors="replace")
            stderr = err.read().decode(errors="replace")
        parts = []
        if stdout.strip():
            parts.append(stdout.rstrip())
        if stderr.strip():
            parts.append(f"[stderr]\n{stderr.rstrip()}")
        parts.append(f"[command timed out after {timeout}s and was stopped]" if timed_out else f"[exit code {code}]")
        return "\n".join(parts)

    def _walk(self, base: Path):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
            for f in sorted(filenames):
                yield Path(dirpath) / f

    def _rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.root))
        except ValueError:
            return str(p)

    def _t_glob(self, pattern: str, path: str | None = None) -> str:
        base = self.resolve(path)
        matches = [
            m for m in base.glob(pattern)
            if m.is_file() and not any(part in IGNORED_DIRS for part in m.relative_to(base).parts)
        ]
        matches.sort(key=lambda m: m.stat().st_mtime, reverse=True)
        if not matches:
            return "No files matched."
        shown = matches[:500]
        out = "\n".join(self._rel(m) for m in shown)
        if len(matches) > len(shown):
            out += f"\n... and {len(matches) - len(shown)} more"
        return out

    def _t_grep(self, pattern: str, path: str | None = None, glob: str | None = None, ignore_case: bool = False) -> str:
        base = self.resolve(path)
        rg = shutil.which("rg")
        if rg:
            cmd = [rg, "--line-number", "--no-heading", "--color=never", "--max-columns=300"]
            if ignore_case:
                cmd.append("-i")
            if glob:
                cmd += ["--glob", glob]
            cmd += ["--", pattern, str(base)]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, **NO_WINDOW)
            if proc.returncode == 2:
                raise ToolError(proc.stderr.strip() or "rg failed")
            lines = proc.stdout.splitlines()
            lines = [ln.replace(str(self.root) + os.sep, "", 1) for ln in lines]
        else:
            try:
                rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
            except re.error as e:
                raise ToolError(f"bad regex: {e}")
            files = [base] if base.is_file() else self._walk(base)
            lines = []
            for f in files:
                if glob and not fnmatch.fnmatch(f.name, glob):
                    continue
                try:
                    for i, line in enumerate(f.read_text(errors="strict").splitlines(), 1):
                        if rx.search(line):
                            lines.append(f"{self._rel(f)}:{i}:{line[:300]}")
                except (UnicodeDecodeError, OSError):
                    continue
        if not lines:
            return "No matches."
        out = "\n".join(lines[:300])
        if len(lines) > 300:
            out += f"\n... and {len(lines) - 300} more matches"
        return out

    def _t_load_skill(self, name: str) -> str:
        from .skills import discover, render_for_model

        skills = discover(self.root)
        skill = skills.get(name.strip().lstrip("/").lower())
        if not skill:
            raise ToolError(f"no skill named {name!r}. Available: {', '.join(skills) or 'none'}")
        return render_for_model(skill)

    def _t_list_dir(self, path: str | None = None) -> str:
        p = self.resolve(path)
        if not p.is_dir():
            raise ToolError(f"not a directory: {p}")
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        return "\n".join(e.name + ("/" if e.is_dir() else "") for e in entries) or "(empty)"


def _diff(old: str, new: str, label: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{label}", tofile=f"b/{label}", n=3,
    ))
