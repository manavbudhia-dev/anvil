"""MCP OAuth for remote connectors: discovery, dynamic client registration, PKCE, refresh.

Flow (MCP authorization spec): the server answers 401 → read its protected-resource metadata →
find the authorization server → register a client (unless one is configured, e.g. Slack's
pre-registered apps) → open the browser on the authorize URL → a one-shot listener on
http://localhost:<port>/callback receives the code → exchange it for tokens.

The callback port is fixed (default 33418) so pre-registered apps can whitelist the redirect URL.
"""

import base64
import hashlib
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import json

from .mcp import http_request

DEFAULT_PORT = 33418
FLOW_TIMEOUT = 300


class OAuthError(Exception):
    pass


def redirect_uri(port: int = DEFAULT_PORT) -> str:
    return f"http://localhost:{port}/callback"


def _get_json(url: str) -> dict | None:
    try:
        r = http_request("GET", url, headers={"Accept": "application/json", "MCP-Protocol-Version": "2025-06-18",
                                              "User-Agent": "anvil-mcp"}, timeout=20)
        try:
            if r.status_code == 200:
                data = json.loads(r.read())
                return data if isinstance(data, dict) else None
        finally:
            r.close()
    except (OSError, ValueError):
        pass
    return None


def _post(url: str, body: bytes, headers: dict) -> tuple[int, dict, str]:
    try:
        r = http_request("POST", url, body, {"Accept": "application/json", "User-Agent": "anvil-mcp", **headers}, 30)
    except OSError as e:
        raise OAuthError(f"couldn't reach {url}: {e}") from e
    try:
        text = r.read().decode(errors="replace")
    finally:
        r.close()
    try:
        data = json.loads(text)
    except ValueError:
        data = {}
    return r.status_code, data if isinstance(data, dict) else {}, text


def _well_known(base: str, name: str) -> list[str]:
    u = urlparse(base)
    origin = f"{u.scheme}://{u.netloc}"
    path = u.path.rstrip("/")
    urls = [f"{origin}/.well-known/{name}{path}"] if path else []
    urls.append(f"{origin}/.well-known/{name}")
    return urls


def discover(server_url: str, www_authenticate: str = "") -> dict:
    """Return {"resource", "authorization_endpoint", "token_endpoint", "registration_endpoint", "scopes"}."""
    prm = None
    m = re.search(r'resource_metadata="([^"]+)"', www_authenticate or "")
    if m:
        prm = _get_json(m.group(1))
    if prm is None:
        for url in _well_known(server_url, "oauth-protected-resource"):
            prm = _get_json(url)
            if prm:
                break
    u = urlparse(server_url)
    auth_server = ((prm or {}).get("authorization_servers") or [f"{u.scheme}://{u.netloc}"])[0]
    meta = None
    for url in _well_known(auth_server, "oauth-authorization-server") + _well_known(auth_server, "openid-configuration"):
        meta = _get_json(url)
        if meta and meta.get("authorization_endpoint"):
            break
    if not meta or not meta.get("authorization_endpoint") or not meta.get("token_endpoint"):
        raise OAuthError(f"couldn't find OAuth metadata for {server_url}; this server may need a token instead")
    scope_m = re.search(r'scope="([^"]+)"', www_authenticate or "")
    return {
        "resource": (prm or {}).get("resource") or server_url,
        "authorization_endpoint": meta["authorization_endpoint"],
        "token_endpoint": meta["token_endpoint"],
        "registration_endpoint": meta.get("registration_endpoint", ""),
        "auth_methods": meta.get("token_endpoint_auth_methods_supported") or [],
        "scopes": scope_m.group(1) if scope_m else " ".join((prm or {}).get("scopes_supported") or []),
    }


def register(registration_endpoint: str, redirect: str) -> dict:
    status, data, text = _post(registration_endpoint, json.dumps({
        "client_name": "Anvil", "redirect_uris": [redirect], "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none"}).encode(), {"Content-Type": "application/json"})
    if status >= 400:
        raise OAuthError(f"client registration failed ({status}): {text[:300]}")
    if not data.get("client_id"):
        raise OAuthError("client registration returned no client_id")
    return {"client_id": data["client_id"], "client_secret": data.get("client_secret", "")}


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _token_request(meta: dict, client_id: str, client_secret: str, form: dict) -> dict:
    form = {**form, "client_id": client_id}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if client_secret:
        methods = meta.get("auth_methods", [])
        if "client_secret_basic" in methods and "client_secret_post" not in methods:
            raw = f"{client_id}:{client_secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        else:
            form["client_secret"] = client_secret
    status, data, text = _post(meta["token_endpoint"], urlencode(form).encode(), headers)
    if status >= 400 or "access_token" not in data:
        raise OAuthError(f"token request failed ({status}): {data.get('error_description') or data.get('error') or text[:300]}")
    out = {"access_token": data["access_token"], "token_type": data.get("token_type", "Bearer")}
    if data.get("refresh_token"):
        out["refresh_token"] = data["refresh_token"]
    if data.get("expires_in"):
        out["expires_at"] = time.time() + float(data["expires_in"])
    return out


class Flow:
    """One sign-in attempt. start() returns the URL to open; wait() blocks until the browser comes back."""

    def __init__(self, server_url: str, www_authenticate: str = "", client_id: str = "", client_secret: str = "",
                 scope: str = "", port: int = DEFAULT_PORT):
        self.server_url, self.www_auth = server_url, www_authenticate
        self.client_id, self.client_secret, self.scope, self.port = client_id, client_secret, scope, port
        self.meta: dict = {}
        self.state = secrets.token_urlsafe(24)
        self.verifier = ""
        self._result: dict = {}
        self._done = threading.Event()
        self._server: HTTPServer | None = None

    def start(self) -> str:
        self.meta = discover(self.server_url, self.www_auth)
        redirect = redirect_uri(self.port)
        if not self.client_id:
            if not self.meta["registration_endpoint"]:
                raise OAuthError("this server doesn't support automatic app registration; set a client ID for it")
            reg = register(self.meta["registration_endpoint"], redirect)
            self.client_id, self.client_secret = reg["client_id"], reg["client_secret"]
        self.verifier, challenge = _pkce()
        params = {"response_type": "code", "client_id": self.client_id, "redirect_uri": redirect, "state": self.state,
                  "code_challenge": challenge, "code_challenge_method": "S256", "resource": self.meta["resource"]}
        scope = self.scope or self.meta.get("scopes", "")
        if scope:
            params["scope"] = scope
        self._listen()
        sep = "&" if "?" in self.meta["authorization_endpoint"] else "?"
        return self.meta["authorization_endpoint"] + sep + urlencode(params)

    def _listen(self) -> None:
        flow = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urlparse(self.path)
                if u.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                ok = q.get("state") == flow.state and "code" in q
                if ok:
                    flow._result = {"code": q["code"]}
                else:
                    flow._result = {"error": q.get("error_description") or q.get("error") or "state mismatch"}
                body = ("<h3>Anvil: signed in. You can close this tab.</h3>" if ok
                        else f"<h3>Anvil: sign-in failed</h3><p>{flow._result['error'][:200]}</p>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                flow._done.set()

        try:
            self._server = HTTPServer(("127.0.0.1", self.port), Handler)
        except OSError as e:
            raise OAuthError(f"port {self.port} is busy ({e}); finish or cancel the other sign-in first") from e
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        srv = self._server
        srv.timeout = 1
        deadline = time.time() + FLOW_TIMEOUT
        while not self._done.is_set() and time.time() < deadline:
            srv.handle_request()
        srv.server_close()
        self._done.set()

    def wait(self, timeout: float = FLOW_TIMEOUT) -> dict:
        """Return the stored credential bundle (tokens + client + endpoints)."""
        self._done.wait(timeout)
        if "code" not in self._result:
            raise OAuthError(self._result.get("error") or "sign-in timed out")
        tokens = _token_request(self.meta, self.client_id, self.client_secret, {
            "grant_type": "authorization_code", "code": self._result["code"], "redirect_uri": redirect_uri(self.port),
            "code_verifier": self.verifier, "resource": self.meta["resource"]})
        return {**tokens, "client_id": self.client_id, "client_secret": self.client_secret,
                "token_endpoint": self.meta["token_endpoint"], "auth_methods": self.meta.get("auth_methods", []),
                "resource": self.meta["resource"]}


def refresh(bundle: dict) -> dict:
    """Use the refresh token; returns the updated bundle. Providers that rotate refresh tokens
    (Notion) return a new one, which replaces the old."""
    if not bundle.get("refresh_token"):
        raise OAuthError("the sign-in expired; sign in again")
    form = {"grant_type": "refresh_token", "refresh_token": bundle["refresh_token"]}
    if bundle.get("resource"):
        form["resource"] = bundle["resource"]
    tokens = _token_request(bundle, bundle["client_id"], bundle.get("client_secret", ""), form)
    return {**bundle, **tokens}
