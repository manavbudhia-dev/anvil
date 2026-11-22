"""HTTP routes for connectors, plugins, schedules, artifacts, search and the browser panel.

Kept apart from server.py so the core server stays small. server.py calls handle() for every
authenticated request; it returns (status, payload) or None when the route isn't ours.
"""

import importlib
import os
import secrets
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from .. import artifacts, connectors, plugins, schedules
from ..search import search

_ATTACHED: set[int] = set()


def attach(app) -> None:
    """Start the scheduler and wire change notifications into the app's event stream (once per app)."""
    if id(app) in _ATTACHED:
        return
    _ATTACHED.add(id(app))
    publish = lambda type_, **d: app.hub.publish({"session": "", "type": type_, **d})
    connectors.manager().listeners.append(lambda cid: publish("connectors_changed", id=cid))
    artifacts.ArtifactTools.listeners.append(lambda a: publish("artifacts_changed", id=a["id"], title=a["title"]))
    if os.environ.get("ANVIL_NO_SCHEDULER") != "1":
        app.scheduler = schedules.Scheduler(lambda s: run_schedule(app, s))
        app.scheduler.start()


def run_schedule(app, sched: dict) -> str:
    """Start a scheduled task as a normal session, so it shows up in the sidebar and in search."""
    from .server import Session

    root = Path(sched["root"])
    if not root.is_dir():
        raise ValueError(f"folder not found: {root}")
    sid = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    s = Session(app, sid, root, title=f"⏰ {sched.get('name') or 'Scheduled task'}")
    if sched.get("project_id"):
        s.agent.project_id = sched["project_id"]
    try:
        s.set_mode(sched.get("mode") or "edits")
    except ValueError:
        s.set_mode("ask")  # the machine's permission limit is stricter; approvals wait in the app
    with app.lock:
        app.sessions[sid] = s
    app.send(s, sched["prompt"])
    app.hub.publish({"session": "", "type": "sessions_changed", "id": sid})
    return sid


def _browser_view(app) -> dict:
    from ..browser import available

    importlib.invalidate_caches()
    c = app.config
    return {"installed": available(), "enabled": bool(c.browser_enabled), "headless": c.browser_headless,
            "channel": c.browser_channel, "executable": c.browser_executable,
            "installing": getattr(app, "_browser_installing", False), "log": getattr(app, "_browser_log", "")[-4000:],
            "frozen": bool(getattr(sys, "frozen", False))}


def _install_browser(app) -> None:
    if getattr(app, "_browser_installing", False):
        raise ValueError("Already installing.")
    app._browser_installing, app._browser_log = True, ""

    def log(line: str) -> None:
        app._browser_log += line.rstrip() + "\n"
        app.hub.publish({"session": "", "type": "browser_changed"})

    def run():
        try:
            if getattr(sys, "frozen", False):
                from ..browser import install_chromium
                log("Downloading Chromium…")
                code = install_chromium()
            else:
                code = 0
                for cmd in ([sys.executable, "-m", "pip", "install", "--upgrade", "playwright"],
                            [sys.executable, "-m", "playwright", "install", "chromium"]):
                    log("$ " + " ".join(cmd))
                    proc = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=900)
                    out = (proc.stdout + proc.stderr).strip().splitlines()
                    log("\n".join(out[-8:]))
                    if proc.returncode != 0:
                        code = proc.returncode
                        break
            log("Done. Browser tools are ready." if code == 0 else f"Failed (exit code {code}).")
        except Exception as e:
            log(f"Failed: {e}")
        finally:
            app._browser_installing = False
            app.hub.publish({"session": "", "type": "browser_changed"})
            for s in list(app.sessions.values()):
                if not s.running and not s.agent.messages:
                    s.agent.reset_system()

    threading.Thread(target=run, daemon=True).start()


def handle(app, method: str, path: str, query: dict, data: dict):
    parts = path.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "api":
        return None
    area, rest = parts[1], parts[2:]
    q = lambda k: (query.get(k) or [""])[0]

    # --- search ---------------------------------------------------------------------
    if area == "search" and method == "GET":
        return 200, {"results": search(q("q"), limit=int(q("limit") or 50))}

    # --- connectors -----------------------------------------------------------------
    if area == "connectors":
        mgr = connectors.manager()
        if method == "GET" and not rest:
            return 200, {"connectors": [connectors.public_view(c) for c in connectors.all_connectors()],
                         "presets": connectors.PRESETS, "redirect_uri": connectors.mcp_oauth.redirect_uri()}
        if method == "POST" and not rest:
            c = connectors.save_connector(data)
            if c.get("enabled", True):
                threading.Thread(target=mgr.connect, args=(c["id"], True), daemon=True).start()
            return 200, {"connector": connectors.public_view(c)}
        if rest:
            cid = rest[0]
            if not connectors.get(cid):
                return 404, {"error": "connector not found"}
            action = rest[1] if len(rest) > 1 else ""
            if method == "DELETE" and not action:
                connectors.delete_connector(cid)
                return 200, {"ok": True}
            if method == "POST" and action == "enabled":
                connectors.set_enabled(cid, bool(data.get("enabled")))
                return 200, {"ok": True}
            if method == "POST" and action == "test":
                mgr.connect(cid, force=True)
                return 200, {"connector": connectors.public_view(connectors.get(cid))}
            if method == "POST" and action == "login":
                return 200, {"url": mgr.start_login(cid)}
            if method == "POST" and action == "logout":
                mgr.sign_out(cid)
                return 200, {"ok": True}
        return 404, {"error": "not found"}

    # --- plugins ----------------------------------------------------------------------
    if area == "plugins":
        if method == "GET" and not rest:
            return 200, {"plugins": plugins.installed()}
        if method == "POST" and rest == ["install"]:
            p = plugins.install(str(data.get("source", "")), str(data.get("plugin", "")).strip())
            app.refresh_skills()
            return 200, {"plugin": p}
        if rest:
            name = rest[0]
            action = rest[1] if len(rest) > 1 else ""
            if method == "DELETE" and not action:
                plugins.remove(name)
            elif method == "POST" and action == "enabled":
                plugins.set_enabled(name, bool(data.get("enabled")))
            elif method == "POST" and action == "update":
                plugins.update(name)
            else:
                return 404, {"error": "not found"}
            for c in list(connectors.manager().clients):
                if c.startswith(name.lower()):
                    connectors.manager().forget(c)
            app.refresh_skills()
            return 200, {"plugins": plugins.installed()}
        return 404, {"error": "not found"}

    # --- schedules --------------------------------------------------------------------
    if area == "schedules":
        if method == "GET" and not rest:
            return 200, {"schedules": [schedules.view(s) for s in schedules.load()], "presets": schedules.PRESETS,
                         "default_root": str(app.default_root)}
        if method == "POST" and rest == ["preview"]:
            try:
                nxt = schedules.next_run(str(data.get("cron", "")))
                return 200, {"next_run": nxt.isoformat(), "description": schedules.describe(str(data.get("cron", "")))}
            except ValueError as e:
                return 200, {"error": str(e)}
        if method == "POST" and not rest:
            return 200, {"schedule": schedules.view(schedules.save(data))}
        if rest:
            s = schedules.get(rest[0])
            if not s:
                return 404, {"error": "scheduled task not found"}
            action = rest[1] if len(rest) > 1 else ""
            if method == "DELETE" and not action:
                schedules.delete(s["id"])
                return 200, {"ok": True}
            if method == "POST" and action == "run":
                sid = run_schedule(app, s)
                schedules.mark(s["id"], last_run=datetime.now().astimezone().isoformat(timespec="seconds"),
                               last_session=sid, last_status="started (manual)")
                return 200, {"session": sid}
        return 404, {"error": "not found"}

    # --- artifacts --------------------------------------------------------------------
    if area == "artifacts":
        if method == "GET" and not rest:
            return 200, {"artifacts": artifacts.list_all(q("session"), q("project"))}
        if rest:
            aid = rest[0]
            if method == "GET":
                a = artifacts.get(aid)
                return (200, {"artifact": a}) if a else (404, {"error": "artifact not found"})
            if method == "DELETE":
                artifacts.delete(aid)
                return 200, {"ok": True}
        return 404, {"error": "not found"}

    # --- browser ----------------------------------------------------------------------
    if area == "browser":
        if method == "GET" and not rest:
            return 200, _browser_view(app)
        if method == "POST" and not rest:
            c = app.config
            if "enabled" in data:
                c.browser_enabled = bool(data["enabled"])
            if data.get("headless") in ("auto", "true", "false"):
                c.browser_headless = data["headless"]
            if data.get("channel") in ("auto", "msedge", "chrome", "chromium"):
                c.browser_channel = data["channel"]
            c.save()
            for s in list(app.sessions.values()):
                if not s.running and not s.agent.messages:
                    s.agent.reset_system()
            return 200, _browser_view(app)
        if method == "POST" and rest == ["install"]:
            _install_browser(app)
            return 200, _browser_view(app)
        if method == "POST" and rest == ["test"]:
            from ..browser import self_test
            try:
                return 200, {"result": self_test(app.config)}
            except Exception as e:
                return 200, {"error": f"{type(e).__name__}: {str(e).splitlines()[0][:300]}"}
        return 404, {"error": "not found"}

    return None
