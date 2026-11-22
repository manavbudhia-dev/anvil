"""Local web server that drives the desktop UI. Bound to 127.0.0.1 and protected by a
per-launch random token, so other websites open in your browser can't call it."""

import json
import os
import queue
import secrets
import subprocess
import threading
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import anthropic
import openai

from ..agent import Agent, Interrupted
from ..checkpoints import forget_session as forget_checkpoints
from ..fsutil import atomic_write_text
from ..skills import add_repo, display_text, sync_dirs
from ..config import CONFIG_DIR, EFFORTS, SESSIONS_DIR, Config, price_for, resolve_model

STATIC = Path(__file__).parent / "static"
TOKEN_FILE = CONFIG_DIR / "web-token"
PUBLIC_FILES = {  # served without a token: the app shell and its install assets
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/sw.js": ("sw.js", "text/javascript"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
}


def load_token(rotate: bool = False) -> str:
    """A token that survives restarts, so phones and apps stay paired."""
    if not rotate:
        try:
            tok = TOKEN_FILE.read_text().strip()
            if len(tok) >= 24:
                return tok
        except OSError:
            pass
    tok = secrets.token_urlsafe(32)
    atomic_write_text(TOKEN_FILE, tok, private=True)
    return tok
TOOL_ARG_KEYS = {"load_skill": "name", "read_file": "path", "write_file": "path", "edit_file": "path", "bash": "command",
                 "glob": "pattern", "grep": "pattern", "list_dir": "path",
                 "browser_open": "url", "browser_click": "ref", "browser_type": "text", "browser_select": "option",
                 "browser_press": "key", "browser_scroll": "direction", "browser_history": "action",
                 "browser_tabs": "action", "browser_wait": "text"}
UI_RESULT_LIMIT = 20_000
MAX_BODY = 10 * 1024 * 1024  # largest JSON request body accepted


def tool_label(name: str, args) -> str:
    key = TOOL_ARG_KEYS.get(name)
    if key and isinstance(args, dict):
        return str(args.get(key, "") or "")
    return ""


class Hub:
    """Fan-out of server-sent events to every connected window.

    Each subscriber has a bounded queue. A client that stops reading (a phone that went to sleep
    without closing the connection) is dropped once its queue fills, instead of growing memory
    forever; its EventSource reconnects and reloads state when it wakes up.
    """

    MAX_QUEUED = 2000

    def __init__(self):
        self.subscribers: list[queue.Queue] = []
        self.dropped: set[int] = set()
        self.lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self.MAX_QUEUED)
        with self.lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subscribers:
                self.subscribers.remove(q)
            self.dropped.discard(id(q))

    def is_dropped(self, q: queue.Queue) -> bool:
        with self.lock:
            return id(q) in self.dropped

    def publish(self, event: dict) -> None:
        with self.lock:
            for q in list(self.subscribers):
                try:
                    q.put_nowait(event)
                except queue.Full:
                    self.subscribers.remove(q)
                    self.dropped.add(id(q))


class GuiUI:
    """Implements the agent's UI callbacks by publishing events to the browser."""

    def __init__(self, hub: Hub, sid: str):
        self.hub = hub
        self.sid = sid
        self.cancel = threading.Event()
        self.pending: dict[str, list] = {}

    def emit(self, type_: str, **data) -> None:
        self.hub.publish({"session": self.sid, "type": type_, **data})

    def _check(self) -> None:
        if self.cancel.is_set():
            raise KeyboardInterrupt

    def text_start(self):
        self._check()
        self.emit("text_start")

    def text_delta(self, text):
        self._check()
        self.emit("text_delta", text=text)

    def text_end(self):
        self.emit("text_end")

    def thinking_start(self):
        self._check()

    def thinking_delta(self, text):
        self._check()
        if text:
            self.emit("thinking_delta", text=text)

    def thinking_end(self):
        self.emit("thinking_end")

    def stream_aborted(self):
        self.emit("stream_aborted")

    def tool_call_streaming(self, name):
        pass

    def tool_running(self, name, args):
        self._check()
        self.emit("tool_start", name=name, label=tool_label(name, args), args=args)

    def tool_image(self, name, data_url):
        self.emit("tool_image", name=name, image=data_url)

    def tool_result(self, name, args, result, is_error):
        self.emit("tool_result", name=name, label=tool_label(name, args), args=args,
                  result=result[:UI_RESULT_LIMIT], is_error=is_error)

    def confirm(self, name, args, preview, dangers=None):
        req_id = uuid.uuid4().hex
        done = threading.Event()
        self.pending[req_id] = [done, None]
        self.emit("confirm", req_id=req_id, name=name, label=tool_label(name, args), args=args, preview=preview,
                  dangers=dangers or [])
        try:
            while not done.wait(0.2):
                if self.cancel.is_set():
                    raise KeyboardInterrupt
            return self.pending[req_id][1]
        finally:
            self.pending.pop(req_id, None)
            self.emit("confirm_done", req_id=req_id)

    def resolve(self, req_id: str, decision: str) -> bool:
        slot = self.pending.get(req_id)
        if not slot:
            return False
        slot[1] = decision
        slot[0].set()
        return True

    def notice(self, msg):
        self.emit("notice", text=msg)


def messages_to_items(messages: list[dict]) -> list[dict]:
    """Turn stored API history into transcript items for the UI."""
    items: list[dict] = []
    tools: dict[str, dict] = {}
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if m["role"] == "user":
            texts = []
            for b in content:
                if b.get("type") == "tool_result" and b.get("tool_use_id") in tools:
                    c = b.get("content", "")
                    if isinstance(c, list):
                        img = next((x for x in c if isinstance(x, dict) and x.get("type") == "image"), None)
                        if img:
                            tools[b["tool_use_id"]]["image"] = f"data:{img['source']['media_type']};base64,{img['source']['data']}"
                        c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
                    tools[b["tool_use_id"]].update(result=str(c)[:UI_RESULT_LIMIT], is_error=bool(b.get("is_error")))
                elif b.get("type") == "text":
                    texts.append(b["text"])
            if texts:
                items.append({"kind": "user", "text": display_text("\n\n".join(texts))})
        else:
            for b in content:
                t = b.get("type")
                if t == "thinking" and b.get("thinking"):
                    items.append({"kind": "thinking", "text": b["thinking"]})
                elif t == "text" and b.get("text"):
                    items.append({"kind": "assistant", "text": b["text"]})
                elif t == "tool_use":
                    item = {"kind": "tool", "name": b["name"], "label": tool_label(b["name"], b.get("input")),
                            "args": b.get("input", {}), "result": None, "is_error": False}
                    tools[b["id"]] = item
                    items.append(item)
    return items


def checkpoints_view(s: "Session") -> dict:
    cps = s.agent.checkpoints
    items = [{"n": c["n"], "time": c["time"], "prompt": c["prompt"], "files": c["changed"],
              "chat": c.get("msg_index") is not None} for c in reversed(cps.items())]
    return {"checkpoints": items, "checkpoint_mode": cps.mode() if items else ""}


def session_title(messages: list[dict]) -> str:
    for m in messages:
        if m["role"] != "user":
            continue
        content = m["content"]
        if isinstance(content, str):
            return content.strip()[:80]
        for b in content:
            if b.get("type") == "text" and b["text"].strip():
                return display_text(b["text"].strip()).splitlines()[0][:80]
    return "New session"


class Session:
    def __init__(self, app: "App", sid: str, root: Path, messages: list | None = None, title: str = ""):
        self.sid = sid
        self.ui = GuiUI(app.hub, sid)
        self.agent = Agent(app.config, root, self.ui)
        self.agent.session_id = sid
        if messages:
            self.agent.messages = messages
        self.title = title or session_title(self.agent.messages)
        self.mode = "ask"
        self.running = False
        self.deleted = False
        self.backend_stale = False
        self.thread: threading.Thread | None = None
        self._busy_lock = threading.Lock()

    def try_start(self) -> bool:
        """Atomically mark the session busy. Two requests at once (a double-tap on Send) can't both win."""
        with self._busy_lock:
            if self.running or self.deleted:
                return False
            self.running = True
            if self.backend_stale:  # settings changed while it was busy
                self.agent.rebuild_backend()
                self.backend_stale = False
            return True

    def finish(self) -> None:
        with self._busy_lock:
            self.running = False

    def set_mode(self, mode: str) -> None:
        from ..safety import MODES, allowed

        if mode not in MODES:
            mode = "ask"
        if not allowed(mode, self.agent.ceiling):
            raise ValueError(f"The permission limit on this machine is '{self.agent.ceiling}'.")
        self.mode = mode
        self.agent.auto_approve_all = mode == "auto"
        self.agent.accept_edits = mode == "edits"

    def save(self) -> None:
        if not self.agent.messages or self.deleted:
            return
        atomic_write_text(SESSIONS_DIR / f"{self.sid}.json", json.dumps({
            "root": str(self.agent.root), "model": self.agent.config.model, "title": self.title,
            "saved_at": datetime.now().isoformat(timespec="seconds"), "messages": self.agent.messages,
        }), private=True)

    def meta(self) -> dict:
        a = self.agent
        return {"id": self.sid, "title": self.title, "root": str(a.root), "running": self.running,
                "mode": self.mode, "ceiling": a.ceiling, "cost": round(a.total_cost, 4), "cost_known": a.cost_known,
                "tokens_in": a.total_input_tokens, "tokens_out": a.total_output_tokens, "cache": a.cache_stats(),
                "context": a.last_context_tokens}


class App:
    def __init__(self, config: Config, default_root: Path):
        self.config = config
        self.default_root = default_root
        self.hub = Hub()
        self.sessions: dict[str, Session] = {}
        self.lock = threading.Lock()
        self.token = load_token()
        self.pick_folder = None  # set by the desktop launcher when a native dialog exists
        self.port = 0
        # Extra host names allowed in the Host header (e.g. a Tailscale name). Anything else is
        # refused, which blocks DNS-rebinding attacks from websites open in a browser.
        self.allowed_hosts = {h.lower() for h in config.web_allowed_hosts}
        self.cloud_login: dict = {"status": "idle"}

    def local_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def public_url(self) -> str | None:
        return self.config.web_public_url or None

    def connect_url(self) -> str:
        return f"{self.public_url() or self.local_url()}#token={self.token}"

    # sessions ---------------------------------------------------------------
    def list_sessions(self) -> list[dict]:
        out, seen = [], set()
        for s in self.sessions.values():
            if s.agent.messages or s.running:
                out.append({"id": s.sid, "title": s.title, "root": str(s.agent.root),
                            "running": s.running, "updated": datetime.now().isoformat(timespec="seconds")})
                seen.add(s.sid)
        if SESSIONS_DIR.exists():
            for p in SESSIONS_DIR.glob("*.json"):
                if p.stem in seen:
                    continue
                try:
                    d = json.loads(p.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                out.append({"id": p.stem, "title": d.get("title") or session_title(d.get("messages", [])),
                            "root": d.get("root", ""), "running": False, "updated": d.get("saved_at", "")})
        out.sort(key=lambda s: (s["running"], s["updated"]), reverse=True)
        return out

    def get_session(self, sid: str) -> Session | None:
        with self.lock:
            if sid in self.sessions:
                return self.sessions[sid]
            p = SESSIONS_DIR / f"{sid}.json"
            if not p.exists():
                return None
            d = json.loads(p.read_text())
            root = Path(d.get("root") or self.default_root)
            if not root.is_dir():
                root = self.default_root
            s = Session(self, sid, root, d.get("messages", []), d.get("title", ""))
            self.sessions[sid] = s
            return s

    def new_session(self, root: str | None) -> Session:
        path = Path(os.path.expanduser(root)).resolve() if root else self.default_root
        if not path.is_dir():
            raise ValueError(f"Folder not found: {path}")
        sid = datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
        s = Session(self, sid, path)
        with self.lock:
            self.sessions[sid] = s
        self.default_root = path
        if self.config.last_root != str(path):
            self.config.last_root = str(path)
            self.config.save()
        return s

    def delete_session(self, sid: str) -> None:
        with self.lock:
            s = self.sessions.pop(sid, None)
        if s:
            s.deleted = True  # the running thread's final save would otherwise re-create the file
            s.ui.cancel.set()
        p = SESSIONS_DIR / f"{sid}.json"
        root = str(s.agent.root) if s else ""
        if not root and p.exists():
            try:
                root = json.loads(p.read_text()).get("root", "")
            except (OSError, ValueError):
                pass
        if p.exists():
            p.unlink()
        forget_checkpoints(sid, root)

    def send(self, s: Session, text: str) -> None:
        if text.startswith("/"):
            expanded = s.agent.expand_skill(text)
            if expanded is None:
                raise ValueError(f"Unknown skill {text.split()[0]}. Open Skills in the sidebar to see what's available.")
        else:
            expanded = text
        if not s.try_start():
            raise ValueError("This session is already working. Stop it first.")
        if s.title == "New session":
            s.title = text.strip().splitlines()[0][:80] if text.strip() else s.title
        s.ui.cancel.clear()

        def run():
            s.ui.emit("turn_start")
            try:
                s.agent.send(expanded)
            except Interrupted:
                s.ui.emit("interrupted")
            except (anthropic.AuthenticationError, openai.AuthenticationError):
                s.ui.emit("error", text="Authentication failed: the endpoint rejected your API key. Open Settings to fix it.")
            except (anthropic.NotFoundError, openai.NotFoundError) as e:
                s.ui.emit("error", text=f"Not found: {e.message}. Check the model name and base URL in Settings.")
            except (anthropic.APIStatusError, openai.APIStatusError) as e:
                s.ui.emit("error", text=f"API error {e.status_code}: {e.message}")
            except (anthropic.APIConnectionError, openai.APIConnectionError):
                s.ui.emit("error", text="Couldn't reach the API endpoint. Check the base URL and your connection.")
            except Exception as e:  # keep the app alive and show what happened
                s.ui.emit("error", text=f"{type(e).__name__}: {e}")
            finally:
                try:
                    s.save()
                finally:
                    s.finish()
                    s.ui.emit("turn_end", meta=s.meta())

        s.thread = threading.Thread(target=run, daemon=True)
        try:
            s.thread.start()
        except BaseException:
            s.finish()
            raise

    def undo(self, s: Session, n, keep_chat: bool) -> dict:
        from ..checkpoints import CheckpointError

        if not s.try_start():
            raise ValueError("Wait for the current turn to finish, or stop it, before undoing.")
        try:
            res = s.agent.checkpoints.undo(int(n) if n not in (None, "") else None, rewind_chat=not keep_chat)
        except CheckpointError as e:
            raise ValueError(f"Can't undo: {e}") from e
        finally:
            s.finish()
        if s.agent.messages:
            s.save()
        else:  # nothing left to resume: don't bring the undone chat back on restart
            (SESSIONS_DIR / f"{s.sid}.json").unlink(missing_ok=True)
        return res

    def refresh_skills(self) -> None:
        for s in self.sessions.values():
            if not s.running and not s.agent.messages:
                s.agent.reset_system()

    # config -----------------------------------------------------------------
    def config_view(self) -> dict:
        c = self.config
        return {"provider": c.resolved_provider(), "base_url": c.base_url, "model": c.model,
                "effort": c.effort, "reasoning_effort": c.reasoning_effort, "compat_mode": c.compat_mode,
                "language": c.language if c.language in ("english", "hinglish") else "english",
                "price_input": c.price_input, "price_output": c.price_output,
                "key_set": bool(c.resolved_key()), "efforts": list(EFFORTS),
                "price_known": price_for(c.model, c) is not None, "cloud": self.cloud_view()}

    # Anvil cloud --------------------------------------------------------------
    def cloud_view(self) -> dict:
        from ..cloud.client import load_creds
        creds = load_creds() or {}
        return {"email": self.config.cloud_email, "server": creds.get("server", ""),
                "saved_login": bool(creds), "login": self.cloud_login}

    def _rebuild_backends(self) -> None:
        for s in list(self.sessions.values()):
            with s._busy_lock:
                if s.running:
                    s.backend_stale = True
                else:
                    s.agent.rebuild_backend()

    def cloud_login_start(self, server: str) -> dict:
        from ..cloud import client
        start = client.start_device_login(server)
        info = {k: start[k] for k in ("user_code", "verification_uri", "verification_uri_complete")}
        self.cloud_login = {"status": "pending", **info}

        def wait():
            try:
                email = client.poll_device_login(start)
                client.apply_settings(self.config, client.fetch_settings())
                self.config._cloud_email = email
                self._rebuild_backends()
                self.cloud_login = {"status": "ok", "email": email}
            except client.CloudError as e:
                self.cloud_login = {"status": "error", "message": str(e)}
        threading.Thread(target=wait, daemon=True).start()
        return info

    def cloud_logout(self) -> None:
        from ..cloud import client
        client.logout()
        for k, v in (getattr(self.config, "_cloud_local", None) or {}).items():
            setattr(self.config, k, v)
        self.config._cloud_local = {}
        self.config._cloud_email = ""
        self.cloud_login = {"status": "idle"}
        self._rebuild_backends()

    def update_config(self, data: dict) -> None:
        c = self.config
        if "provider" in data:
            c.provider = "openai" if data["provider"] == "openai" else "anthropic"
        if "base_url" in data:
            url = (data["base_url"] or "").strip().rstrip("/")
            if c.provider == "anthropic" and url.endswith("/v1"):
                url = url[:-3]
            c.base_url = url
        if data.get("model"):
            c.model = resolve_model(data["model"])
        if data.get("effort") in EFFORTS:
            c.effort = data["effort"]
        if "reasoning_effort" in data:
            c.reasoning_effort = data["reasoning_effort"] or ""
        if "compat_mode" in data:
            c.compat_mode = bool(data["compat_mode"])
        if data.get("language") in ("english", "hinglish"):
            c.language = data["language"]  # applies from each session's next request
        for k in ("price_input", "price_output"):
            if k in data:
                try:
                    setattr(c, k, float(data[k] or 0))
                except (TypeError, ValueError):
                    pass
        if data.get("api_key"):
            c.api_key = data["api_key"].strip()
        if c.cloud_email:
            from ..cloud.client import push_settings
            from ..cloud.server import SYNCED
            push_settings({k: getattr(c, k) for k in SYNCED if k != "api_key" or data.get("api_key")})
        c.save()
        self._rebuild_backends()


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        # helpers ------------------------------------------------------------
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            host = host.rsplit(":", 1)[0] if not host.endswith("]") else host
            return host in ("127.0.0.1", "localhost") or host in app.allowed_hosts

        def _authed(self, query: dict) -> bool:
            token = self.headers.get("X-Anvil-Token") or (query.get("token") or [""])[0]
            # compare_digest raises TypeError on non-ASCII str; compare bytes so a junk header is just a 401.
            return self._host_ok() and secrets.compare_digest(token.encode("utf-8", "replace"), app.token.encode())

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)

        def _security_headers(self) -> None:
            # The UI can approve shell commands, so it must never be framed (clickjacking), and the
            # token (sometimes in the query string) must never leak through a Referer header.
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data).encode(), "application/json")

        def _body(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n <= 0:
                return {}
            if n > MAX_BODY:
                # The body is left unread, so drop the connection or its bytes get parsed as the next request.
                self.close_connection = True
                raise ValueError("Request body too large.")
            try:
                data = json.loads(self.rfile.read(n))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}
            return data if isinstance(data, dict) else {}

        # routes -------------------------------------------------------------
        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path in ("/", "/index.html") or url.path in PUBLIC_FILES:
                if not self._host_ok():
                    return self._send(403, b"forbidden: unknown host name. Add it with anvil-desktop --allow-host NAME",
                                      "text/plain")
                name, ctype = PUBLIC_FILES.get(url.path, ("index.html", "text/html; charset=utf-8"))
                return self._send(200, (STATIC / name).read_bytes(), ctype)
            if not self._authed(q):
                return self._json({"error": "unauthorized"}, 401)
            if url.path == "/api/state":
                return self._json({"config": app.config_view(), "sessions": app.list_sessions(),
                                   "default_root": str(app.default_root), "can_pick_folder": app.pick_folder is not None,
                                   "home": str(Path.home())})
            if url.path == "/api/skills":
                sid = (q.get("session") or [""])[0]
                s = app.sessions.get(sid)
                root = s.agent.root if s else app.default_root
                from ..skills import discover
                return self._json({"skills": [{"name": k.name, "description": k.description, "source": k.source,
                                               "path": str(k.path)} for k in discover(root).values()]})
            if url.path == "/api/connect":
                return self._json({"url": app.connect_url(), "public_url": app.public_url(),
                                   "local_url": app.local_url()})
            if url.path == "/api/cloud":
                return self._json(app.cloud_view())
            if url.path == "/api/qr.svg":
                return self._send(200, qr_svg(app.connect_url()), "image/svg+xml")
            if url.path == "/api/events":
                return self._events()
            if url.path.startswith("/api/sessions/") and url.path.endswith("/checkpoints"):
                s = app.get_session(url.path.split("/")[3])
                if not s:
                    return self._json({"error": "not found"}, 404)
                return self._json(checkpoints_view(s))
            if url.path.startswith("/api/sessions/"):
                s = app.get_session(url.path.split("/")[3])
                if not s:
                    return self._json({"error": "not found"}, 404)
                return self._json({"meta": s.meta(), "items": messages_to_items(s.agent.messages)})
            self._json({"error": "not found"}, 404)

        def do_DELETE(self):
            url = urlparse(self.path)
            if not self._authed(parse_qs(url.query)):
                return self._json({"error": "unauthorized"}, 401)
            if url.path.startswith("/api/sessions/"):
                app.delete_session(url.path.split("/")[3])
                return self._json({"ok": True})
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            url = urlparse(self.path)
            if not self._authed(parse_qs(url.query)):
                return self._json({"error": "unauthorized"}, 401)
            parts = url.path.strip("/").split("/")
            try:
                data = self._body()
                if url.path == "/api/config":
                    app.update_config(data)
                    return self._json({"config": app.config_view()})
                if url.path == "/api/cloud/login":
                    info = app.cloud_login_start(str(data.get("server") or ""))
                    host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
                    if host in ("127.0.0.1", "localhost"):  # the desktop app: open the system browser
                        try:
                            webbrowser.open(info["verification_uri_complete"])
                        except Exception:
                            pass
                    return self._json(info)
                if url.path == "/api/cloud/logout":
                    app.cloud_logout()
                    return self._json({"config": app.config_view()})
                if url.path == "/api/skills/sync":
                    s = app.sessions.get(data.get("session", ""))
                    lines = sync_dirs(s.agent.root if s else app.default_root)
                    app.refresh_skills()
                    return self._json({"result": lines})
                if url.path == "/api/skills/add":
                    dest = add_repo(str(data.get("url", "")).strip())
                    app.refresh_skills()
                    return self._json({"result": [f"Added to {dest}"]})
                if url.path == "/api/open":
                    link = str(data.get("url", ""))
                    if link.startswith(("http://", "https://")):
                        webbrowser.open(link)
                    return self._json({"ok": True})
                if url.path == "/api/pick-folder":
                    path = app.pick_folder() if app.pick_folder else None
                    return self._json({"path": path})
                if url.path == "/api/sessions":
                    s = app.new_session(data.get("root"))
                    return self._json({"meta": s.meta(), "items": []})
                if len(parts) == 4 and parts[:2] == ["api", "sessions"]:
                    s = app.get_session(parts[2])
                    if not s:
                        return self._json({"error": "session not found"}, 404)
                    action = parts[3]
                    if action == "send":
                        text = (data.get("text") or "").strip()
                        if not text:
                            return self._json({"error": "empty message"}, 400)
                        app.send(s, text)
                    elif action == "stop":
                        s.ui.cancel.set()
                    elif action == "approve":
                        s.ui.resolve(data.get("req_id", ""), data.get("decision") or "no")
                    elif action == "mode":
                        s.set_mode(data.get("mode", "ask"))
                    elif action == "undo":
                        res = app.undo(s, data.get("n"), bool(data.get("keep_chat")))
                        return self._json({"meta": s.meta(), "undo": res, "items": messages_to_items(s.agent.messages),
                                           **checkpoints_view(s)})
                    elif action == "compact":
                        if not s.try_start():
                            raise ValueError("Wait for the current turn to finish.")
                        try:
                            s.agent.compact()
                            s.save()
                        finally:
                            s.finish()
                    else:
                        return self._json({"error": "unknown action"}, 404)
                    return self._json({"meta": s.meta()})
            except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                return self._json({"error": str(e)}, 400)
            except (anthropic.APIError, openai.APIError) as e:
                return self._json({"error": str(e)}, 502)
            self._json({"error": "not found"}, 404)

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self._security_headers()
            self.end_headers()
            q = app.hub.subscribe()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while not app.hub.is_dropped(q):
                    try:
                        ev = q.get(timeout=15)
                        self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                app.hub.unsubscribe(q)
                self.close_connection = True

    return Handler


def qr_svg(text: str) -> bytes:
    import io

    import qrcode
    import qrcode.image.svg

    buf = io.BytesIO()
    qrcode.make(text, image_factory=qrcode.image.svg.SvgPathFillImage, border=2).save(buf)
    return buf.getvalue()


def print_qr(text: str) -> None:
    import qrcode

    qr = qrcode.QRCode(border=1)
    qr.add_data(text)
    try:
        qr.print_ascii(invert=True)
    except BrokenPipeError:
        pass


class _Server(ThreadingHTTPServer):
    request_queue_size = 128  # the default of 5 drops connections in bursts (event stream + phone + desktop)
    daemon_threads = True


def start_server(app: App, port: int = 0) -> ThreadingHTTPServer:
    server = _Server(("127.0.0.1", port), make_handler(app))
    app.port = server.server_address[1]
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
