import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import anthropic
import openai
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from . import __version__
from .agent import Agent, Interrupted
from .fsutil import atomic_write_text
from .skills import PERSONAL_DIR, add_repo, discover, sync_dirs
from .config import CONFIG_DIR, EFFORTS, HISTORY_FILE, SESSIONS_DIR, Config, price_for, resolve_model

console = Console(highlight=False)

COMMANDS = {
    "/help": "Show this help",
    "/clear": "Start a new conversation",
    "/model": "Show or switch model: /model opus|sonnet|haiku|fable|<model-id>",
    "/effort": "Show or set effort: low|medium|high|xhigh|max (OpenAI format: low|medium|high|off)",
    "/cost": "Show spend and context size for this session",
    "/compact": "Summarize the conversation to free up context",
    "/save": "Save the session: /save [name]",
    "/load": "Load a saved session: /load <name>",
    "/sessions": "List saved sessions",
    "/yolo": "Toggle auto-approving edits and shell commands",
    "/web": "Toggle the web search tool",
    "/edit": "Compose a long prompt in $EDITOR",
    "/init": "Have the agent write an AGENTS.md for this project",
    "/key": "Set and save your API key",
    "/setup": "Configure provider, base URL, model and key (for gateways)",
    "/provider": "Show the current provider, endpoint and model",
    "/skills": "List skills · /skills sync (git pull skill repos) · /skills add <git-url>",
    "/exit": "Quit",
}

TOOL_ARG_KEYS = {"load_skill": "name", "read_file": "path", "write_file": "path", "edit_file": "path", "bash": "command",
                 "glob": "pattern", "grep": "pattern", "list_dir": "path",
                 "browser_open": "url", "browser_click": "ref", "browser_type": "text", "browser_select": "option",
                 "browser_press": "key", "browser_scroll": "direction", "browser_history": "action",
                 "browser_tabs": "action", "browser_wait": "text"}


class TerminalUI:
    def __init__(self, interactive: bool = True):
        self.interactive = interactive
        self._live: Live | None = None
        self._buf = ""
        self._thinking_open = False

    # streaming text --------------------------------------------------------
    def text_start(self):
        self._buf = ""
        self._live = Live(Markdown(""), console=console, refresh_per_second=12, vertical_overflow="visible")
        self._live.start()

    def text_delta(self, text: str):
        self._buf += text
        if self._live:
            self._live.update(Markdown(self._buf))

    def text_end(self):
        if self._live:
            self._live.update(Markdown(self._buf))
            self._live.stop()
            self._live = None

    def thinking_start(self):
        self._thinking_open = False

    def thinking_delta(self, text: str):
        if not text:
            return
        if not self._thinking_open:
            console.print(Text("✻ thinking", style="dim magenta"))
            self._thinking_open = True
        console.print(Text(text, style="dim italic"), end="")

    def thinking_end(self):
        if self._thinking_open:
            console.print()
        self._thinking_open = False

    def stream_aborted(self):
        if self._live:
            self._live.stop()
            self._live = None
        if self._thinking_open:
            console.print()
            self._thinking_open = False

    # tools -----------------------------------------------------------------
    def tool_call_streaming(self, name: str):
        pass

    def _label(self, name: str, args: dict) -> str:
        key = TOOL_ARG_KEYS.get(name)
        val = str(args.get(key, "")) if key else ""
        if len(val) > 100:
            val = val[:97] + "..."
        return f"{name}({val})"

    def tool_running(self, name: str, args: dict):
        console.print(Text("● ", style="cyan") + Text(self._label(name, args), style="bold"))

    def tool_result(self, name: str, args: dict, result: str, is_error: bool):
        lines = result.splitlines() or [""]
        preview = "\n".join(lines[:6])
        if len(lines) > 6:
            preview += f"\n… (+{len(lines) - 6} lines)"
        style = "red" if is_error else "dim"
        if name in ("read_file",) and not is_error:
            preview = f"Read {len(lines)} lines"
        if name.startswith("browser_") and not is_error:
            m = re.search(r'<untrusted-web-content url="([^"]*)" title="([^"]*)"', result)
            if m:
                n = len(re.findall(r"^\[\d+\] ", result, re.M))
                preview = f"{m.group(2) or '(untitled)'} · {m.group(1)} · {n} interactive elements"
        console.print(Text("  ⎿ ", style="dim") + Text(preview.replace("\n", "\n    "), style=style))

    def tool_image(self, name: str, data_url: str):
        console.print(Text(f"    (screenshot sent to the model, {len(data_url) * 3 // 4 // 1024} KB)", style="dim"))

    def confirm(self, name: str, args: dict, preview: str, dangers: list[str] | None = None):
        if not self.interactive:
            return "no" if not dangers else (
                "no. This is a destructive operation (" + "; ".join(dangers) + ") and needs a person to "
                "approve it in an interactive session. Explain what you wanted to run and why instead.")
        title = "Run command?" if name == "bash" else "Browser action?" if name.startswith("browser_") else f"{name}: {args.get('path', '')}"
        lang = "bash" if name == "bash" else "text" if name.startswith("browser_") else "diff"
        body = Syntax(preview, lang, word_wrap=True, theme="ansi_dark")
        if dangers:
            console.print(Panel(body, title=f"⚠ DESTRUCTIVE · {title}", border_style="red", expand=False,
                                subtitle="; ".join(dangers)))
        else:
            console.print(Panel(body, title=title, border_style="yellow", expand=False))
        try:
            if dangers:
                ans = console.input("[red bold]Type 'yes' to allow this once[/], anything else to deny (or type instructions): ").strip()
                return "yes" if ans.lower() == "yes" else (ans if ans and ans.lower() not in ("n", "no") else "no")
            ans = console.input("[yellow]Allow? [/][bold]y[/]es / [bold]a[/]lways this session / [bold]n[/]o (or type instructions): ").strip()
        except (EOFError, KeyboardInterrupt):
            raise KeyboardInterrupt
        low = ans.lower()
        if low in ("y", "yes", ""):
            return "yes"
        if low in ("a", "always"):
            return "always"
        if low in ("n", "no"):
            return "no"
        return ans

    def notice(self, msg: str):
        console.print(Text(f"! {msg}", style="yellow"))


# --- sessions ---------------------------------------------------------------

def save_session(agent: Agent, name: str | None = None) -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = SESSIONS_DIR / f"{name or agent.session_id}.json"
    atomic_write_text(path, json.dumps({
        "root": str(agent.root), "model": agent.config.model,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "messages": agent.messages,
    }), private=True)
    return path


def load_session(agent: Agent, name: str) -> bool:
    path = SESSIONS_DIR / f"{name}.json"
    if not path.exists():
        return False
    data = json.loads(path.read_text())
    agent.messages = data["messages"]
    agent.session_id = name
    return True


def latest_session_for(root: Path) -> str | None:
    if not SESSIONS_DIR.exists():
        return None
    for p in sorted(SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            if json.loads(p.read_text()).get("root") == str(root):
                return p.stem
        except (json.JSONDecodeError, OSError):
            continue
    return None


# --- helpers ----------------------------------------------------------------

def setup_wizard(config: Config) -> None:
    console.print(Panel(
        "Which API format does your endpoint speak?\n"
        "  [bold]1[/] Anthropic official API (api.anthropic.com)\n"
        "  [bold]2[/] Anthropic-compatible gateway (messages format, /v1/messages)\n"
        "  [bold]3[/] OpenAI-compatible gateway (chat completions, /v1/chat/completions)\n"
        "     e.g. OpenRouter, LiteLLM, Together, Groq, vLLM, most proxies\n"
        f"Settings are saved to {CONFIG_DIR / 'config.json'} (readable only by you).",
        title="Anvil setup", border_style="cyan"))
    default = {"anthropic": "2" if config.base_url else "1", "openai": "3"}[config.resolved_provider()]
    choice = (console.input(f"Choice [{default}]: ").strip() or default)
    if choice == "1":
        config.provider, config.base_url = "anthropic", ""
    else:
        config.provider = "openai" if choice == "3" else "anthropic"
        hint = " (usually ends in /v1, e.g. https://openrouter.ai/api/v1)" if choice == "3" else " (without /v1/messages)"
        url = console.input(f"Base URL{hint} [{config.base_url}]: ").strip()
        if url:
            config.base_url = url.rstrip("/")
        if config.provider == "anthropic" and config.base_url.endswith("/v1"):
            config.base_url = config.base_url[:-3]  # the Anthropic SDK appends /v1/messages itself
    model = console.input(f"Model name, exactly as your provider spells it [{config.model}]: ").strip()
    if model:
        config.model = resolve_model(model)
    key = getpass.getpass("API key (leave empty to keep the current one): ").strip()
    if key:
        config.api_key = key
    if save_to_cloud(config):
        return
    config.save()
    console.print("[green]Saved.[/] Run [bold]anvil[/] in your project folder. Change this any time with [bold]anvil --setup[/] or /setup.")


def save_to_cloud(config: Config) -> bool:
    """When signed in to Anvil cloud, synced settings go to the server, not config.json."""
    if not config.cloud_email:
        return False
    from .cloud.client import CloudError, push_settings
    from .cloud.server import SYNCED
    try:
        push_settings({k: getattr(config, k) for k in SYNCED})
    except CloudError as e:
        console.print(f"[red]Couldn't save to Anvil cloud: {e}[/]")
        return True
    config.save()  # machine-only settings
    console.print(f"[green]Saved to Anvil cloud[/] ({config.cloud_email}). Every signed-in machine gets it on next start.")
    return True


def ensure_configured(config: Config) -> None:
    if config.resolved_key():
        return
    if not config.cloud_email:
        console.print("[dim]Have an Anvil cloud server? Press Ctrl+C and run [bold]anvil login <server>[/] instead.[/]")
    setup_wizard(config)
    if not config.resolved_key():
        console.print("[red]No API key given; exiting.[/]")
        sys.exit(1)


def describe_endpoint(config: Config) -> str:
    fmt = "OpenAI format" if config.resolved_provider() == "openai" else "Anthropic format"
    return f"{config.resolved_base_url() or 'api.anthropic.com'} ({fmt})"


def edit_in_editor() -> str:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as f:
        path = f.name
    try:
        subprocess.call([*editor.split(), path])
        return Path(path).read_text().strip()
    finally:
        os.unlink(path)


def run_turn(agent: Agent, text: str) -> None:
    try:
        agent.send(text)
    except Interrupted:
        console.print("[yellow]⏹ Interrupted.[/] [dim]Tell the agent what to do instead, or continue.[/]")
    except (anthropic.AuthenticationError, openai.AuthenticationError):
        console.print("[red]Authentication failed: the endpoint rejected your API key. Use /key or /setup.[/]")
    except (anthropic.PermissionDeniedError, openai.PermissionDeniedError) as e:
        console.print(f"[red]Permission denied: {e.message}[/]")
    except (anthropic.NotFoundError, openai.NotFoundError) as e:
        console.print(f"[red]Not found: {e.message}[/]\n[dim]Check the model name ({agent.config.model}) and base URL "
                      f"({describe_endpoint(agent.config)}). Use /setup to fix them.[/]")
    except (anthropic.RateLimitError, openai.RateLimitError):
        console.print("[red]Rate limited even after retries. Wait a bit and try again.[/]")
    except (anthropic.BadRequestError, openai.BadRequestError) as e:
        console.print(f"[red]Bad request: {e.message}[/]")
    except (anthropic.APIStatusError, openai.APIStatusError) as e:
        console.print(f"[red]API error {e.status_code}: {e.message}[/]")
    except (anthropic.APIConnectionError, openai.APIConnectionError):
        console.print(f"[red]Couldn't reach {describe_endpoint(agent.config)}. Check the base URL and your connection.[/]")


def install_browser() -> int:
    """pip install playwright, then download Chromium (and, as root on apt systems, its libraries)."""
    if getattr(sys, "frozen", False):  # packaged app: Playwright is bundled, only the browser may be missing
        from .browser import install_chromium
        return install_chromium()
    steps = [[sys.executable, "-m", "pip", "install", "--upgrade", "playwright"]]
    pw = [sys.executable, "-m", "playwright", "install", "chromium"]
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0 and shutil.which("apt-get"):
        pw.append("--with-deps")
    steps.append(pw)
    for cmd in steps:
        console.print(f"[dim]$ {' '.join(cmd)}[/]")
        if subprocess.call(cmd) != 0:
            console.print("[red]That step failed.[/] On Oracle Linux/RHEL, Chromium needs some system libraries: "
                          "sudo dnf install -y nss atk at-spi2-atk cups-libs libdrm libxkbcommon libXcomposite "
                          "libXdamage libXrandr mesa-libgbm pango alsa-lib")
            return 1
    console.print("[green]Browser tools are ready.[/] Anvil now offers browser_open, browser_click and friends.")
    return 0


def print_help() -> None:
    for cmd, desc in COMMANDS.items():
        console.print(f"  [bold cyan]{cmd:<10}[/] {desc}")
    console.print("  [bold cyan]!<cmd>    [/] Run a shell command yourself")
    console.print("\n  [dim]Enter sends · Alt+Enter (or Esc then Enter) inserts a newline · Ctrl+C interrupts · Ctrl+D quits[/]")


def handle_command(agent: Agent, line: str) -> str | None:
    """Handle a slash command. Returns a prompt to send to the agent, or None."""
    cmd, _, arg = line.partition(" ")
    arg = arg.strip()
    cfg = agent.config
    if cmd == "/help":
        print_help()
    elif cmd == "/clear":
        agent.reset()
        agent.session_id = new_session_id()
        console.print("[dim]Started a new conversation.[/]")
    elif cmd == "/model":
        if arg:
            cfg.model = resolve_model(arg)
            cfg.save()
            agent.rebuild_backend()
            if agent.messages:
                console.print("[dim]Note: the prompt cache is per model, so the next request re-reads the whole conversation once.[/]")
        console.print(f"Model: [bold]{cfg.model}[/]  [dim](aliases: opus, sonnet, haiku, fable)[/]")
        if price_for(cfg.model, cfg) is None:
            console.print("[dim]No price known for this model, so /cost shows tokens only. "
                          "Set price_input/price_output in the config file for estimates.[/]")
    elif cmd == "/effort" and cfg.resolved_provider() == "openai":
        if arg:
            cfg.reasoning_effort = "" if arg == "off" else arg
            cfg.save()
        console.print(f"Reasoning effort: [bold]{cfg.reasoning_effort or 'off (not sent)'}[/]")
    elif cmd == "/effort":
        if arg:
            if arg not in EFFORTS:
                console.print(f"[red]Effort must be one of {', '.join(EFFORTS)}[/]")
                return None
            if agent.messages and arg != cfg.effort:
                console.print("[dim]Note: changing effort mid-conversation re-processes the history once (cache miss).[/]")
            cfg.effort = arg
            cfg.save()
        console.print(f"Effort: [bold]{cfg.effort}[/]")
    elif cmd == "/cost":
        spend = f"${agent.total_cost:.4f}" + ("" if agent.cost_known else " (some calls had no known price)")
        console.print(f"Tokens in: {agent.total_input_tokens:,} · out: {agent.total_output_tokens:,} · "
                      f"spend: [bold]{spend}[/] · context: {agent.last_context_tokens:,} tokens")
        console.print(cache_line(agent))
    elif cmd == "/compact":
        with console.status("Compacting conversation…"):
            try:
                summary = agent.compact()
            except (anthropic.APIError, RuntimeError) as e:
                console.print(f"[red]Compaction failed: {e}[/]")
                return None
        console.print(Panel(Markdown(summary or "(nothing to compact)"), title="Conversation summary", border_style="dim"))
    elif cmd == "/save":
        console.print(f"Saved to {save_session(agent, arg or None)}")
    elif cmd == "/load":
        if load_session(agent, arg):
            console.print(f"Loaded session {arg} ({len(agent.messages)} messages)")
        else:
            console.print(f"[red]No session named {arg!r}. Try /sessions.[/]")
    elif cmd == "/sessions":
        files = sorted(SESSIONS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if SESSIONS_DIR.exists() else []
        for p in files[:20]:
            try:
                d = json.loads(p.read_text())
                console.print(f"  [bold]{p.stem}[/]  [dim]{d.get('saved_at', '')} · {d.get('root', '')} · {len(d.get('messages', []))} msgs[/]")
            except (json.JSONDecodeError, OSError):
                continue
        if not files:
            console.print("[dim]No saved sessions.[/]")
    elif cmd == "/yolo":
        if not agent.auto_approve_all and agent.ceiling != "auto":
            console.print(f"[red]Auto-approve is disabled here: the permission limit is '{agent.ceiling}' "
                          "(set by /etc/anvil/policy.json or permission_ceiling in your config).[/]")
            return None
        agent.auto_approve_all = not agent.auto_approve_all
        console.print(f"Auto-approve: [bold]{'ON — edits and commands run without asking' if agent.auto_approve_all else 'off'}[/]")
    elif cmd == "/web":
        cfg.web_search = not cfg.web_search
        cfg.save()
        console.print(f"Web search: [bold]{'on' if cfg.web_search else 'off'}[/]")
        if cfg.web_search and not agent.backend.supports_web_search:
            console.print("[dim]This endpoint doesn't support Anthropic's web search tool, so it stays unused.[/]")
    elif cmd == "/edit":
        text = edit_in_editor()
        if text:
            return text
    elif cmd == "/init":
        return ("Explore this project and write an AGENTS.md file at the project root that would help an AI coding agent "
                "work here: what the project is, how it's structured, how to build, run and test it, and the conventions it follows. "
                "Keep it concise. If one already exists, improve it.")
    elif cmd == "/key":
        key = getpass.getpass("New API key: ").strip()
        if key:
            cfg.api_key = key
            cfg.save()
            agent.rebuild_backend()
            console.print("[green]Key saved.[/]")
    elif cmd == "/setup":
        setup_wizard(cfg)
        agent.rebuild_backend()
    elif cmd == "/provider":
        console.print(f"Endpoint: [bold]{describe_endpoint(cfg)}[/]\nModel: [bold]{cfg.model}[/]")
    elif cmd == "/skills":
        sub, _, rest = arg.partition(" ")
        if sub == "sync":
            for line in sync_dirs(agent.root):
                console.print(f"  {line}")
            agent.reset_system()
        elif sub == "add" and rest.strip():
            try:
                dest = add_repo(rest.strip())
                console.print(f"[green]Added skills from {rest.strip()} to {dest}[/]")
                agent.reset_system()
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                console.print(f"[red]{e}[/]")
        else:
            print_skills(agent)
    elif cmd in ("/exit", "/quit"):
        raise EOFError
    else:
        expanded = agent.expand_skill(line)
        if expanded:
            console.print(f"[dim]Running skill {cmd}…[/]")
            return expanded
        console.print(f"[red]Unknown command {cmd}. Type /help or /skills.[/]")
    return None


def cache_line(agent: Agent) -> str:
    c = agent.cache_stats()
    if not c["enabled"]:
        return "[yellow]Prompt cache: off for this endpoint[/] (it rejected cache markers)"
    if not (c["read"] or c["write"]):
        return "[dim]Prompt cache: nothing cached yet (prompts under ~1k tokens, or the endpoint doesn't report it)[/]"
    saved = f" · saved [bold green]${c['saved_usd']:.4f}[/] vs. no cache" if agent.cost_known else ""
    pings = f" · {c['pings']} keep-alive refresh{'es' if c['pings'] != 1 else ''}" if c["pings"] else ""
    return (f"Prompt cache: [bold]{c['hit_rate']:.0%}[/] of input read from cache "
            f"({c['read']:,} read, {c['write']:,} written, {c['fresh']:,} uncached){saved}{pings}")


def print_skills(agent: Agent) -> None:
    skills = agent.skills()
    if not skills:
        console.print("[dim]No skills found.[/]")
    for s in skills.values():
        console.print(f"  [bold cyan]/{s.name:<18}[/] {s.description}  [dim]({s.source})[/]")
    console.print(f"\n  [dim]Add your own: {PERSONAL_DIR}/<name>/SKILL.md (personal) or .anvil/skills/<name>/SKILL.md (project).[/]")


def stdin_has_data() -> bool:
    if sys.stdin is None or sys.stdin.closed or sys.stdin.isatty():
        return False
    if os.name == "nt":
        return True
    import select
    ready, _, _ = select.select([sys.stdin], [], [], 0.5)
    return bool(ready)


def new_session_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def make_prompt_session(agent: Agent | None = None) -> PromptSession:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    kb = KeyBindings()

    @kb.add("escape", "enter")
    def _(event):
        event.current_buffer.insert_text("\n")

    return PromptSession(
        history=FileHistory(str(HISTORY_FILE)),
        completer=WordCompleter(list(COMMANDS) + ([f"/{n}" for n in agent.skills()] if agent else []), sentence=True),
        key_bindings=kb,
        multiline=False,
    )


def interactive(agent: Agent) -> None:
    console.print(Panel.fit(
        f"[bold]Anvil[/] v{__version__} · your terminal coding agent\n"
        f"[dim]model[/] {agent.config.model} · [dim]endpoint[/] {describe_endpoint(agent.config)}\n"
        f"[dim]cwd[/] {agent.root}" + (f" · [dim]permissions locked to[/] {agent.ceiling}" if agent.ceiling != "auto" else "") + "\n"
        "[dim]/help for commands · Ctrl+C interrupts · Ctrl+D quits[/]",
        border_style="cyan"))
    session = make_prompt_session(agent)
    while True:
        try:
            line = session.prompt([("class:prompt", "\n❯ ")]).strip()
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        if not line:
            continue
        try:
            if line.startswith("/"):
                prompt = handle_command(agent, line)
                if prompt is None:
                    continue
                line = prompt
            elif line.startswith("!"):
                subprocess.call(line[1:], shell=True, cwd=agent.root)
                continue
        except EOFError:
            break
        run_turn(agent, line)
        if agent.messages:
            save_session(agent)
    if agent.messages:
        console.print(f"[dim]Session saved as {agent.session_id}. Resume with: anvil --resume {agent.session_id}[/]")
    console.print(f"[dim]Total spend this session: ${agent.total_cost:.4f}"
                  + (f" (prompt cache saved ${agent.saved_usd:.4f})" if agent.saved_usd > 0 else "") + "[/]")


def main() -> None:
    from .extras_cli import COMMANDS, main as extras_main
    if len(sys.argv) > 1 and sys.argv[1] in COMMANDS:  # anvil connectors|plugins|schedule|artifacts|search
        sys.exit(extras_main(sys.argv[1:]))
    from .cloud.cli import USAGE as CLOUD_USAGE, dispatch as cloud_dispatch
    rc = cloud_dispatch(sys.argv[1:])
    if rc is not None:
        sys.exit(rc)
    parser = argparse.ArgumentParser(prog="anvil", description="A terminal coding agent on your own Anthropic API key.",
                                     epilog="Cloud sign-in:\n" + CLOUD_USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("prompt", nargs="*", help="Run this prompt non-interactively and exit (same as -p)")
    parser.add_argument("-p", "--print", dest="print_prompt", help="Run one prompt non-interactively and exit")
    parser.add_argument("-m", "--model", help="Model id or alias (opus, sonnet, haiku, fable)")
    parser.add_argument("-e", "--effort", choices=EFFORTS)
    parser.add_argument("-y", "--yolo", action="store_true", help="Auto-approve edits and shell commands")
    parser.add_argument("-c", "--continue", dest="cont", action="store_true", help="Resume the latest session for this directory")
    parser.add_argument("-r", "--resume", metavar="NAME", help="Resume a saved session by name")
    parser.add_argument("-C", "--cwd", default=".", help="Project directory (default: current)")
    parser.add_argument("--provider", choices=["anthropic", "openai"], help="API format of the endpoint")
    parser.add_argument("--base-url", help="Custom endpoint / gateway base URL")
    parser.add_argument("--compat", action="store_true", help="Send only basic request fields (for strict gateways)")
    parser.add_argument("--setup", action="store_true", help="Configure provider, base URL, model and key, then exit")
    parser.add_argument("--set-key", action="store_true", help="Save your API key and exit")
    parser.add_argument("--add-skills", metavar="GIT_URL", help="Clone a skills repository into your personal skills and exit")
    parser.add_argument("--sync-skills", action="store_true", help="git pull your skills repositories and exit")
    parser.add_argument("--list-skills", action="store_true", help="List available skills and exit")
    parser.add_argument("--install-browser", action="store_true", help="Install Playwright and Chromium for the browser tools, then exit")
    parser.add_argument("--doctor", action="store_true", help="Check your setup end to end with a few small real requests")
    parser.add_argument("--json", action="store_true", help="With --doctor: print machine-readable results")
    parser.add_argument("--version", action="version", version=f"anvil {__version__}")
    args = parser.parse_args()

    config = Config.load()
    from .cloud.client import load_cloud_settings
    if load_cloud_settings(config, notice=lambda m: console.print(f"[yellow]{m}[/]")):
        console.print(f"[dim]Settings from Anvil cloud ({config.cloud_email})[/]")
    if args.install_browser:
        sys.exit(install_browser())
    if args.add_skills:
        try:
            console.print(f"Added skills to {add_repo(args.add_skills)}")
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            console.print(f"[red]{e}[/]")
            sys.exit(1)
        return
    if args.sync_skills:
        for line in sync_dirs(Path(args.cwd).expanduser().resolve()):
            console.print(line)
        return
    if args.list_skills:
        for sk in discover(Path(args.cwd).expanduser().resolve()).values():
            console.print(f"/{sk.name:<18} {sk.description}  ({sk.source})")
        return
    if args.set_key:
        config.api_key = getpass.getpass("API key: ").strip()
        if save_to_cloud(config):
            return
        config.save()
        console.print(f"Saved to {CONFIG_DIR / 'config.json'}")
        return
    if args.setup:
        setup_wizard(config)
        return
    if args.provider:
        config.provider = args.provider
    if args.base_url:
        config.base_url = args.base_url.rstrip("/")
    if args.compat:
        config.compat_mode = True
    if args.model:
        config.model = resolve_model(args.model)
    if args.effort:
        config.effort = args.effort

    root = Path(args.cwd).expanduser().resolve()
    if args.doctor:
        from .doctor import main as doctor_main
        sys.exit(doctor_main(console, config, root, as_json=args.json))
    one_shot = args.print_prompt or " ".join(args.prompt)
    ensure_configured(config)

    ui = TerminalUI(interactive=not one_shot)
    agent = Agent(config, root, ui, auto_approve_all=args.yolo)
    agent.session_id = new_session_id()
    if args.yolo and agent.ceiling != "auto":
        agent.auto_approve_all = False
        ui.notice(f"--yolo ignored: the permission limit on this machine is '{agent.ceiling}'.")

    resume = args.resume or (latest_session_for(root) if args.cont else None)
    if resume:
        if load_session(agent, resume):
            console.print(f"[dim]Resumed session {resume} ({len(agent.messages)} messages)[/]")
        else:
            console.print(f"[red]No session {resume!r} found.[/]")

    if one_shot:
        if stdin_has_data():
            piped = sys.stdin.read()
            if piped.strip():
                one_shot = f"{one_shot}\n\n<stdin>\n{piped}\n</stdin>"
        if not args.yolo:
            ui.notice("Non-interactive mode: edits and shell commands are declined unless you pass --yolo.")
        if one_shot.startswith("/"):
            expanded = agent.expand_skill(one_shot)
            if expanded is None:
                console.print(f"[red]Unknown skill {one_shot.split()[0]}. See: anvil --list-skills[/]")
                sys.exit(1)
            one_shot = expanded
        run_turn(agent, one_shot)
        save_session(agent)
        return
    interactive(agent)


if __name__ == "__main__":
    main()
