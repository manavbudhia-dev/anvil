"""Client side of Anvil cloud: `anvil login`, and fetching settings at startup.

Only the server URL, your email and a refresh token are saved on this machine
(~/.config/anvil/cloud.json, readable only by you). The API key and other synced settings are
fetched at startup and kept in memory; they are never written to config.json.
"""

import json
import os
import platform
import socket
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from ..config import CONFIG_DIR, Config
from ..fsutil import atomic_write_text, read_text

CRED_FILE = CONFIG_DIR / "cloud.json"
TIMEOUT = 15


class CloudError(RuntimeError):
    pass


class NotLoggedIn(CloudError):
    pass


def _request(method: str, url: str, data: dict | None = None, token: str | None = None) -> tuple[int, dict]:
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Accept": "application/json", "User-Agent": "anvil"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {"error": f"HTTP {e.code}"}
    except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
        raise CloudError(f"couldn't reach {url.split('/api/')[0]}: {getattr(e, 'reason', e)}") from e
    except json.JSONDecodeError as e:
        raise CloudError(f"{url} didn't answer like an Anvil cloud server") from e


def normalize_server(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "https://" + url
    if url.startswith("http://") and not url.startswith(("http://127.0.0.1", "http://localhost")):
        raise CloudError("use an https:// address; your API key would travel unencrypted over http")
    return url


def load_creds() -> dict | None:
    try:
        d = json.loads(read_text(CRED_FILE))
        return d if d.get("server") and d.get("refresh_token") else None
    except (OSError, json.JSONDecodeError):
        return None


def save_creds(server: str, refresh_token: str, email: str) -> None:
    atomic_write_text(CRED_FILE, json.dumps({"server": server, "email": email, "refresh_token": refresh_token},
                                            indent=2), private=True)


def default_label() -> str:
    return f"{socket.gethostname() or 'computer'} ({platform.system() or 'unknown'})"[:80]


def start_device_login(server: str, label: str | None = None) -> dict:
    server = normalize_server(server)
    code, d = _request("POST", server + "/api/device/code", {"label": label or default_label()})
    if code != 200 or "device_code" not in d:
        raise CloudError(d.get("error") or f"server answered {code}")
    d["server"] = server
    return d


def poll_device_login(start: dict, sleep=time.sleep, on_wait=None) -> str:
    """Wait until the code is approved in the browser; saves credentials and returns the email."""
    interval = max(1, int(start.get("interval", 3)))
    deadline = time.monotonic() + int(start.get("expires_in", 600))
    while time.monotonic() < deadline:
        sleep(interval)
        code, d = _request("POST", start["server"] + "/api/device/token", {"device_code": start["device_code"]})
        if code == 200 and d.get("refresh_token"):
            save_creds(start["server"], d["refresh_token"], d.get("email", ""))
            _ACCESS.clear()
            return d.get("email", "")
        err = d.get("error")
        if err == "slow_down":
            interval += 2
        elif err == "access_denied":
            raise CloudError("sign-in was denied in the browser")
        elif err == "expired_token":
            raise CloudError("the code expired; run anvil login again")
        elif err != "authorization_pending" and code != 429:
            raise CloudError(err or f"server answered {code}")
        if on_wait:
            on_wait()
    raise CloudError("the code expired; run anvil login again")


def login(server: str, say=print, open_browser=True) -> str:
    start = start_device_login(server)
    say(f"\nOpen {start['verification_uri']} and confirm this code:\n\n    {start['user_code']}\n")
    if open_browser and not os.environ.get("SSH_CONNECTION"):
        try:
            webbrowser.open(start["verification_uri_complete"])
        except Exception:
            pass
    say("Waiting for you to approve in the browser (Ctrl+C to cancel)...")
    return poll_device_login(start)


def logout() -> bool:
    creds = load_creds()
    _ACCESS.clear()
    if not creds:
        return False
    try:
        _request("POST", creds["server"] + "/api/token/revoke", {"refresh_token": creds["refresh_token"]})
    except CloudError:
        pass  # still forget it locally
    try:
        Path(CRED_FILE).unlink()
    except OSError:
        pass
    return True


_ACCESS: dict = {}  # server -> (token, expires_at), in memory only


def _access_token(creds: dict, force: bool = False) -> str:
    cached = _ACCESS.get(creds["server"])
    if cached and not force and cached[1] > time.time() + 60:
        return cached[0]
    code, d = _request("POST", creds["server"] + "/api/token/refresh", {"refresh_token": creds["refresh_token"]})
    if code == 401:
        raise NotLoggedIn("your Anvil cloud sign-in expired or was revoked; run anvil login again")
    if code != 200 or "access_token" not in d:
        raise CloudError(d.get("error") or f"server answered {code}")
    _ACCESS[creds["server"]] = (d["access_token"], time.time() + int(d.get("expires_in", 3600)))
    return d["access_token"]


def _authed(method: str, path: str, data: dict | None = None) -> dict:
    creds = load_creds()
    if not creds:
        raise NotLoggedIn("not signed in; run anvil login <server>")
    for attempt in (0, 1):
        code, d = _request(method, creds["server"] + path, data, _access_token(creds, force=attempt == 1))
        if code == 401 and attempt == 0:
            continue
        if code == 401:
            raise NotLoggedIn("this account isn't allowed on the server any more; run anvil login again")
        if code != 200:
            raise CloudError(d.get("error") or f"server answered {code}")
        return d
    raise AssertionError("unreachable")


def fetch_settings() -> dict:
    return _authed("GET", "/api/settings").get("settings", {})


def push_settings(settings: dict) -> dict:
    return _authed("PUT", "/api/settings", settings).get("settings", {})


def whoami() -> dict:
    return _authed("GET", "/api/me")


def apply_settings(config: Config, settings: dict) -> None:
    """Overlay cloud settings on a loaded Config without letting save() write them to disk."""
    from .server import SYNCED
    local = getattr(config, "_cloud_local", None) or {}
    for k in SYNCED:
        if k in settings and hasattr(config, k):
            local.setdefault(k, getattr(config, k))
            setattr(config, k, settings[k])
    config._cloud_local = local


def load_cloud_settings(config: Config, notice=None) -> str | None:
    """At startup: fetch and apply cloud settings if this machine is signed in.

    Returns the signed-in email, or None if not signed in / disabled. If the server can't be
    reached, the local settings are used and `notice` is told why.
    """
    if os.environ.get("ANVIL_CLOUD", "").lower() in ("0", "off", "false", "no"):
        return None
    creds = load_creds()
    if not creds:
        return None
    try:
        apply_settings(config, fetch_settings())
        config._cloud_email = creds.get("email", "")
        return config._cloud_email
    except CloudError as e:
        if notice:
            notice(f"Anvil cloud: {e}. Using this machine's local settings.")
        return None
