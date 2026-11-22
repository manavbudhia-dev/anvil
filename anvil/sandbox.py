"""Optional sandbox for the bash tool.

When it is on, commands run with the project folder (and the temp dir) writable, the rest of the
filesystem read-only, and secrets (SSH keys, cloud credentials, Anvil's own settings) hidden.
Network access can be turned off too. Permission prompts still apply as usual: the sandbox is an
extra layer, not a replacement.

Backends, tried in this order by mode "auto":
- Linux:   bubblewrap (bwrap), firejail, then Docker/Podman
- macOS:   sandbox-exec (built in), then Docker/Podman
- Windows: Docker/Podman

Settings (later ones win, except where noted):
1. ANVIL_SANDBOX env var: a mode name.
2. "sandbox" in ~/.config/anvil/config.json: {"mode": "auto", "network": true, "writable": [...], "image": "..."}
3. "sandbox" in <project>/.anvil/settings.json: same keys. A project can turn the sandbox on or
   make it stricter (network off), but it can't turn off a sandbox your own config turned on, and
   its extra writable paths must be inside the project or the temp dir.
4. "sandbox" in the admin's policy file (/etc/anvil/policy.json): always wins. {"required": true}
   refuses to run commands when no backend works, instead of running them unsandboxed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import CONFIG_DIR, CONFIG_FILE

BACKENDS = ("bwrap", "firejail", "sandbox-exec", "docker", "podman")
MODES = ("off", "auto") + BACKENDS
DEFAULT_IMAGE = "python:3.12-slim"
PROJECT_SETTINGS = Path(".anvil") / "settings.json"

# Hidden inside the sandbox when they exist.
SECRET_DIRS = [".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".config/gcloud"]
SECRET_FILES = [".netrc", ".git-credentials", ".pypirc", ".npmrc"]

_probe_cache: dict[str, bool] = {}


class SandboxUnavailable(RuntimeError):
    pass


@dataclass
class SandboxSettings:
    mode: str = "off"
    network: bool = True
    writable: list[str] = field(default_factory=list)
    image: str = DEFAULT_IMAGE
    required: bool = False
    sources: list[str] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.mode != "off" or self.required


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _section(data: dict) -> dict:
    s = data.get("sandbox")
    if isinstance(s, str):
        return {"required": True} if s == "required" else {"mode": s}
    if isinstance(s, bool):
        return {"mode": "auto" if s else "off"}
    return s if isinstance(s, dict) else {}


def _inside(path: Path, base: Path) -> bool:
    try:
        return path.resolve().is_relative_to(base.resolve())
    except (OSError, ValueError):
        return False


def load_settings(root: Path, config_sandbox: dict | None = None) -> SandboxSettings:
    """The sandbox settings for a project. config_sandbox: the "sandbox" value from the user's
    config (read from config.json when not given)."""
    from .safety import load_policy  # imported here: safety imports config, not sandbox

    s = SandboxSettings()
    env_mode = os.environ.get("ANVIL_SANDBOX", "").strip().lower()
    if env_mode in MODES:
        s.mode = env_mode
        s.sources.append("ANVIL_SANDBOX")

    user = _section({"sandbox": config_sandbox}) if config_sandbox is not None else _section(_read_json(CONFIG_FILE))
    _apply(s, user, trusted=True)
    if user:
        s.sources.append(str(CONFIG_FILE))
    user_on = s.mode != "off"

    project_file = Path(root) / PROJECT_SETTINGS
    proj = _section(_read_json(project_file))
    if proj:
        if proj.get("mode") == "off" and user_on:
            proj = {k: v for k, v in proj.items() if k != "mode"}
        if proj.get("network") is True and not s.network:
            proj.pop("network")
        extra = [w for w in proj.get("writable", []) or [] if isinstance(w, str)]
        proj["writable"] = [w for w in extra if _inside(Path(root) / os.path.expanduser(w), Path(root))
                            or _inside(Path(os.path.expanduser(w)), Path(tempfile.gettempdir()))]
        proj.pop("required", None)
        _apply(s, proj, trusted=False)
        s.sources.append(str(project_file))

    pol = _section(load_policy())
    if pol:
        _apply(s, pol, trusted=True)
        if pol.get("required"):
            s.required = True
        s.sources.append("policy file")
    if s.required and s.mode == "off":
        s.mode = "auto"
    return s


def _apply(s: SandboxSettings, d: dict, trusted: bool) -> None:
    mode = str(d.get("mode", "")).lower()
    if mode in MODES:
        s.mode = mode
    if isinstance(d.get("network"), bool):
        s.network = d["network"]
    if isinstance(d.get("writable"), list):
        s.writable += [w for w in d["writable"] if isinstance(w, str)]
    if trusted and isinstance(d.get("image"), str) and d["image"]:
        s.image = d["image"]
    if trusted and d.get("required") is True:
        s.required = True


# --- backends ---------------------------------------------------------------------

def _run_ok(argv: list[str], timeout: int = 15) -> bool:
    try:
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, **kw).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def backend_works(name: str) -> bool:
    """Whether a backend is installed and actually runs here (cached)."""
    if name in _probe_cache:
        return _probe_cache[name]
    exe = shutil.which(name)
    ok = False
    if exe:
        if name == "bwrap":
            ok = _run_ok([exe, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-all",
                          "--die-with-parent", "true"])
        elif name == "firejail":
            ok = _run_ok([exe, "--quiet", "--noprofile", "true"])
        elif name == "sandbox-exec":
            ok = _run_ok([exe, "-p", "(version 1)(allow default)", "/usr/bin/true"])
        elif name in ("docker", "podman"):
            ok = _run_ok([exe, "info", "--format", "{{.ID}}"], timeout=20)
    _probe_cache[name] = ok
    return ok


def candidates() -> list[str]:
    if sys.platform.startswith("linux"):
        return ["bwrap", "firejail", "docker", "podman"]
    if sys.platform == "darwin":
        return ["sandbox-exec", "docker", "podman"]
    return ["docker", "podman"]


def choose_backend(settings: SandboxSettings) -> str | None:
    if not settings.enabled:
        return None
    if settings.mode in BACKENDS:
        return settings.mode if backend_works(settings.mode) else None
    for name in candidates():
        if backend_works(name):
            return name
    return None


def _shell() -> str:
    return shutil.which("bash") or "/bin/sh"


def _writable_paths(root: Path, settings: SandboxSettings) -> list[str]:
    out = [str(root)]
    for w in settings.writable:
        p = Path(os.path.expanduser(w))
        p = p if p.is_absolute() else root / p
        if p.exists():
            out.append(str(p.resolve()))
    return out


def _secrets(home: Path) -> tuple[list[str], list[str]]:
    dirs = [str(home / d) for d in SECRET_DIRS if (home / d).is_dir()]
    if CONFIG_DIR.is_dir():
        dirs.append(str(CONFIG_DIR.resolve()))
    files = [str(home / f) for f in SECRET_FILES if (home / f).is_file()]
    return dirs, files


def _project_tmp(root: Path) -> Path:
    tag = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    uid = os.getuid() if hasattr(os, "getuid") else 0
    p = Path(tempfile.gettempdir()) / f"anvil-sandbox-{uid}-{tag}"
    p.mkdir(mode=0o700, exist_ok=True)
    return p


def bwrap_argv(command: str, root: Path, settings: SandboxSettings) -> list[str]:
    home = Path.home()
    secret_dirs, secret_files = _secrets(home)
    argv = [shutil.which("bwrap") or "bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--bind", str(_project_tmp(root)), "/tmp"]
    for d in secret_dirs:
        if not _inside(root, Path(d)):
            argv += ["--tmpfs", d]
    for f in secret_files:
        argv += ["--ro-bind", "/dev/null", f]
    for w in _writable_paths(root, settings):
        argv += ["--bind", w, w]
    argv += ["--unshare-all"] + ([] if not settings.network else ["--share-net"])
    argv += ["--die-with-parent", "--new-session", "--chdir", str(root), "--setenv", "ANVIL_SANDBOX", "bwrap",
             "--", _shell(), "-c", command]
    return argv


def firejail_argv(command: str, root: Path, settings: SandboxSettings) -> list[str]:
    home = Path.home()
    secret_dirs, secret_files = _secrets(home)
    argv = [shutil.which("firejail") or "firejail", "--quiet", "--noprofile", "--read-only=/"]
    for w in _writable_paths(root, settings) + [tempfile.gettempdir(), "/tmp", "/dev/shm"]:
        argv.append(f"--read-write={w}")
    for p in secret_dirs + secret_files:
        if not _inside(root, Path(p)):
            argv.append(f"--blacklist={p}")
    if not settings.network:
        argv.append("--net=none")
    argv += ["--env=ANVIL_SANDBOX=firejail", "--", _shell(), "-c", command]
    return argv


def _sbpl_str(p: str) -> str:
    return '"' + p.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_exec_profile(root: Path, settings: SandboxSettings) -> str:
    home = Path.home()
    secret_dirs, secret_files = _secrets(home)
    writable = [os.path.realpath(w) for w in _writable_paths(root, settings)]
    writable += [os.path.realpath(tempfile.gettempdir()), "/private/tmp", "/private/var/folders"]
    lines = ["(version 1)", "(allow default)", "(deny file-write*)",
             "(allow file-write* " + " ".join(f"(subpath {_sbpl_str(w)})" for w in writable)
             + ' (literal "/dev/null") (literal "/dev/zero") (literal "/dev/tty") (literal "/dev/dtracehelper")'
             + ' (regex #"^/dev/fd/") (regex #"^/dev/ttys"))']
    hidden = [f"(subpath {_sbpl_str(os.path.realpath(d))})" for d in secret_dirs if not _inside(root, Path(d))]
    hidden += [f"(literal {_sbpl_str(os.path.realpath(f))})" for f in secret_files]
    if hidden:
        lines.append("(deny file-read* file-write* " + " ".join(hidden) + ")")
    if not settings.network:
        lines.append("(deny network-outbound (remote ip))")
        lines.append("(deny network-inbound (local ip))")
    return "\n".join(lines)


def sandbox_exec_argv(command: str, root: Path, settings: SandboxSettings) -> list[str]:
    return ["/usr/bin/sandbox-exec", "-p", sandbox_exec_profile(root, settings), "/usr/bin/env",
            "ANVIL_SANDBOX=sandbox-exec", _shell(), "-c", command]


_CONTAINER_SH = 'if command -v bash >/dev/null 2>&1; then exec bash -c "$1"; else exec sh -c "$1"; fi'


def docker_argv(command: str, root: Path, settings: SandboxSettings, engine: str = "docker") -> list[str]:
    windows = os.name == "nt"
    work = "/work" if windows else str(root)
    argv = [shutil.which(engine) or engine, "run", "--rm", "-i", "--init", "-v", f"{root}:{work}", "-w", work,
            "--security-opt", "no-new-privileges", "--cap-drop", "ALL", "--pids-limit", "1024",
            "-e", "HOME=/tmp", "-e", f"ANVIL_SANDBOX={engine}"]
    if hasattr(os, "getuid"):
        argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
    for w in _writable_paths(root, settings)[1:]:
        if not windows:
            argv += ["-v", f"{w}:{w}"]
    if not settings.network:
        argv += ["--network", "none"]
    argv += [settings.image, "sh", "-c", _CONTAINER_SH, "anvil-sandbox", command]
    return argv


def wrap(command: str, root: Path, settings: SandboxSettings | None = None) -> list[str] | None:
    """The argv to run command inside the sandbox, or None when the sandbox is off.
    Raises SandboxUnavailable when the sandbox is required (or a specific backend was asked for)
    but can't run here."""
    root = Path(root).resolve()
    settings = settings or load_settings(root)
    if not settings.enabled:
        return None
    backend = choose_backend(settings)
    if backend is None:
        if settings.required or settings.mode in BACKENDS:
            want = settings.mode if settings.mode in BACKENDS else " or ".join(candidates())
            raise SandboxUnavailable(
                f"the sandbox is {'required' if settings.required else 'set to ' + settings.mode} but {want} "
                f"isn't available here. Install one, or change \"sandbox\" in your Anvil settings.")
        return None  # "auto" with nothing available: run unsandboxed (anvil --doctor warns)
    if backend == "bwrap":
        return bwrap_argv(command, root, settings)
    if backend == "firejail":
        return firejail_argv(command, root, settings)
    if backend == "sandbox-exec":
        return sandbox_exec_argv(command, root, settings)
    return docker_argv(command, root, settings, engine=backend)


def describe(root: Path) -> tuple[str, str]:
    """(status, detail) for anvil --doctor: status is PASS, WARN, FAIL or SKIP."""
    settings = load_settings(Path(root))
    if not settings.enabled:
        return "SKIP", "off (optional: set \"sandbox\": {\"mode\": \"auto\"} in your settings)"
    backend = choose_backend(settings)
    net = "network on" if settings.network else "network off"
    if backend:
        return "PASS", f"{backend} · {net} · writable: project folder" + (" + extras" if settings.writable else "")
    if settings.required or settings.mode in BACKENDS:
        return "FAIL", f"{settings.mode} requested but no working backend (tried: {', '.join(candidates())})"
    return "WARN", f"auto, but no backend works here (tried: {', '.join(candidates())}); commands run unsandboxed"
