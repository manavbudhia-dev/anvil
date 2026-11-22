"""Browser chat on the Anvil cloud server: the desktop app's UI at /app/, behind Google sign-in.

The agent runs on the server, as the user the server runs as, in `chat_workspace` (or any folder
you pick for a new session). Requests are authenticated by the cloud server's sign-in cookie and,
for anything that changes state, a per-sign-in token that the page sends in a header, so other
websites can't drive it. Bash safety rules, the permission ceiling and the sandbox come from this
machine's own ~/.config/anvil/config.json, exactly as for the terminal.
"""

import json
import re
import secrets
import threading
from pathlib import Path

from ..config import Config

PREFIX = "/app"
# Desktop-only endpoints: they expose the desktop pairing token, open windows on the server or
# sign the server's own Anvil into a cloud account.
BLOCKED = {"/api/connect", "/api/qr.svg", "/api/open", "/api/pick-folder", "/api/cloud/login", "/api/cloud/logout"}


def _chat_app_class():
    from ..gui.server import App

    class ChatApp(App):
        """The desktop App, with settings stored in the cloud server's encrypted store."""

        def __init__(self, config: Config, root: Path, store, public_url: str):
            super().__init__(config, root)
            self.store, self.server_url = store, public_url
            self.email = ""
            self._applied: dict | None = None
            self._sync_lock = threading.Lock()

        def sync(self, email: str) -> None:
            """Use the signed-in account's saved settings (provider, model, key, ...)."""
            from .client import apply_settings
            settings = self.store.get_settings(email)
            with self._sync_lock:
                if email == self.email and settings == self._applied:
                    return
                apply_settings(self.config, settings)
                self.email, self._applied = email, settings
            self._rebuild_backends()

        def cloud_view(self) -> dict:
            return {"email": self.email, "server": self.server_url, "saved_login": True,
                    "login": {"status": "idle"}, "managed": True}

        def cloud_login_start(self, server: str) -> dict:
            raise ValueError("Not available on the cloud server.")

        def cloud_logout(self) -> None:
            raise ValueError("Not available on the cloud server.")

        def update_config(self, data: dict) -> None:
            from .server import SYNCED
            super().update_config(data)  # never pushes: this config has no cloud sign-in of its own
            c = self.config
            with self._sync_lock:
                cur = self.store.get_settings(self.email)
                cur.update({k: getattr(c, k) for k in SYNCED if k != "api_key" or data.get("api_key")})
                self.store.put_settings(self.email, cur)
                self._applied = cur

    return ChatApp


def _inject(index: bytes, data: dict) -> bytes:
    blob = json.dumps(data).replace("<", "\\u003c")
    # Absolute links to the app's own files (e.g. a script added by a feature) live under /app/ here.
    index = re.sub(rb'(\s(?:src|href)=")/(?!/)', rb"\1" + PREFIX.encode() + rb"/", index)
    return index.replace(b"<head>", f"<head>\n<script>window.ANVIL_CLOUD = {blob};</script>".encode(), 1)


def public_path(path: str) -> bool:
    """The app shell's install files, which browsers fetch without cookies (the manifest)."""
    from ..gui.server import PUBLIC_FILES
    return path in tuple(PREFIX + p for p in PUBLIC_FILES)


class CloudChat:
    def __init__(self, cfg, store):
        from ..gui.server import STATIC, make_handler

        root = Path(cfg.chat_workspace or "~").expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"chat_workspace {root} is not a folder")
        config = Config.load()
        self.app = _chat_app_class()(config, root, store, cfg.public_url)
        self.emails = [e.strip().lower() for e in (cfg.chat_emails or cfg.allowed_emails)]
        secure = cfg.public_url.startswith("https://")

        class ChatHandler(make_handler(self.app)):
            # The cloud server has already checked the sign-in cookie and the CSRF token.
            def _host_ok(self) -> bool:
                return True

            def _authed(self, query) -> bool:
                return True

            def _security_headers(self) -> None:
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
                self.send_header("X-Content-Type-Options", "nosniff")
                # Not "no-referrer": browsers then send "Origin: null" on same-origin requests.
                self.send_header("Referrer-Policy", "same-origin")
                if secure:
                    self.send_header("Strict-Transport-Security", "max-age=31536000")

            def do_GET(self):
                if self.path.split("?")[0] in ("/", "/index.html"):
                    body = _inject((STATIC / "index.html").read_bytes(), self.cloud_page)
                    return self._send(200, body, "text/html; charset=utf-8")
                super().do_GET()

        self.handler_cls = ChatHandler

    def allowed(self, email: str) -> bool:
        return email.lower() in self.emails

    def handle(self, outer, method: str, email: str, csrf: str) -> None:
        """Serve a /app request on the cloud server's connection, as the desktop server would."""
        path = outer.path[len(PREFIX):] or "/"
        client_close = outer.close_connection  # what the request itself asked for
        if method not in ("GET", "HEAD"):
            outer.close_connection = True  # refused requests leave the body unread; delegated ones reset this
        if not path.startswith("/"):
            return outer._json({"error": "not found"}, 404)
        if path.split("?")[0] in BLOCKED:
            return outer._json({"error": "not available on the cloud server"}, 404)
        if method not in ("GET", "HEAD"):
            site = (outer.headers.get("Sec-Fetch-Site") or "").lower()
            token = outer.headers.get("X-Anvil-Token") or ""
            if (site and site not in ("same-origin", "none")) or not csrf \
                    or not secrets.compare_digest(token.encode("utf-8", "replace"), csrf.encode()):
                return outer._json({"error": "forbidden"}, 403)
        if email:
            self.app.sync(email)
        h = self.handler_cls.__new__(self.handler_cls)  # share the connection, skip handle()
        h.__dict__.update(outer.__dict__)
        h.close_connection = client_close
        h.path = path
        h.cloud_page = {"base": PREFIX, "token": csrf, "email": email}
        fn = getattr(h, "do_" + ("GET" if method == "HEAD" else method), None)
        if fn is None:
            return outer._json({"error": "method not allowed"}, 405)
        fn()
        outer.close_connection = h.close_connection or method == "HEAD"  # HEAD got a body too
