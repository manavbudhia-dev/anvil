"""Plugins: installable bundles of skills, slash commands and connectors.

A plugin is a folder (usually a git repository) with a manifest and any of:

    plugin.json                  {"name", "description", "version", "author"}
                                 (.claude-plugin/plugin.json is read too, so Claude Code plugins work)
    skills/<name>/SKILL.md       skills, loaded like personal skills
    commands/<name>.md           slash commands (/name), handled like skills
    .mcp.json or mcp.json        {"mcpServers": {"name": {"command", "args", "env"} | {"type": "http", "url", "headers"}}}

${ANVIL_PLUGIN_ROOT} and ${CLAUDE_PLUGIN_ROOT} in connector settings expand to the plugin folder.
A repository with .claude-plugin/marketplace.json can hold several plugins; pick one by name.
"""

import json
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import CONFIG_DIR
from .fsutil import atomic_write_text, read_text

PLUGINS_DIR = CONFIG_DIR / "plugins"
STATE_FILE = CONFIG_DIR / "plugins.json"
MANIFESTS = ("plugin.json", "anvil-plugin.json", ".claude-plugin/plugin.json")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_state() -> dict:
    try:
        d = json.loads(read_text(STATE_FILE))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(d: dict) -> None:
    atomic_write_text(STATE_FILE, json.dumps(d, indent=2))


def read_manifest(folder: Path) -> dict | None:
    for m in MANIFESTS:
        p = folder / m
        if p.is_file():
            try:
                data = json.loads(read_text(p))
                return data if isinstance(data, dict) else None
            except (OSError, json.JSONDecodeError):
                return None
    return None


def _marketplace(folder: Path) -> list[dict]:
    p = folder / ".claude-plugin" / "marketplace.json"
    if not p.is_file():
        return []
    try:
        data = json.loads(read_text(p))
    except (OSError, json.JSONDecodeError):
        return []
    return [x for x in data.get("plugins", []) if isinstance(x, dict) and x.get("name")]


def _contents(folder: Path) -> dict:
    skills = sorted(p.parent.name for p in (folder / "skills").glob("*/SKILL.md")) if (folder / "skills").is_dir() else []
    commands = sorted(p.stem for p in (folder / "commands").glob("*.md")) if (folder / "commands").is_dir() else []
    servers = sorted(_mcp_config(folder))
    return {"skills": skills, "commands": commands, "connectors": servers}


def _mcp_config(folder: Path) -> dict:
    for name in (".mcp.json", "mcp.json"):
        p = folder / name
        if p.is_file():
            try:
                data = json.loads(read_text(p))
            except (OSError, json.JSONDecodeError):
                return {}
            servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
            return {k: v for k, v in servers.items() if isinstance(v, dict)}
    return {}


def installed() -> list[dict]:
    st = _load_state()
    out = []
    if PLUGINS_DIR.is_dir():
        for d in sorted(PLUGINS_DIR.iterdir()):
            if not d.is_dir():
                continue
            m = read_manifest(d) or {}
            info = st.get(d.name, {})
            author = m.get("author")
            out.append({"name": d.name, "description": m.get("description", ""), "version": m.get("version", ""),
                        "author": author.get("name", "") if isinstance(author, dict) else (author or ""),
                        "enabled": info.get("enabled", True), "source": info.get("source", ""),
                        "installed_at": info.get("installed_at", ""), "updated_at": info.get("updated_at", ""),
                        "path": str(d), **_contents(d)})
    return out


def enabled_dirs() -> list[Path]:
    st = _load_state()
    if not PLUGINS_DIR.is_dir():
        return []
    return [d for d in sorted(PLUGINS_DIR.iterdir()) if d.is_dir() and st.get(d.name, {}).get("enabled", True)]


def skill_dirs() -> list[tuple[Path, str]]:
    """(folder, source label) pairs for skills.discover."""
    out = []
    for d in enabled_dirs():
        for sub in ("skills", "commands"):
            if (d / sub).is_dir():
                out.append((d / sub, f"plugin:{d.name}"))
    return out


def _expand(value, root: Path):
    if isinstance(value, str):
        return value.replace("${ANVIL_PLUGIN_ROOT}", str(root)).replace("${CLAUDE_PLUGIN_ROOT}", str(root))
    if isinstance(value, list):
        return [_expand(v, root) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, root) for k, v in value.items()}
    return value


def mcp_servers() -> list[dict]:
    """Connector definitions from enabled plugins (see anvil/connectors.py)."""
    out = []
    for d in enabled_dirs():
        for name, cfg in _mcp_config(d).items():
            cfg = _expand(cfg, d)
            cid = re.sub(r"[^a-z0-9-]+", "-", f"{d.name}-{name}".lower()).strip("-")
            kind = cfg.get("type") or ("http" if cfg.get("url") else "stdio")
            conn = {"id": cid, "name": f"{name} ({d.name})", "source": f"plugin:{d.name}", "preset": "",
                    "auth": "oauth" if kind in ("http", "sse", "streamable-http") else "none"}
            if kind in ("http", "sse", "streamable-http"):
                conn.update(transport="http", url=cfg.get("url", ""), headers=cfg.get("headers") or {})
            else:
                conn.update(transport="stdio", command=cfg.get("command", ""), args=cfg.get("args") or [],
                            env=cfg.get("env") or {}, cwd=str(d))
            out.append(conn)
    return out


def _clone(url: str, dest: Path) -> None:
    proc = subprocess.run(["git", "clone", "--depth", "1", "--", url, str(dest)], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git clone failed")


def install(source: str, plugin: str = "") -> dict:
    """Install from a git URL or a local folder. For a marketplace repository, `plugin` picks one."""
    source = source.strip()
    if not source or source.startswith("-") or any(c in source for c in "\n\r\0"):
        raise RuntimeError("enter a git URL or a folder path")
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="anvil-plugin-") as tmp:
        local = Path(source).expanduser()
        if local.is_dir():
            src = local
        else:
            src = Path(tmp) / "repo"
            _clone(source, src)
        if not read_manifest(src):
            market = _marketplace(src)
            if not market:
                if not (src / "skills").is_dir() and not (src / "commands").is_dir() and not _mcp_config(src):
                    raise RuntimeError("no plugin.json, skills/, commands/ or .mcp.json found there")
            else:
                names = [m["name"] for m in market]
                if not plugin:
                    raise RuntimeError("this repository holds several plugins; choose one: " + ", ".join(names))
                entry = next((m for m in market if m["name"] == plugin), None)
                if not entry:
                    raise RuntimeError(f"no plugin named {plugin!r}; choose one of: {', '.join(names)}")
                rel = entry.get("source")
                if not isinstance(rel, str) or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
                    raise RuntimeError("only plugins stored inside the same repository can be installed")
                src = (src / rel).resolve()
                if not src.is_dir():
                    raise RuntimeError(f"plugin folder {rel} is missing")
        m = read_manifest(src) or {}
        name = str(m.get("name") or plugin or src.name).lower()
        name = re.sub(r"\.git$", "", name)
        if not NAME_RE.match(name):
            raise RuntimeError(f"bad plugin name {name!r}")
        dest = PLUGINS_DIR / name
        if dest.exists():
            raise RuntimeError(f"plugin {name!r} is already installed; update or remove it first")
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"))
    with _LOCK:
        st = _load_state()
        st[name] = {"enabled": True, "source": source, "plugin": plugin, "installed_at": _now(), "updated_at": _now()}
        _save_state(st)
    return next(p for p in installed() if p["name"] == name)


def update(name: str) -> dict:
    st = _load_state()
    info = st.get(name)
    if not info or not info.get("source"):
        raise RuntimeError("this plugin wasn't installed from a source Anvil can update from")
    dest = PLUGINS_DIR / name
    backup = dest.with_name(f".{name}.old")
    if backup.exists():
        shutil.rmtree(backup)
    dest.rename(backup)
    try:
        install(info["source"], info.get("plugin", ""))
    except Exception:
        if dest.exists():
            shutil.rmtree(dest)
        backup.rename(dest)
        raise
    shutil.rmtree(backup, ignore_errors=True)
    with _LOCK:
        st = _load_state()
        st[name] = {**info, "enabled": info.get("enabled", True), "updated_at": _now()}
        _save_state(st)
    return next(p for p in installed() if p["name"] == name)


def set_enabled(name: str, enabled: bool) -> None:
    if not (PLUGINS_DIR / name).is_dir():
        raise RuntimeError(f"no plugin named {name!r}")
    with _LOCK:
        st = _load_state()
        st.setdefault(name, {})["enabled"] = bool(enabled)
        st[name]["updated_at"] = _now()
        _save_state(st)


def remove(name: str) -> None:
    if not NAME_RE.match(name):
        raise RuntimeError("bad plugin name")
    dest = PLUGINS_DIR / name
    if dest.is_dir():
        shutil.rmtree(dest)
    with _LOCK:
        st = _load_state()
        st.pop(name, None)
        _save_state(st)
