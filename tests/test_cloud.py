import http.client
import json
import os
import re
import threading
import urllib.parse

import pytest

pytest.importorskip("cryptography")

from anvil.cloud import client  # noqa: E402
from anvil.cloud.server import CloudApp, CloudConfig, clean_settings, make_handler  # noqa: E402
from anvil.cloud.store import Store, load_master_key  # noqa: E402
from anvil.config import CONFIG_FILE, Config  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

OWNER = "manav@example.com"


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "db.sqlite", load_master_key(tmp_path / "master.key"), clock=Clock())


def test_settings_are_encrypted_at_rest_and_tokens_hashed(store, tmp_path):
    store.put_settings(OWNER, {"api_key": "sk-secret-123", "model": "m"})
    pair = store.issue_pair(OWNER, "laptop")
    raw = (tmp_path / "db.sqlite").read_bytes() + (tmp_path / "db.sqlite-wal").read_bytes()
    assert b"sk-secret-123" not in raw
    assert pair["refresh_token"].encode() not in raw and pair["access_token"].encode() not in raw
    assert store.get_settings(OWNER)["api_key"] == "sk-secret-123"
    assert os.name == "nt" or (tmp_path / "master.key").stat().st_mode & 0o077 == 0  # no POSIX modes on Windows


def test_tokens_expire_refresh_and_revoke(store):
    pair = store.issue_pair(OWNER, "laptop")
    assert store.check(pair["access_token"], "access") == OWNER
    assert store.check(pair["access_token"], "refresh") is None  # kinds don't mix
    store.clock.t += 3601
    assert store.check(pair["access_token"], "access") is None
    new = store.refresh(pair["refresh_token"])
    assert store.check(new["access_token"], "access") == OWNER
    store.revoke(pair["refresh_token"])
    assert store.refresh(pair["refresh_token"]) is None
    assert store.check(new["access_token"], "access") is None  # child access tokens die too


def test_device_flow_states(store):
    d = store.new_device("laptop")
    assert store.poll_device(d["device_code"])[0] == "authorization_pending"
    assert store.poll_device(d["device_code"])[0] == "slow_down"
    assert store.decide_device(d["user_code"].lower().replace("-", ""), OWNER, True)
    status, tokens = store.poll_device(d["device_code"])
    assert status == "ok" and tokens["email"] == OWNER
    assert store.poll_device(d["device_code"])[0] == "expired_token"  # single use
    d2 = store.new_device("x")
    store.decide_device(d2["user_code"], None, False)
    assert store.poll_device(d2["device_code"])[0] == "access_denied"
    d3 = store.new_device("x")
    store.clock.t += 601
    assert store.poll_device(d3["device_code"])[0] == "expired_token"
    assert not store.decide_device(d3["user_code"], OWNER, True)


def test_clean_settings_drops_unknown_fields_and_coerces():
    out = clean_settings({"provider": "anthropic", "base_url": "https://gw/v1/", "max_tokens": "100",
                          "compat_mode": "true", "permission_ceiling": "auto", "last_root": "/x"})
    assert out == {"provider": "anthropic", "base_url": "https://gw", "max_tokens": 100, "compat_mode": True}


# --- end to end ----------------------------------------------------------------

class Browser:
    """Tiny cookie-keeping HTTP client that doesn't follow redirects."""

    def __init__(self, port):
        self.port, self.cookies = port, {}

    def req(self, method, path, form=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form)
            h["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        data = r.read().decode()
        for c in r.headers.get_all("Set-Cookie") or []:
            k, v = c.split(";")[0].split("=", 1)
            if v:
                self.cookies[k] = v
            else:
                self.cookies.pop(k, None)
        conn.close()
        return r.status, r.headers, data

    def sign_in(self, email=OWNER):
        st, h, _ = self.req("GET", "/login?next=%2F")
        assert st == 303
        loc = urllib.parse.urlparse(h["Location"])
        q = urllib.parse.parse_qs(loc.query)
        assert loc.netloc == "accounts.google.com" and q["code_challenge_method"] == ["S256"]
        st, h, body = self.req("GET", f"/auth/callback?state={q['state'][0]}&code={email}")
        return st, h, body


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), lambda *a: None)
    port = srv.server_address[1]
    cfg = CloudConfig(public_url=f"http://127.0.0.1:{port}", google_client_id="cid", google_client_secret="sec",
                      allowed_emails=[OWNER])
    store = Store(tmp_path / "srv" / "db.sqlite", load_master_key(tmp_path / "srv" / "master.key"))
    app = CloudApp(cfg, store, exchange=lambda code, verifier: {"email": code, "name": "Manav"})
    srv.RequestHandlerClass = make_handler(app)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    monkeypatch.setattr(client, "CRED_FILE", tmp_path / "cloud.json")
    client._ACCESS.clear()
    yield app, port
    srv.shutdown()
    srv.server_close()


def csrf_of(html):
    return re.search(r"name=csrf value=['\"]([0-9a-f]+)", html).group(1)


def test_google_login_and_web_settings(cloud):
    app, port = cloud
    b = Browser(port)
    st, h, _ = b.sign_in()
    assert st == 303 and h["Location"] == "/" and "anvil_cloud" in b.cookies
    st, _, page = b.req("GET", "/")
    assert OWNER in page
    csrf = csrf_of(page)
    form = {"csrf": csrf, "provider": "openai", "base_url": "https://openrouter.ai/api/v1", "model": "x/y",
            "api_key": "sk-1", "effort": "high", "compat_mode": "false"}
    assert b.req("POST", "/settings", form)[0] == 303
    assert app.store.get_settings(OWNER)["api_key"] == "sk-1"
    # Empty key keeps the saved one; missing CSRF or a foreign Origin is refused.
    b.req("POST", "/settings", {**form, "api_key": "", "model": "z"})
    assert app.store.get_settings(OWNER)["api_key"] == "sk-1" and app.store.get_settings(OWNER)["model"] == "z"
    assert b.req("POST", "/settings", {**form, "csrf": "bad"})[0] == 403
    assert b.req("POST", "/settings", form, {"Origin": "https://evil.example"})[0] == 403
    assert "sk-1" not in b.req("GET", "/")[2]  # the key is never echoed into the page


def test_login_rejects_unlisted_email_and_unbound_state(cloud):
    _, port = cloud
    st, _, body = Browser(port).sign_in("stranger@example.com")
    assert st == 403 and "isn't allowed" in body
    b = Browser(port)
    st, h, _ = b.req("GET", "/login")
    state = urllib.parse.parse_qs(urllib.parse.urlparse(h["Location"]).query)["state"][0]
    other = Browser(port)  # a different browser can't finish this sign-in
    assert other.req("GET", f"/auth/callback?state={state}&code={OWNER}")[0] == 400


def test_anvil_login_device_flow_fetches_settings(cloud, monkeypatch):
    app, port = cloud
    app.store.put_settings(OWNER, {"provider": "openai", "base_url": "https://gw/v1", "api_key": "sk-cloud", "model": "gw-model"})
    server = f"http://127.0.0.1:{port}"
    start = client.start_device_login(server, "test laptop")
    b = Browser(port)
    st, h, _ = b.req("GET", f"/device?code={start['user_code']}")
    assert st == 303 and h["Location"].startswith("/login")  # must sign in first
    b.sign_in()
    st, _, page = b.req("GET", f"/device?code={start['user_code']}")
    assert start["user_code"] in page and "test laptop" in page
    st, _, page = b.req("POST", "/device", {"csrf": csrf_of(page), "user_code": start["user_code"], "action": "approve"})
    assert st == 200 and "Signed in" in page
    assert client.poll_device_login(start, sleep=lambda s: None) == OWNER
    creds = json.loads(client.CRED_FILE.read_text())
    assert creds["server"] == server and "refresh_token" in creds
    assert os.name == "nt" or client.CRED_FILE.stat().st_mode & 0o077 == 0  # no POSIX modes on Windows

    cfg = Config(api_key="local-key", model="local-model")
    assert client.load_cloud_settings(cfg) == OWNER
    assert (cfg.api_key, cfg.model, cfg.resolved_provider()) == ("sk-cloud", "gw-model", "openai")
    cfg.save()  # the cloud key must not reach config.json
    saved = json.loads(CONFIG_FILE.read_text())
    assert saved["api_key"] == "local-key" and saved["model"] == "local-model"

    client.push_settings({"model": "new-model", "api_key": ""})
    assert app.store.get_settings(OWNER)["model"] == "new-model"
    assert app.store.get_settings(OWNER)["api_key"] == "sk-cloud"

    assert client.logout() and not client.CRED_FILE.exists()
    assert app.store.list_logins(OWNER) == []


def test_bad_tokens_and_unreachable_server(cloud, tmp_path):
    _, port = cloud
    b = Browser(port)
    assert b.req("GET", "/api/settings", headers={"Authorization": "Bearer nope"})[0] == 401
    client.save_creds(f"http://127.0.0.1:{port}", "anv_r_bogus", OWNER)
    with pytest.raises(client.NotLoggedIn):
        client.fetch_settings()
    client.save_creds("http://127.0.0.1:1", "anv_r_x", OWNER)
    notes = []
    cfg = Config(api_key="local")
    assert client.load_cloud_settings(cfg, notes.append) is None
    assert cfg.api_key == "local" and "local settings" in notes[0]


def test_plain_http_server_refused():
    with pytest.raises(client.CloudError):
        client.normalize_server("http://anvil.example.com")
    assert client.normalize_server("anvil.example.com/") == "https://anvil.example.com"


def test_desktop_app_signs_in_syncs_and_signs_out(cloud, project):
    from anvil.gui.server import App
    app, port = cloud
    app.store.put_settings(OWNER, {"api_key": "sk-cloud", "model": "cloud-model"})
    gui = App(Config(api_key="local-key", model="local-model", browser_enabled=False), project)
    info = gui.cloud_login_start(f"http://127.0.0.1:{port}")
    assert gui.config_view()["cloud"]["login"]["status"] == "pending"
    app.store.decide_device(info["user_code"], OWNER, True)
    for _ in range(100):  # the app polls every few seconds
        if gui.cloud_login["status"] != "pending":
            break
        threading.Event().wait(0.1)
    assert gui.cloud_login["status"] == "ok", gui.cloud_login
    assert gui.config.api_key == "sk-cloud" and gui.config_view()["cloud"]["email"] == OWNER

    gui.update_config({"model": "picked-in-app"})  # goes to the server, key untouched
    saved = app.store.get_settings(OWNER)
    assert (saved["api_key"], saved["model"]) == ("sk-cloud", "picked-in-app")
    assert json.loads(CONFIG_FILE.read_text())["api_key"] == "local-key"

    gui.cloud_logout()
    assert (gui.config.api_key, gui.config.model) == ("local-key", "local-model")
    assert not client.CRED_FILE.exists()
