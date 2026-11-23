"""Terminal commands for connectors, plugins, schedules, artifacts and search.

    anvil connectors [list | presets | add PRESET | login ID | test ID | enable ID | disable ID | rm ID]
    anvil plugins    [list | install SOURCE [--plugin NAME] | update NAME | enable NAME | disable NAME | rm NAME]
    anvil schedule   [list | add | run ID | rm ID | enable ID | disable ID | daemon]
    anvil artifacts  [list | show ID | rm ID]
    anvil search     QUERY...
"""

import argparse
import getpass
import json
import sys
import time
from datetime import datetime
from pathlib import Path

COMMANDS = ("connectors", "plugins", "schedule", "schedules", "artifacts", "search")


def _console():
    from rich.console import Console
    return Console()


def main(argv: list[str]) -> int:
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "connectors":
            return connectors_cmd(rest)
        if cmd == "plugins":
            return plugins_cmd(rest)
        if cmd in ("schedule", "schedules"):
            return schedule_cmd(rest)
        if cmd == "artifacts":
            return artifacts_cmd(rest)
        if cmd == "search":
            return search_cmd(rest)
    except (ValueError, RuntimeError, OSError) as e:
        _console().print(f"[red]{e}[/]")
        return 1
    return 2


# --- connectors --------------------------------------------------------------------

def connectors_cmd(rest: list[str]) -> int:
    from . import connectors as C

    con = _console()
    p = argparse.ArgumentParser(prog="anvil connectors")
    p.add_argument("action", nargs="?", default="list",
                   choices=["list", "presets", "add", "login", "logout", "test", "enable", "disable", "rm"])
    p.add_argument("target", nargs="?")
    a = p.parse_args(rest)
    if a.action == "list":
        items = C.all_connectors()
        if not items:
            con.print("No connectors yet. See presets with: anvil connectors presets")
        for c in items:
            v = C.public_view(c)
            auth = " signed in" if v["signed_in"] else (" token set" if v["token_set"] else "")
            con.print(f"{'●' if v['enabled'] else '○'} {v['id']:<22} {v['name']:<28} {v['transport']}{auth}  ({v['source']})")
        return 0
    if a.action == "presets":
        for pr in C.PRESETS:
            con.print(f"{pr['id']:<18} {pr['name']:<26} {pr['description']}")
        return 0
    if not a.target:
        con.print("[red]Which one? Give a preset (for add) or a connector id.[/]")
        return 2
    if a.action == "add":
        pr = C.PRESET_BY_ID.get(a.target)
        if not pr:
            raise ValueError(f"unknown preset {a.target!r}; see: anvil connectors presets")
        data = {"preset": pr["id"], "values": {}}
        if pr.get("notes"):
            con.print(f"[dim]{pr['notes']}[/]")
        if pr.get("custom"):
            data["name"] = input("Name: ").strip()
            if pr["transport"] == "http":
                data["url"] = input("Server URL: ").strip()
                tok = getpass.getpass("Bearer token (empty to sign in with the browser): ").strip()
                if tok:
                    data["values"]["token"] = tok
            else:
                data["command"] = input("Command (e.g. npx -y some-mcp-server): ").strip()
        for f in pr.get("fields", []):
            prompt = f"{f['label']}{' [' + f['default'] + ']' if f.get('default') else ''}: "
            data["values"][f["key"]] = getpass.getpass(prompt) if f.get("secret") else input(prompt)
        c = C.save_connector(data)
        con.print(f"Added connector [bold]{c['id']}[/].")
        if c.get("auth") == "oauth":
            con.print(f"Sign in with: anvil connectors login {c['id']}")
        return 0
    cid = a.target
    if not C.get(cid):
        raise ValueError(f"no connector {cid!r}")
    mgr = C.manager()
    if a.action in ("enable", "disable"):
        C.set_enabled(cid, a.action == "enable")
    elif a.action == "rm":
        C.delete_connector(cid)
    elif a.action == "logout":
        mgr.sign_out(cid)
    elif a.action == "test":
        mgr.connect(cid, force=True)
        st = mgr.status.get(cid, {})
        con.print(f"{st.get('state')}: {', '.join(st.get('tools', [])) or st.get('error', '')}")
        mgr.close_all()
        return 0 if st.get("state") == "ready" else 1
    elif a.action == "login":
        import webbrowser
        done = []
        url = mgr.start_login(cid, on_done=lambda _: done.append(1))
        con.print(f"Opening your browser to sign in. If it doesn't open, visit:\n{url}")
        webbrowser.open(url)
        while not done:
            time.sleep(0.3)
        st = mgr.status.get(cid, {})
        con.print("[green]Signed in.[/]" if st.get("state") == "ready" else f"[red]{st.get('error') or st.get('state')}[/]")
        mgr.close_all()
        return 0 if st.get("state") == "ready" else 1
    con.print("Done.")
    return 0


# --- plugins -----------------------------------------------------------------------

def plugins_cmd(rest: list[str]) -> int:
    from . import plugins as P

    con = _console()
    p = argparse.ArgumentParser(prog="anvil plugins")
    p.add_argument("action", nargs="?", default="list", choices=["list", "install", "update", "enable", "disable", "rm"])
    p.add_argument("target", nargs="?")
    p.add_argument("--plugin", default="", help="For a repository with several plugins: which one")
    a = p.parse_args(rest)
    if a.action == "list":
        items = P.installed()
        if not items:
            con.print("No plugins installed. Install one with: anvil plugins install <git-url or folder>")
        for x in items:
            parts = [f"{len(x['skills'])} skills", f"{len(x['commands'])} commands", f"{len(x['connectors'])} connectors"]
            con.print(f"{'●' if x['enabled'] else '○'} {x['name']:<22} {x['version']:<8} {', '.join(parts)}  {x['description'][:60]}")
        return 0
    if not a.target:
        con.print("[red]Give a plugin name, git URL or folder.[/]")
        return 2
    if a.action == "install":
        x = P.install(a.target, a.plugin)
        con.print(f"Installed [bold]{x['name']}[/]: {len(x['skills'])} skills, {len(x['commands'])} commands, "
                  f"{len(x['connectors'])} connectors.")
    elif a.action == "update":
        P.update(a.target)
        con.print("Updated.")
    elif a.action in ("enable", "disable"):
        P.set_enabled(a.target, a.action == "enable")
        con.print(f"{a.action.capitalize()}d.")
    elif a.action == "rm":
        P.remove(a.target)
        con.print("Removed.")
    return 0


# --- schedules ---------------------------------------------------------------------

def _local(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "-"


def schedule_cmd(rest: list[str]) -> int:
    from . import schedules as S

    con = _console()
    p = argparse.ArgumentParser(prog="anvil schedule")
    p.add_argument("action", nargs="?", default="list", choices=["list", "add", "run", "rm", "enable", "disable", "daemon"])
    p.add_argument("target", nargs="?")
    p.add_argument("--name", default="")
    p.add_argument("--cron", default="", help="5-field cron, or hourly/daily/weekdays/weekly")
    p.add_argument("--prompt", default="")
    p.add_argument("-C", "--cwd", default=".")
    p.add_argument("--mode", choices=list(S.MODES), default="edits",
                   help="What runs without asking: ask (nothing), edits (file edits), auto (edits and commands)")
    a = p.parse_args(rest)
    if a.action == "list":
        items = S.load()
        if not items:
            con.print("No scheduled tasks. Add one with: anvil schedule add --cron daily --prompt '...'")
        for s in items:
            con.print(f"{'●' if s.get('enabled', True) else '○'} {s['id']:<14} {s['name'][:30]:<30} "
                      f"{S.describe(s['cron']):<24} next {_local(s.get('next_run', ''))}  last {s.get('last_status') or '-'}")
        return 0
    if a.action == "add":
        prompt = a.prompt or input("Prompt: ").strip()
        cron = a.cron or input("When (cron, or hourly/daily/weekdays/weekly) [daily]: ").strip() or "daily"
        s = S.save({"name": a.name, "prompt": prompt, "cron": cron, "root": str(Path(a.cwd).expanduser().resolve()),
                    "mode": a.mode})
        con.print(f"Added [bold]{s['id']}[/]: {S.describe(s['cron'])}, next run {_local(s['next_run'])}.")
        con.print("[dim]Runs happen while the desktop app is open, or with: anvil schedule daemon[/]")
        return 0
    if a.action == "daemon":
        return daemon()
    if not a.target or not S.get(a.target):
        con.print("[red]Give a scheduled task id (see: anvil schedule list).[/]")
        return 2
    if a.action == "rm":
        S.delete(a.target)
    elif a.action in ("enable", "disable"):
        s = S.get(a.target)
        S.save({**s, "enabled": a.action == "enable"})
    elif a.action == "run":
        sid = run_headless(S.get(a.target))
        con.print(f"Finished. Session {sid}.")
        return 0
    con.print("Done.")
    return 0


def run_headless(sched: dict) -> str:
    """Run a scheduled task in this process with no one to approve anything: calls that would
    need approval under the task's mode are declined."""
    from .agent import Agent
    from .cli import TerminalUI, ensure_configured, new_session_id, run_turn
    from .config import SESSIONS_DIR, Config
    from .fsutil import atomic_write_text
    from .safety import allowed

    config = Config.load()
    ensure_configured(config)
    ui = TerminalUI(interactive=False)
    agent = Agent(config, Path(sched["root"]), ui)
    agent.session_id = new_session_id()
    agent.project_id = sched.get("project_id", "")
    mode = sched.get("mode", "edits")
    if mode == "auto" and allowed("auto", agent.ceiling):
        agent.auto_approve_all = True
    elif mode in ("auto", "edits") and allowed("edits", agent.ceiling):
        agent.accept_edits = True
    text = sched["prompt"]
    if text.startswith("/"):
        text = agent.expand_skill(text) or text
    try:
        run_turn(agent, text)
    finally:
        if agent.messages:
            atomic_write_text(SESSIONS_DIR / f"{agent.session_id}.json", json.dumps({
                "root": str(agent.root), "model": config.model, "title": f"⏰ {sched.get('name', '')}",
                "saved_at": datetime.now().isoformat(timespec="seconds"), "schedule_id": sched["id"],
                "messages": agent.messages}), private=True)
        agent.stop_keepalive()
    return agent.session_id


def daemon() -> int:
    from . import schedules as S

    con = _console()
    sched = S.Scheduler(run_headless)
    con.print(f"Anvil scheduler running ({len(S.load())} tasks). Ctrl+C to stop.")
    try:
        sched.loop()
    except KeyboardInterrupt:
        sched.stop.set()
    return 0


# --- artifacts & search -----------------------------------------------------------------

def artifacts_cmd(rest: list[str]) -> int:
    from . import artifacts as A

    con = _console()
    p = argparse.ArgumentParser(prog="anvil artifacts")
    p.add_argument("action", nargs="?", default="list", choices=["list", "show", "rm"])
    p.add_argument("target", nargs="?")
    a = p.parse_args(rest)
    if a.action == "list":
        for x in A.list_all():
            con.print(f"{x['id']:<18} {x['kind']:<9} v{x['version']:<3} {_local(x['updated_at'])}  {x['title']}")
        return 0
    art = A.get(a.target or "")
    if not art:
        con.print("[red]No such artifact.[/]")
        return 1
    if a.action == "rm":
        A.delete(art["id"])
        con.print("Deleted.")
    else:
        sys.stdout.write(art["content"])
    return 0


def search_cmd(rest: list[str]) -> int:
    from .search import search

    con = _console()
    q = " ".join(rest).strip()
    if not q:
        con.print("Usage: anvil search WORDS")
        return 2
    results = search(q)
    if not results:
        con.print("No matches.")
    for r in results:
        con.print(f"[bold]{r['type']:<9}[/] {r['id']:<22} {r['title'][:50]}")
        if r.get("snippet"):
            con.print(f"          [dim]{r['snippet'][:140]}[/]", markup=True, highlight=False)
    return 0
