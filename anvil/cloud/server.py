"""Anvil cloud server: sign in with Google, keep your Anvil settings in one place.

Run it on your own server behind HTTPS (Caddy, nginx). The terminal (`anvil login`) and the
desktop app sign in through it and fetch provider, base URL, model, API key and other settings,
so no machine needs its own `anvil --setup`.

Only email addresses listed in `allowed_emails` can sign in.
"""

import base64
import hashlib
import html
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..config import CONFIG_DIR, Config
from .store import DEVICE_INTERVAL, Store, load_master_key

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
COOKIE = "anvil_cloud"
MAX_BODY = 64 * 1024

# Settings that follow you between machines. Machine-specific ones (last folder, permission
# ceiling, browser binary, web app host names, auto-approve rules) stay local.
SYNCED = ("provider", "base_url", "api_key", "model", "effort", "max_tokens", "compat_mode",
          "reasoning_effort", "price_input", "price_output", "web_search", "cache_ttl",
          "cache_keepalive_minutes", "auto_compact_tokens", "openai_cache_control", "browser_enabled",
          "language")
_DEFAULTS = Config()


def clean_settings(data: dict) -> dict:
    """Keep only known synced fields, coerced to the type of the Config default."""
    out = {}
    for k in SYNCED:
        if k not in data:
            continue
        default, v = getattr(_DEFAULTS, k), data[k]
        try:
            if isinstance(default, bool):
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")
            elif isinstance(default, int):
                v = int(v or 0)
            elif isinstance(default, float):
                v = float(v or 0)
            else:
                v = str(v or "").strip()[:2000]
        except (TypeError, ValueError):
            continue
        out[k] = v
    if "provider" in out and out["provider"] not in ("anthropic", "openai"):
        out["provider"] = "openai" if "openai" in out["provider"].lower() else "anthropic"
    if "language" in out and out["language"] not in ("english", "hinglish"):
        out["language"] = "english"
    if "base_url" in out:
        out["base_url"] = out["base_url"].rstrip("/")
        if out.get("provider", data.get("_provider_hint")) == "anthropic" and out["base_url"].endswith("/v1"):
            out["base_url"] = out["base_url"][:-3]
    return out


@dataclass
class CloudConfig:
    public_url: str = ""                 # e.g. https://anvil.example.com (what Google redirects back to)
    google_client_id: str = ""
    google_client_secret: str = ""
    allowed_emails: list[str] = field(default_factory=list)
    host: str = "127.0.0.1"
    port: int = 8787
    data_dir: str = ""                   # default: ~/.config/anvil/cloud-server
    chat: bool = False                   # browser chat at /app/: runs the agent on this server
    chat_workspace: str = ""             # folder new chat sessions start in (default: home folder)
    chat_emails: list[str] = field(default_factory=list)  # who may use chat (default: allowed_emails)

    @classmethod
    def load(cls, path: Path) -> "CloudConfig":
        cfg = cls()
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        cfg.google_client_secret = os.environ.get("ANVIL_GOOGLE_CLIENT_SECRET") or cfg.google_client_secret
        cfg.public_url = cfg.public_url.rstrip("/")
        cfg.allowed_emails = [e.strip().lower() for e in cfg.allowed_emails if e.strip()]
        return cfg

    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser() if self.data_dir else CONFIG_DIR / "cloud-server"

    def problems(self) -> list[str]:
        out = []
        if not self.public_url.startswith("https://") and not self.public_url.startswith("http://127.0.0.1") \
                and not self.public_url.startswith("http://localhost"):
            out.append("public_url must be an https:// URL (or http://localhost for testing)")
        if not self.google_client_id or not self.google_client_secret:
            out.append("google_client_id and google_client_secret are required")
        if not self.allowed_emails:
            out.append("allowed_emails is empty, so nobody could sign in")
        if self.chat and not Path(self.chat_workspace or "~").expanduser().is_dir():
            out.append(f"chat_workspace {self.chat_workspace} is not a folder")
        return out


def google_exchange(cfg: CloudConfig, code: str, verifier: str) -> dict:
    """Trade an authorization code for the signed-in user's {email, name}.

    The ID token comes straight from Google's token endpoint over TLS, authenticated with our
    client secret, so (per OpenID Connect) its signature needn't be checked; the claims still are.
    """
    body = urllib.parse.urlencode({
        "code": code, "client_id": cfg.google_client_id, "client_secret": cfg.google_client_secret,
        "redirect_uri": cfg.public_url + "/auth/callback", "grant_type": "authorization_code",
        "code_verifier": verifier}).encode()
    req = urllib.request.Request(GOOGLE_TOKEN, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        tok = json.loads(r.read())
    payload = tok["id_token"].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    if claims.get("iss") not in GOOGLE_ISSUERS or claims.get("aud") != cfg.google_client_id:
        raise ValueError("ID token is not for this app")
    if float(claims.get("exp", 0)) < time.time():
        raise ValueError("ID token expired")
    if not claims.get("email_verified"):
        raise ValueError("Google account email is not verified")
    return {"email": claims["email"].lower(), "name": claims.get("name", "")}


class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute, self.hits, self.lock = per_minute, {}, threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            recent = [t for t in self.hits.get(key, []) if now - t < 60]
            ok = len(recent) < self.per_minute
            if ok:
                recent.append(now)
            self.hits[key] = recent
            if len(self.hits) > 10_000:
                self.hits.clear()
            return ok


class CloudApp:
    def __init__(self, cfg: CloudConfig, store: Store | None = None, exchange=None, chat=None):
        self.cfg = cfg
        if store is None:
            data = cfg.data_path()
            store = Store(data / "anvil-cloud.db", load_master_key(data / "master.key"))
        self.store = store
        self.exchange = exchange or (lambda code, verifier: google_exchange(cfg, code, verifier))
        self.limiter = RateLimiter(120)
        self.secure_cookie = cfg.public_url.startswith("https://")
        if chat is None and cfg.chat:
            from .chat import CloudChat
            chat = CloudChat(cfg, store)
        self.chat = chat

    def allowed(self, email: str) -> bool:
        return email.lower() in self.cfg.allowed_emails


# --- HTML -------------------------------------------------------------------

STYLE = """
:root{color-scheme:light dark;--bg:#f6f6f4;--card:#fff;--fg:#1d1d1b;--mut:#6b6b66;--line:#ddd;--acc:#c2410c}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#20201e;--fg:#ecece8;--mut:#9a9a94;--line:#353532;--acc:#fb923c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:560px;margin:40px auto;padding:0 16px}.card{background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:20px;margin-bottom:16px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:0 0 12px}
p{margin:6px 0}.mut{color:var(--mut);font-size:13px}label{display:block;margin:10px 0 4px;font-size:13px;color:var(--mut)}
input,select{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);font:inherit}
button,.btn{display:inline-block;padding:9px 16px;border-radius:8px;border:1px solid var(--line);background:var(--card);
color:var(--fg);font:inherit;cursor:pointer;text-decoration:none}.primary{background:var(--acc);border-color:var(--acc);color:#fff}
.row{display:flex;gap:8px;align-items:center;justify-content:space-between;margin-top:14px;flex-wrap:wrap}
.code{font:600 28px/1 ui-monospace,monospace;letter-spacing:3px;text-align:center;padding:14px;border:1px dashed var(--line);border-radius:10px}
.chk{display:flex;gap:8px;align-items:center;margin-top:10px}.chk input{width:auto}
table{width:100%;border-collapse:collapse;font-size:13px}td{padding:6px 0;border-top:1px solid var(--line)}
"""


def page(title: str, body: str) -> bytes:
    return (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body><main>{body}</main></body></html>").encode()


def esc(v) -> str:
    return html.escape(str(v), quote=True)


def settings_form(s: dict, csrf: str) -> str:
    prov = s.get("provider", "anthropic")
    key_note = "A key is saved. Leave empty to keep it." if s.get("api_key") else "No key saved yet."
    opts = "".join(f"<option value={v}{' selected' if prov == v else ''}>{t}</option>"
                   for v, t in (("anthropic", "Anthropic format (official API or messages gateway)"),
                                ("openai", "OpenAI format (chat completions gateway)")))
    efforts = "".join(f"<option{' selected' if s.get('effort', 'high') == e else ''}>{e}</option>"
                      for e in ("low", "medium", "high", "xhigh", "max"))
    langs = "".join(f"<option value={v}{' selected' if s.get('language', 'english') == v else ''}>{t}</option>"
                    for v, t in (("english", "English"), ("hinglish", "Hinglish (simple Hindi + English)")))
    return f"""<form method=post action=/settings class=card><h2>Settings</h2>
<input type=hidden name=csrf value="{esc(csrf)}">
<label>API format</label><select name=provider>{opts}</select>
<label>Base URL (empty = official Anthropic API)</label><input name=base_url value="{esc(s.get('base_url', ''))}" placeholder="https://openrouter.ai/api/v1">
<label>Model</label><input name=model value="{esc(s.get('model', _DEFAULTS.model))}">
<label>API key</label><input name=api_key type=password autocomplete=off placeholder="{esc(key_note)}">
<label>Effort</label><select name=effort>{efforts}</select>
<label>Reply language</label><select name=language>{langs}</select>
<label>Reasoning effort (OpenAI format only, empty = don't send)</label><input name=reasoning_effort value="{esc(s.get('reasoning_effort', ''))}">
<label>Price per 1M tokens, input / output (USD, for gateway models; 0 = unknown)</label>
<div class=row style=margin-top:0><input name=price_input value="{esc(s.get('price_input', 0))}" style=flex:1><input name=price_output value="{esc(s.get('price_output', 0))}" style=flex:1></div>
<input type=hidden name=compat_mode value=false><label class=chk><input type=checkbox name=compat_mode value=true{' checked' if s.get('compat_mode') else ''}> Compatibility mode (basic request fields only)</label>
<input type=hidden name=web_search value=false><label class=chk><input type=checkbox name=web_search value=true{' checked' if s.get('web_search') else ''}> Web search tool</label>
<div class=row><span class=mut>Encrypted on this server.</span><button class=primary>Save</button></div></form>"""


def logins_table(logins: list[dict], csrf: str) -> str:
    if not logins:
        return "<div class=card><h2>Signed-in apps</h2><p class=mut>None yet. Run <code>anvil login</code> on a machine.</p></div>"
    rows = "".join(
        f"<tr><td>{esc(l['label'] or 'unnamed')}<br><span class=mut>since {time.strftime('%Y-%m-%d', time.localtime(l['created']))}"
        f"{', last used ' + time.strftime('%Y-%m-%d %H:%M', time.localtime(l['last_used'])) if l['last_used'] else ''}</span></td>"
        f"<td style=text-align:right><form method=post action=/logins/revoke><input type=hidden name=csrf value='{esc(csrf)}'>"
        f"<input type=hidden name=id value='{esc(l['id'])}'><button>Sign out</button></form></td></tr>" for l in logins)
    return f"<div class=card><h2>Signed-in apps</h2><table>{rows}</table></div>"


# --- HTTP -------------------------------------------------------------------

def make_handler(app: CloudApp):
    cfg, store = app.cfg, app.store

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "anvil-cloud"
        sys_version = ""

        def log_message(self, fmt, *args):
            pass

        # helpers ------------------------------------------------------------
        def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            # Not "no-referrer": with it, browsers send "Origin: null" on form posts and the
            # same-origin check below would refuse every form.
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
            if app.secure_cookie:
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            for k, v in (headers or {}).items():
                if isinstance(v, list):
                    for item in v:
                        self.send_header(k, item)
                else:
                    self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data).encode(), "application/json")

        def _html(self, title: str, body: str, code: int = 200, headers: dict | None = None) -> None:
            self._send(code, page(title, body), "text/html; charset=utf-8", headers)

        def _redirect(self, location: str, headers: dict | None = None) -> None:
            h = {"Location": location}
            h.update(headers or {})
            self._send(303, b"", "text/plain", h)

        def _raw_body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self.close_connection = True
                raise ValueError("request body too large")
            return self.rfile.read(n) if n > 0 else b""

        def _form(self) -> dict:
            return {k: v[-1] for k, v in urllib.parse.parse_qs(self._raw_body().decode("utf-8", "replace")).items()}

        def _jbody(self) -> dict:
            raw = self._raw_body()
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                raise ValueError("invalid JSON")
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            return data

        def _cookie(self, headers: dict, value: str, max_age: int) -> None:
            attrs = f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
            headers.setdefault("Set-Cookie", []).append(attrs + ("; Secure" if app.secure_cookie else ""))

        def _web_user(self) -> tuple[str | None, str]:
            """(email, CSRF token) for the browser session, or (None, "")."""
            c = SimpleCookie()
            try:
                c.load(self.headers.get("Cookie") or "")
            except Exception:
                return None, ""
            tok = c[COOKIE].value if COOKIE in c else ""
            email = store.check(tok, "web")
            if not email or not app.allowed(email):
                return None, ""
            return email, store.csrf_for(tok)

        def _bearer(self) -> str | None:
            auth = self.headers.get("Authorization") or ""
            tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            email = store.check(tok, "access")
            return email if email and app.allowed(email) else None

        def _origin_ok(self) -> bool:
            # Browsers say whether a request came from this site's own pages. Behind a proxy the
            # public URL may differ in small ways (port, trailing slash), so trust this first.
            site = (self.headers.get("Sec-Fetch-Site") or "").lower()
            if site:
                return site in ("same-origin", "none")
            origin = self.headers.get("Origin")
            return not origin or origin.rstrip("/") == cfg.public_url

        def _csrf_ok(self, form: dict, csrf: str) -> bool:
            return bool(csrf) and self._origin_ok() and secrets.compare_digest(form.get("csrf", ""), csrf)

        def _client(self) -> str:
            return self.client_address[0]

        # routes ---------------------------------------------------------------
        def do_HEAD(self):
            self.do_GET()

        def _chat(self, method: str) -> bool:
            """Hand /app requests to the browser chat. True if this request was one."""
            from .chat import PREFIX, public_path
            path = urllib.parse.urlparse(self.path).path
            if not app.chat or not (path == PREFIX or path.startswith(PREFIX + "/")):
                return False
            email, csrf = self._web_user()
            if method in ("GET", "HEAD") and public_path(path):
                app.chat.handle(self, method, email or "", csrf)  # no settings or state in these
                return True
            if method in ("GET", "HEAD") and path in (PREFIX, PREFIX + "/", PREFIX + "/index.html"):
                if not email:
                    self._redirect("/login?next=" + urllib.parse.quote(PREFIX + "/", safe=""))
                    return True
                if path == PREFIX:
                    self._redirect(PREFIX + "/")
                    return True
            if not email:
                if method not in ("GET", "HEAD"):
                    self.close_connection = True
                self._json({"error": "unauthorized"}, 401)
                return True
            if not app.chat.allowed(email):
                self.close_connection = True
                self._json({"error": f"{email} can't use chat on this server"}, 403)
                return True
            app.chat.handle(self, method, email, csrf)
            return True

        def do_DELETE(self):
            if not self._chat("DELETE"):
                self._json({"error": "not found"}, 404)

        def do_GET(self):
            if self._chat(self.command):
                return
            url = urllib.parse.urlparse(self.path)
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
            if url.path == "/healthz":
                return self._json({"ok": True})
            if url.path == "/login":
                return self._start_google(q.get("next", "/"))
            if url.path == "/auth/callback":
                return self._google_callback(q)
            if url.path == "/api/me":
                email = self._bearer()
                return self._json({"email": email, "name": store.user_name(email)}) if email \
                    else self._json({"error": "unauthorized"}, 401)
            if url.path == "/api/settings":
                email = self._bearer()
                if not email:
                    return self._json({"error": "unauthorized"}, 401)
                return self._json({"settings": store.get_settings(email), "synced_fields": list(SYNCED)})
            if url.path == "/device":
                return self._device_page(q.get("code", ""))
            if url.path == "/":
                return self._home(q.get("saved"))
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            if self._chat("POST"):
                return
            url = urllib.parse.urlparse(self.path)
            try:
                if url.path.startswith("/api/"):
                    return self._api_post(url.path)
                form = self._form()
                email, csrf = self._web_user()
                if not email:
                    return self._redirect("/login")
                if not self._csrf_ok(form, csrf):
                    return self._html("Anvil", "<div class=card><h2>That form expired.</h2><p><a href=/>Start again</a></p></div>", 403)
                if url.path == "/settings":
                    cur = store.get_settings(email)
                    new = clean_settings(form)
                    if not new.get("api_key"):
                        new["api_key"] = cur.get("api_key", "")
                    cur.update(new)
                    store.put_settings(email, cur)
                    return self._redirect("/?saved=1")
                if url.path == "/device":
                    approve = form.get("action") == "approve"
                    ok = store.decide_device(form.get("user_code", ""), email, approve)
                    if not ok:
                        return self._html("Anvil", "<div class=card><h2>Code not found or expired.</h2>"
                                          "<p>Run <code>anvil login</code> again for a new code.</p></div>", 400)
                    msg = "Signed in. You can close this tab and go back to your terminal or app." if approve \
                        else "Request denied. Nothing was signed in."
                    return self._html("Anvil", f"<div class=card><h2>{msg}</h2></div>")
                if url.path == "/logins/revoke":
                    store.revoke_refresh(form.get("id", ""), email)
                    return self._redirect("/")
                if url.path == "/logout":
                    c = SimpleCookie()
                    c.load(self.headers.get("Cookie") or "")
                    if COOKIE in c:
                        store.revoke(c[COOKIE].value)
                    h: dict = {}
                    self._cookie(h, "", 0)
                    return self._redirect("/", h)
                self._json({"error": "not found"}, 404)
            except ValueError as e:
                self._json({"error": str(e)}, 400)

        def do_PUT(self):
            url = urllib.parse.urlparse(self.path)
            try:
                if url.path != "/api/settings":
                    return self._json({"error": "not found"}, 404)
                email = self._bearer()
                if not email:
                    return self._json({"error": "unauthorized"}, 401)
                data = self._jbody()
                cur = store.get_settings(email)
                new = clean_settings({**data, "_provider_hint": data.get("provider", cur.get("provider"))})
                if not new.get("api_key"):
                    new.pop("api_key", None)  # empty means "keep the saved key"
                cur.update(new)
                store.put_settings(email, cur)
                self._json({"settings": cur})
            except ValueError as e:
                self._json({"error": str(e)}, 400)

        def _api_post(self, path: str):
            if not app.limiter.allow(self._client()):
                return self._json({"error": "slow_down"}, 429)
            data = self._jbody()
            if path == "/api/device/code":
                d = store.new_device(str(data.get("label") or "")[:80])
                d["verification_uri"] = cfg.public_url + "/device"
                d["verification_uri_complete"] = f"{cfg.public_url}/device?code={d['user_code']}"
                return self._json(d)
            if path == "/api/device/token":
                status, tokens = store.poll_device(str(data.get("device_code") or ""))
                if status == "ok":
                    return self._json(tokens)
                return self._json({"error": status, "interval": DEVICE_INTERVAL}, 400)
            if path == "/api/token/refresh":
                out = store.refresh(str(data.get("refresh_token") or ""))
                if not out or not app.allowed(out["email"]):
                    return self._json({"error": "invalid_grant"}, 401)
                return self._json(out)
            if path == "/api/token/revoke":
                store.revoke(str(data.get("refresh_token") or ""))
                return self._json({"ok": True})
            self._json({"error": "not found"}, 404)

        # pages ----------------------------------------------------------------
        def _home(self, saved):
            email, csrf = self._web_user()
            if not email:
                return self._html("Anvil", "<div class=card><h1>Anvil</h1><p class=mut>Your Anvil settings, in one place.</p>"
                                  "<div class=row><a class='btn primary' href=/login>Sign in with Google</a></div></div>")
            note = "<p class=mut style='color:var(--acc)'>Saved. Apps pick this up next time they start.</p>" if saved else ""
            head = (f"<div class=card><div class=row style=margin:0><div><h1>Anvil</h1><p class=mut>Signed in as {esc(email)}</p></div>"
                    f"<form method=post action=/logout><input type=hidden name=csrf value='{esc(csrf)}'><button>Sign out</button></form></div>{note}"
                    f"<p class=mut>On a new machine: <code>anvil login {esc(cfg.public_url)}</code></p>"
                    + ("<div class=row><a class='btn primary' href=/app/>Open chat</a></div>"
                       if app.chat and app.chat.allowed(email) else "") + "</div>")
            self._html("Anvil", head + settings_form(store.get_settings(email), csrf) + logins_table(store.list_logins(email), csrf))

        def _device_page(self, code: str):
            email, csrf = self._web_user()
            if not email:
                nxt = "/device" + (f"?code={urllib.parse.quote(code)}" if code else "")
                return self._redirect("/login?next=" + urllib.parse.quote(nxt, safe=""))
            dev = store.device_by_user_code(code) if code else None
            if not dev:
                bad = "<p class=mut style='color:var(--acc)'>That code wasn't found or has expired.</p>" if code else ""
                return self._html("Anvil sign-in", f"<form method=get action=/device class=card><h2>Sign in an app</h2>{bad}"
                                  "<label>Code shown in your terminal or app</label><input name=code autofocus placeholder=XXXX-XXXX>"
                                  "<div class=row><span></span><button class=primary>Continue</button></div></form>")
            who = esc(dev["label"]) if dev["label"] else "this app"
            self._html("Anvil sign-in", f"""<form method=post action=/device class=card><h2>Let {who} sign in?</h2>
<p>Check that this code matches the one on your screen:</p><p class=code>{esc(dev['user_code'])}</p>
<p class=mut>Approving gives that app your Anvil settings, including your API key, as {esc(email)}.
If you didn't just run <code>anvil login</code>, deny.</p>
<input type=hidden name=csrf value="{esc(csrf)}"><input type=hidden name=user_code value="{esc(dev['user_code'])}">
<div class=row><button name=action value=deny>Deny</button><button class=primary name=action value=approve>Approve</button></div></form>""")

        def _start_google(self, next_path: str):
            if not next_path.startswith("/") or next_path.startswith("//"):
                next_path = "/"
            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            state = store.new_oauth_state(verifier, next_path)
            params = {"client_id": cfg.google_client_id, "redirect_uri": cfg.public_url + "/auth/callback",
                      "response_type": "code", "scope": "openid email profile", "state": state,
                      "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account"}
            h: dict = {}
            # Bind the state to this browser so a login link from someone else can't sign you in as them.
            h.setdefault("Set-Cookie", []).append(
                f"anvil_state={state}; Path=/auth; HttpOnly; SameSite=Lax; Max-Age=600" + ("; Secure" if app.secure_cookie else ""))
            self._redirect(GOOGLE_AUTH + "?" + urllib.parse.urlencode(params), h)

        def _google_callback(self, q: dict):
            c = SimpleCookie()
            try:
                c.load(self.headers.get("Cookie") or "")
            except Exception:
                pass
            state = q.get("state", "")
            bound = c["anvil_state"].value if "anvil_state" in c else ""
            saved = store.take_oauth_state(state) if state else None
            if not saved or not bound or not secrets.compare_digest(bound, state):
                return self._html("Anvil", "<div class=card><h2>Sign-in expired.</h2><p><a href=/login>Try again</a></p></div>", 400)
            if q.get("error") or not q.get("code"):
                return self._html("Anvil", "<div class=card><h2>Google sign-in was cancelled.</h2><p><a href=/login>Try again</a></p></div>", 400)
            verifier, next_path = saved
            try:
                user = app.exchange(q["code"], verifier)
            except (urllib.error.URLError, ValueError, KeyError, json.JSONDecodeError) as e:
                return self._html("Anvil", f"<div class=card><h2>Sign-in failed.</h2><p class=mut>{esc(e)}</p></div>", 400)
            if not app.allowed(user["email"]):
                return self._html("Anvil", f"<div class=card><h2>{esc(user['email'])} isn't allowed on this server.</h2>"
                                  "<p class=mut>Add it to allowed_emails in the server config.</p></div>", 403)
            store.upsert_user(user["email"], user.get("name", ""))
            tok, _ = store.new_web_session(user["email"])
            h: dict = {}
            self._cookie(h, tok, 12 * 3600)
            h["Set-Cookie"].append("anvil_state=; Path=/auth; Max-Age=0")
            self._redirect(next_path, h)

    return Handler


def serve(cfg: CloudConfig, app: CloudApp | None = None) -> ThreadingHTTPServer:
    app = app or CloudApp(cfg)
    srv = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(app))
    srv.daemon_threads = True

    def sweep():
        while True:
            time.sleep(600)
            try:
                app.store.cleanup()
            except Exception:
                pass
    threading.Thread(target=sweep, daemon=True).start()
    return srv
