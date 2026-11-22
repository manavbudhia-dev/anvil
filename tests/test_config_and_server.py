import json
import os
import socket
import stat
import threading
import time
import urllib.error
import urllib.request

import pytest
from fakes import text

from anvil.config import CONFIG_FILE, SESSIONS_DIR, Config
from anvil.fsutil import read_text
from anvil.gui.server import MAX_BODY, TOKEN_FILE, App, Hub, load_token, start_server


# --- settings file ------------------------------------------------------------------

def test_concurrent_saves_never_expose_a_half_written_file():
    c = Config(api_key="sk-secret-" + "x" * 3000)
    c.save()
    bad, stop = [], threading.Event()

    def reader():
        while not stop.is_set():
            try:
                if not json.loads(read_text(CONFIG_FILE)).get("api_key"):
                    bad.append("missing key")
            except json.JSONDecodeError:
                bad.append("corrupt")

    r = threading.Thread(target=reader)
    r.start()
    writers = [threading.Thread(target=lambda: [c.save() for _ in range(200)]) for _ in range(3)]
    [w.start() for w in writers]
    [w.join() for w in writers]
    stop.set()
    r.join()
    assert bad == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_secrets_are_private_files():
    Config(api_key="sk").save()
    assert stat.S_IMODE(CONFIG_FILE.stat().st_mode) == 0o600
    load_token(rotate=True)
    assert stat.S_IMODE(TOKEN_FILE.stat().st_mode) == 0o600


def test_corrupt_config_is_set_aside_not_overwritten():
    CONFIG_FILE.write_text("{ broken")
    Config.load()
    aside = CONFIG_FILE.with_suffix(".json.corrupt")
    assert aside.read_text() == "{ broken" and not CONFIG_FILE.exists()
    aside.unlink()


def test_token_survives_restarts_and_rotates():
    t1 = load_token()
    assert load_token() == t1
    assert load_token(rotate=True) != t1


# --- web app server -----------------------------------------------------------------

@pytest.fixture
def app_server(anthropic, project):
    fake = anthropic()
    cfg = Config(provider="anthropic", base_url=fake.url, api_key="k", auto_compact_tokens=0,
                 cache_keepalive_minutes=0, browser_enabled=False, web_allowed_hosts=["myserver.tailnet.ts.net"])
    app = App(cfg, project)
    srv = start_server(app, 0)
    yield app, srv, fake
    srv.shutdown()


def call(srv, app, method, path, body=None, token=True, host=None):
    url = f"http://127.0.0.1:{srv.server_address[1]}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Anvil-Token"] = app.token
    if host:
        headers["Host"] = host
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_auth_and_host_checks(app_server):
    app, srv, _ = app_server
    assert call(srv, app, "GET", "/api/state", token=False)[0] == 401
    assert call(srv, app, "GET", "/", token=False, host="evil.example.com")[0] == 403
    assert call(srv, app, "GET", "/", token=False, host="myserver.tailnet.ts.net:443")[0] == 200
    assert call(srv, app, "GET", "/manifest.webmanifest", token=False)[0] == 200
    assert call(srv, app, "GET", "/api/state")[0] == 200


def test_bad_requests_are_rejected_cleanly(app_server):
    app, srv, _ = app_server
    port = srv.server_address[1]

    def raw(data: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(data)
            out = b""
            while chunk := s.recv(65536):
                out += chunk
        return out

    host = f"Host: 127.0.0.1:{port}\r\n"
    # Non-ASCII token is a plain 401, not a crash.
    r = raw(f"GET /api/state HTTP/1.1\r\n{host}X-Anvil-Token: \xe9\r\nConnection: close\r\n\r\n".encode("latin-1"))
    assert r.startswith(b"HTTP/1.1 401")
    # A JSON body that is not an object is treated as empty.
    assert call(srv, app, "POST", "/api/config", body=[])[0] == 200
    # An oversized body is refused and the connection closed, so its unread bytes are never parsed as a request.
    smuggled = f"GET /api/state HTTP/1.1\r\n{host}X-Anvil-Token: {app.token}\r\n\r\n"
    r = raw((f"POST /api/config HTTP/1.1\r\n{host}X-Anvil-Token: {app.token}\r\n"
             f"Content-Length: {MAX_BODY + 1}\r\n\r\n" + smuggled).encode())
    assert r.startswith(b"HTTP/1.1 400") and r.count(b"HTTP/1.1 ") == 1
    assert b"X-Frame-Options: DENY" in r and b"Referrer-Policy: no-referrer" in r


def test_send_runs_a_turn_and_saves(app_server, project):
    app, srv, fake = app_server
    fake.script = [text("hello back")]
    sid = call(srv, app, "POST", "/api/sessions", {"root": str(project)})[1]["meta"]["id"]
    assert call(srv, app, "POST", f"/api/sessions/{sid}/send", {"text": "hello"})[0] == 200
    for _ in range(50):
        items = call(srv, app, "GET", f"/api/sessions/{sid}")[1]["items"]
        if any(i["kind"] == "assistant" for i in items):
            break
        time.sleep(0.1)
    assert [i["kind"] for i in items] == ["user", "assistant"]
    for _ in range(30):  # the final save happens just after the reply
        if (SESSIONS_DIR / f"{sid}.json").exists():
            break
        time.sleep(0.1)
    assert (SESSIONS_DIR / f"{sid}.json").exists()


def test_only_one_start_wins(app_server, project):
    app, _, _ = app_server
    s = app.new_session(str(project))
    wins, barrier = [], threading.Barrier(50)

    def go():
        barrier.wait()
        wins.append(s.try_start())

    ts = [threading.Thread(target=go) for _ in range(50)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert wins.count(True) == 1


def test_deleted_running_session_stays_deleted(app_server, project):
    app, _, fake = app_server
    fake.script = [text("slow reply")]
    s = app.new_session(str(project))
    app.send(s, "hello")
    app.delete_session(s.sid)
    s.thread.join(10)
    assert not (SESSIONS_DIR / f"{s.sid}.json").exists()


def test_settings_change_reaches_busy_sessions(app_server, project):
    app, _, _ = app_server
    s = app.new_session(str(project))
    assert s.try_start()
    old = s.agent.backend
    app.update_config({"model": "claude-sonnet-5-5"})
    assert s.backend_stale
    s.finish()
    assert s.try_start() and s.agent.backend is not old
    s.finish()


def test_stalled_event_client_is_dropped():
    h = Hub()
    q = h.subscribe()
    for i in range(Hub.MAX_QUEUED + 10):
        h.publish({"i": i})
    assert h.is_dropped(q) and q.qsize() == Hub.MAX_QUEUED


def test_mode_above_policy_is_refused(app_server, project, tmp_path, monkeypatch):
    from anvil import safety
    pol = tmp_path / "p.json"
    pol.write_text('{"permission_ceiling": "edits"}')
    monkeypatch.setattr(safety, "POLICY_FILE", pol)
    app, srv, _ = app_server
    sid = call(srv, app, "POST", "/api/sessions", {"root": str(project)})[1]["meta"]["id"]
    assert call(srv, app, "POST", f"/api/sessions/{sid}/mode", {"mode": "edits"})[0] == 200
    assert call(srv, app, "POST", f"/api/sessions/{sid}/mode", {"mode": "auto"})[0] == 400
