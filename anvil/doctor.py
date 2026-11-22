"""`anvil --doctor`: check a real setup end to end and explain anything that doesn't work."""

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import anthropic
import openai

from .agent import build_system_prompt
from .backends import make_backend
from .config import CONFIG_FILE, SESSIONS_DIR, Config, cost_usd
from .tools import TOOL_DEFS


@dataclass
class Check:
    name: str
    status: str  # PASS | WARN | FAIL | SKIP
    detail: str
    hint: str = ""


class _QuietUI:
    def __init__(self):
        self.notices: list[str] = []

    def notice(self, msg):
        self.notices.append(msg)

    def __getattr__(self, name):
        return lambda *a, **k: None


TOOL_PROMPT = ("This is an automated connection check. Call the list_dir tool with path \".\" exactly once. "
               "Do not reply with text.")


def _api_error(e: Exception, config: Config) -> tuple[str, str]:
    endpoint = config.resolved_base_url() or "api.anthropic.com"
    if isinstance(e, (anthropic.AuthenticationError, openai.AuthenticationError)):
        return "the endpoint rejected the API key", "Check the key: anvil --setup"
    if isinstance(e, (anthropic.NotFoundError, openai.NotFoundError)):
        return f"not found: {getattr(e, 'message', e)}", f"Check the model name ({config.model}) and base URL ({endpoint})"
    if isinstance(e, (anthropic.APIConnectionError, openai.APIConnectionError)):
        return f"couldn't reach {endpoint}", "Check the base URL, your internet connection and any VPN/firewall"
    if isinstance(e, (anthropic.RateLimitError, openai.RateLimitError)):
        return "rate limited", "Wait a minute and run the doctor again"
    if isinstance(e, (anthropic.APIStatusError, openai.APIStatusError)):
        return f"HTTP {e.status_code}: {getattr(e, 'message', e)}", "Paste this message to support or into an Anvil issue"
    return f"{type(e).__name__}: {e}", ""


def run_checks(config: Config, root: Path, include_browser: bool = True) -> tuple[list[Check], float]:
    checks: list[Check] = []
    spent = 0.0

    # 1. Settings
    provider = config.resolved_provider()
    endpoint = config.resolved_base_url() or "api.anthropic.com (official)"
    if not config.resolved_key():
        checks.append(Check("Settings", "FAIL", "no API key configured", "Run: anvil --setup"))
        return checks, spent
    checks.append(Check("Settings", "PASS", f"{provider} format · {endpoint} · model {config.model}"))

    ui = _QuietUI()
    backend = make_backend(config, ui)
    system = build_system_prompt(root)
    tools = list(TOOL_DEFS)
    msgs = [{"role": "user", "content": [{"type": "text", "text": TOOL_PROMPT}]}]

    # 2-3. Connection and tool calling (first request also writes the prompt cache)
    t0 = time.monotonic()
    try:
        r1 = backend.complete(system, tools, msgs, max_tokens=1024, show=False)
    except Exception as e:  # report every kind of failure as a finding
        what, hint = _api_error(e, config)
        checks.append(Check("API connection", "FAIL", what, hint))
        return checks, spent
    took = time.monotonic() - t0
    spent += cost_usd(r1.model, r1.usage, config) or 0.0
    level = getattr(backend, "level", None)
    detail = f"answered in {took:.1f}s · served by {r1.model}"
    if level and level != "full":
        checks.append(Check("API connection", "WARN", detail + f" · compatibility mode '{level}'",
                            "The endpoint rejected newer request fields (thinking/effort/automatic caching). "
                            "It works, with fewer features. " + " ".join(ui.notices)[:300]))
    else:
        checks.append(Check("API connection", "PASS", detail))
    used_tool = any(b.get("type") == "tool_use" and b.get("name") == "list_dir" for b in r1.content)
    if used_tool:
        checks.append(Check("Tool calling", "PASS", "the model called a tool correctly"))
    else:
        checks.append(Check("Tool calling", "FAIL", "the model answered in text instead of calling a tool",
                            "Anvil can't edit files or run commands with this model/gateway. Pick a model with "
                            "tool (function) calling support."))

    # 4. Prompt cache: an identical second request should read the first one's cache
    try:
        r2 = backend.complete(system, tools, msgs, max_tokens=1024, show=False)
        spent += cost_usd(r2.model, r2.usage, config) or 0.0
        u1, u2 = r1.usage, r2.usage
        prompt = u2.input_tokens + u2.cache_read_input_tokens + u2.cache_creation_input_tokens
        if not getattr(backend, "caching", True):
            checks.append(Check("Prompt cache", "WARN", "off: the endpoint rejected cache markers",
                                "Every request pays full price for the whole conversation. Ask your gateway "
                                "provider about prompt caching support."))
        elif u2.cache_read_input_tokens > 0:
            pct = u2.cache_read_input_tokens / max(prompt, 1)
            checks.append(Check("Prompt cache", "PASS", f"working: second request read {u2.cache_read_input_tokens:,} "
                                f"of {prompt:,} prompt tokens from cache ({pct:.0%})"))
        elif u1.cache_creation_input_tokens or u2.cache_creation_input_tokens:
            checks.append(Check("Prompt cache", "WARN", "the cache was written but not read back",
                                "Requests may be routed to different backends by the gateway, or the cache "
                                "expired. Run the doctor again; if it repeats, caching won't save money here."))
        else:
            checks.append(Check("Prompt cache", "WARN", f"no cache activity reported ({prompt:,}-token prompt)",
                                "The endpoint may not support or report caching for this model."))
    except Exception as e:  # keep going with the other checks
        checks.append(Check("Prompt cache", "WARN", "check failed: " + _api_error(e, config)[0]))

    # 5. Keep-alive
    if provider == "anthropic" and getattr(backend, "caching", False):
        try:
            u = backend.ping(system, tools, msgs)
        except Exception:
            u = None
        if u is not None:
            spent += cost_usd(config.model, u, config) or 0.0
            checks.append(Check("Cache keep-alive", "PASS", "accepted (zero-output refresh requests work)"))
        else:
            checks.append(Check("Cache keep-alive", "WARN", "the endpoint refused max_tokens=0 refresh requests",
                                "Anvil skips keep-alive here; pauses over 5 minutes will re-cache the conversation."))
    else:
        checks.append(Check("Cache keep-alive", "SKIP", "only used with Anthropic-format endpoints"))

    # 6. Browser agent
    if include_browser:
        from .browser import available, self_test
        if not available():
            checks.append(Check("Browser agent", "SKIP", "Playwright isn't installed", "Optional: anvil --install-browser"))
        else:
            try:
                checks.append(Check("Browser agent", "PASS", self_test(config)))
            except Exception as e:  # report, don't crash the doctor
                checks.append(Check("Browser agent", "FAIL", str(e).splitlines()[0][:200] if str(e) else type(e).__name__,
                                    "Run: anvil --install-browser"))

    # 7. Safety
    from .safety import POLICY_FILE, mode_ceiling
    ceiling = mode_ceiling(config.permission_ceiling)
    root_user = hasattr(os, "geteuid") and os.geteuid() == 0
    if root_user:
        checks.append(Check("Safety", "WARN", f"running as root · permission limit '{ceiling}'",
                            "On servers, run Anvil as a separate user: sudo bash server/setup-agent-user.sh"))
    else:
        where = f" (from {POLICY_FILE})" if POLICY_FILE.exists() else ""
        checks.append(Check("Safety", "PASS", f"not root · permission limit '{ceiling}'{where}"))

    from .sandbox import describe as sandbox_status
    status, detail = sandbox_status(root)
    checks.append(Check("Sandbox", status, detail,
                        "" if status in ("PASS", "SKIP") else "Install bubblewrap (Linux) or Docker, or set sandbox mode to off"))

    # 8. Phone access
    if config.web_public_url:
        ts = shutil.which("tailscale")
        if "ts.net" in config.web_public_url and ts:
            proc = subprocess.run([ts, "serve", "status"], capture_output=True, text=True, timeout=20)
            ok = proc.returncode == 0 and "127.0.0.1" in proc.stdout
            checks.append(Check("Phone access", "PASS" if ok else "WARN",
                                f"{config.web_public_url} · tailscale serve {'active' if ok else 'not active'}",
                                "" if ok else "Run: anvil-desktop --tailscale"))
        else:
            checks.append(Check("Phone access", "PASS", f"configured for {config.web_public_url}"))
    else:
        checks.append(Check("Phone access", "SKIP", "not set up", "Optional: anvil-desktop --tailscale"))

    # 9. Files
    problems = []
    if os.name != "nt" and CONFIG_FILE.exists() and stat.S_IMODE(CONFIG_FILE.stat().st_mode) & 0o077:
        problems.append(f"{CONFIG_FILE} is readable by other users (chmod 600 it)")
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        probe = SESSIONS_DIR / ".write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        problems.append(f"can't write sessions: {e}")
    checks.append(Check("Files", "WARN" if problems else "PASS", "; ".join(problems) or "settings private, sessions writable"))
    return checks, spent


ICONS = {"PASS": "✓", "WARN": "!", "FAIL": "✗", "SKIP": "–"}
COLORS = {"PASS": "green", "WARN": "yellow", "FAIL": "red", "SKIP": "dim"}


def main(console, config: Config, root: Path, as_json: bool = False) -> int:
    if not as_json:
        console.print("[bold]Anvil doctor[/] — checking your setup with a few small real requests (costs a few cents)…\n")
    checks, spent = run_checks(config, root)
    if as_json:
        print(json.dumps({"checks": [asdict(c) for c in checks], "cost_usd": round(spent, 5),
                          "version": __import__("anvil").__version__, "python": sys.version.split()[0],
                          "platform": sys.platform}, indent=2))
    else:
        for c in checks:
            color = COLORS[c.status]
            console.print(f"[{color}]{ICONS[c.status]} {c.status:<4}[/] [bold]{c.name:<17}[/] {c.detail}")
            if c.hint and c.status != "PASS":
                console.print(f"       [dim]→ {c.hint}[/]")
        fails = sum(c.status == "FAIL" for c in checks)
        warns = sum(c.status == "WARN" for c in checks)
        console.print(f"\n{'All good.' if not (fails or warns) else f'{fails} failed, {warns} warning(s).'} "
                      f"[dim]This check cost about ${spent:.4f}. Share the result with: anvil --doctor --json[/]")
    return 1 if any(c.status == "FAIL" for c in checks) else 0
