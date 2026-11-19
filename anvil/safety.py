"""Guards for running an agent on a real server.

- dangerous_command(): reasons a shell command needs a human's explicit yes, every time.
- protected_path(): files whose edits always need a human's yes.
- load_policy(): a root-owned policy file that caps the permission mode.
- backup_file(): copies a file aside before the agent changes it.
"""

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from .config import CONFIG_DIR

POLICY_FILE = Path(os.environ.get("ANVIL_POLICY_FILE", "/etc/anvil/policy.json"))
BACKUP_DIR = CONFIG_DIR / "backups"
MODES = ("ask", "edits", "auto")  # least to most permissive

# (pattern, reason). Patterns run on each command segment, case-insensitive.
_RULES = [
    (r"\brm\s+(-[a-z]*[rf]|--recursive|--force)", "deletes files recursively or forcefully (rm -r/-f)"),
    (r"\bfind\b.*\s-(delete|exec\s+rm)\b", "deletes files found by find"),
    (r"\b(shred|wipefs|mkfs(\.\w+)?|fdisk|sfdisk|parted)\b", "destroys disks or filesystems"),
    (r"\bdd\b.*\bof=", "writes raw data with dd"),
    (r"\b(ufw|firewall-cmd|iptables|ip6tables|nft|iptables-restore)\b", "changes the firewall"),
    (r"\boci\s+network\b", "changes Oracle Cloud networking"),
    (r"\b(shutdown|reboot|poweroff|halt)\b|\binit\s+[06]\b", "shuts down or reboots the server"),
    (r"\bsystemctl\s+(stop|disable|mask|kill|isolate)\b", "stops or disables a service"),
    (r"\b(userdel|usermod|groupdel|passwd|chpasswd|visudo)\b", "changes users or passwords"),
    (r"\bchmod\s+(-[a-z]*r|--recursive)|\bchown\s+(-[a-z]*r|--recursive)|\bchmod\s+[0-7]?777\b", "changes permissions recursively or world-writable"),
    (r"\bgit\s+push\b.*(\s-f\b|--force)|\bgit\s+reset\s+--hard\b|\bgit\s+clean\s+-[a-z]*f|\bgit\s+branch\s+-D\b", "rewrites or discards git history/work"),
    (r"\b(drop\s+(table|database|schema)|truncate\s+table)\b|\bdropdb\b", "deletes database data"),
    (r"\bdocker\s+(rm|rmi|system\s+prune|volume\s+(rm|prune)|network\s+rm)\b|\bdocker\s+compose\s+down\b.*\s-v\b", "deletes containers, images or volumes"),
    (r"\b(kill\s+-9|killall|pkill)\b", "force-kills processes"),
    (r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b", "pipes a downloaded script into a shell"),
    (r"\b(apt(-get)?|dnf|yum|apk)\s+(remove|purge|autoremove|erase|del)\b", "uninstalls packages"),
    (r"\bcrontab\s+-r\b", "deletes the crontab"),
    (r"(^|[^>0-9&])>>?\s*/(etc|boot|usr|var/lib)/", "overwrites a system file with a redirect"),
    (r"\btee\s+(-a\s+)?/(etc|boot)/", "writes a system file with tee"),
    (r"\bsed\s+-i\b.*\s/(etc|boot)/", "edits a system file in place"),
    (r"\.ssh/(authorized_keys|config)|/etc/ssh/", "changes SSH access"),
    (r"(\.config/anvil|/etc/anvil)", "changes Anvil's own settings or safety policy"),
    (r"/etc/sudoers", "changes sudo rules"),
]
_COMPILED = [(re.compile(p, re.I), r) for p, r in _RULES]

_RELOAD_RULES = [
    (re.compile(r"\bcaddy\s+reload\b|\bsystemctl\s+(reload|restart)\s+caddy\b", re.I),
     re.compile(r"\bcaddy\s+validate\b|anvil-caddy-apply", re.I),
     "reloads Caddy without running caddy validate first (use: sudo anvil-caddy-apply apply <file>)"),
    (re.compile(r"\bnginx\s+-s\s+reload\b|\bsystemctl\s+(reload|restart)\s+nginx\b", re.I),
     re.compile(r"\bnginx\s+-t\b", re.I),
     "reloads nginx without running nginx -t first"),
]


def dangerous_command(command: str) -> list[str]:
    reasons: list[str] = []
    for seg in re.split(r"\s*(?:&&|\|\||;|\n)\s*", command):
        for rx, reason in _COMPILED:
            if rx.search(seg) and reason not in reasons:
                reasons.append(reason)
    for action, check, reason in _RELOAD_RULES:
        if action.search(command) and not check.search(command):
            reasons.append(reason)
    return reasons


_PROTECTED = [
    (re.compile(r"^/etc/(sudoers|ssh/|anvil/|passwd|shadow|group|fstab|hosts)"), "a core system file"),
    (re.compile(r"/\.ssh/"), "SSH keys or config"),
    (re.compile(r"/\.config/anvil/"), "Anvil's own settings"),
    (re.compile(r"^/etc/caddy/"), "the live Caddy config (edit a copy, then: sudo anvil-caddy-apply apply <file>)"),
    (re.compile(r"/etc/(nginx|systemd|cron)"), "a server config file"),
]


def protected_path(path: Path) -> str | None:
    p = str(path)
    for rx, what in _PROTECTED:
        if rx.search(p):
            return f"edits {what}"
    return None


BACKUP_NAME_RE = re.compile(r"(Caddyfile|\.conf$|\.service$|\.timer$|\.env$|\.ya?ml$|\.toml$|\.ini$|nginx)", re.I)


def should_back_up(path: Path, inside_project: bool) -> bool:
    return path.is_file() and (not inside_project or bool(BACKUP_NAME_RE.search(path.name)))


def backup_file(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = BACKUP_DIR / stamp / str(path.resolve()).lstrip("/\\").replace(":", "")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    _prune()
    return dest


def _prune(keep: int = 300) -> None:
    try:
        dirs = sorted(d for d in BACKUP_DIR.iterdir() if d.is_dir())
        for d in dirs[:-keep]:
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def load_policy() -> dict:
    """System-wide limits set by the server admin. The agent's user can't write this file."""
    try:
        data = json.loads(POLICY_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def mode_ceiling(config_ceiling: str = "") -> str:
    """The most permissive mode allowed: the stricter of the policy file and the user's config."""
    candidates = [m for m in (load_policy().get("permission_ceiling"), config_ceiling,
                              os.environ.get("ANVIL_PERMISSION_CEILING")) if m in MODES]
    return min(candidates, key=MODES.index) if candidates else "auto"


def allowed(mode: str, ceiling: str) -> bool:
    return MODES.index(mode) <= MODES.index(ceiling)
