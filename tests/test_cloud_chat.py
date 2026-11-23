"""Browser chat on the Anvil cloud server (/app/), behind Google sign-in."""

import json
import re
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

pytest.importorskip("cryptography")

from fakes import text, tools  # noqa: E402
from test_cloud import OWNER, Browser  # noqa: E402

from anvil.cloud.server import CloudApp, CloudConfig, make_handler  # noqa: E402
from anvil.cloud.store import Store, load_master_key  # noqa: E402

FRIEND = "friend@example.com"


@pytest.fixture
def chat(tmp_path, anthropic, project):
    fake = anthropic()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), lambda *a: None)
    srv.daemon_threads = True
    port = srv.server_address[1]
    cfg = CloudConfig(public_url=f"http://127.0.0.1:{port}", google_client_id="cid", google_client_secret="sec",
                      allowed_emails=[OWNER, FRIEND], chat=True, chat_workspace=str(project), chat_emails=[OWNER])
    store = Store(tmp_path / "srv" / "db.sqlite", load_master_key(tmp_path / "srv" / "master.key"))
    store.put_settings(OWNER, {"provider": "anthropic", "base_url": fake.url, "api_key": "sk-cloud",
                               "model": "claude-opus-5-5", "auto_compact_tokens": 0,
                               "cache_keepalive_minutes": 0, "browser_enabled": False})
    app = CloudApp(cfg, store, exchange=lambda code, verifier: {"email": code, "name": ""})
    srv.RequestHandlerClass = make_handler(app)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, port, fake
    for s in list(app.chat.app.sessions.values()):
        s.ui.cancel.set()
        s.agent.stop_keepalive()
    srv.shutdown()
    srv.server_close()


def page_token(html):
    return json.loads(re.search(r"window.ANVIL_CLOUD = (\{.*?\});", html).group(1))["token"]


def wait_idle(b, sid, token):
    for _ in range(200):
        st, _, body = b.req("GET", f"/app/api/sessions/{sid}", headers={"X-Anvil-Token": token})
        d = json.loads(body)
        if not d["meta"]["running"]:
            return d
        time.sleep(0.05)
    raise AssertionError("turn didn't finish")


def test_chat_needs_sign_in(chat):
    _, port, _ = chat
    b = Browser(port)
    st, h, _ = b.req("GET", "/app/")
    assert st == 303 and h["Location"] == "/login?next=%2Fapp%2F"
    assert b.req("GET", "/app/api/state")[0] == 401
    assert b.req("POST", "/app/api/sessions", json_body={})[0] == 401
    assert b.req("GET", "/app/api/events")[0] == 401
    # The sign-in comes back to the chat.
    st, h, _ = b.req("GET", "/login?next=%2Fapp%2F")
    import urllib.parse
    state = urllib.parse.parse_qs(urllib.parse.urlparse(h["Location"]).query)["state"][0]
    st, h, _ = b.req("GET", f"/auth/callback?state={state}&code={OWNER}")
    assert st == 303 and h["Location"] == "/app/"


def test_chat_page_and_csrf(chat):
    app, port, _ = chat
    b = Browser(port)
    b.sign_in()
    assert "href=/app/" in b.req("GET", "/")[2]  # the home page links to the chat
    st, h, _ = b.req("GET", "/app")
    assert st == 303 and h["Location"] == "/app/"
    st, h, html = b.req("GET", "/app/")
    assert st == 200 and "window.ANVIL_CLOUD" in html and h["Referrer-Policy"] == "same-origin"
    assert h["X-Frame-Options"] == "DENY"
    token = page_token(html)
    assert b.req("GET", "/app/manifest.webmanifest")[0] == 200
    st, _, body = b.req("GET", "/app/api/state")
    assert st == 200 and json.loads(body)["config"]["cloud"]["email"] == OWNER
    assert json.loads(body)["default_root"] == app.cfg.chat_workspace
    # State changes need the page's token, and no cross-site request gets through.
    assert b.req("POST", "/app/api/sessions", json_body={})[0] == 403
    assert b.req("POST", "/app/api/sessions", json_body={}, headers={"X-Anvil-Token": "nope"})[0] == 403
    assert b.req("POST", "/app/api/sessions", json_body={},
                 headers={"X-Anvil-Token": token, "Sec-Fetch-Site": "cross-site"})[0] == 403
    st, _, body = b.req("POST", "/app/api/sessions", json_body={},
                        headers={"X-Anvil-Token": token, "Sec-Fetch-Site": "same-origin", "Origin": "null"})
    assert st == 200 and json.loads(body)["meta"]["root"] == app.cfg.chat_workspace
    # Desktop-only endpoints are off.
    for path in ("/app/api/connect", "/app/api/qr.svg"):
        assert b.req("GET", path)[0] == 404
    assert b.req("POST", "/app/api/open", json_body={"url": "https://x"}, headers={"X-Anvil-Token": token})[0] == 404
    assert b.req("POST", "/app/api/cloud/login", json_body={}, headers={"X-Anvil-Token": token})[0] == 404
    # Extras: the panels' script and APIs work, but nothing that installs or listens on the server.
    assert b.req("GET", "/app/extras.js")[0] == 200
    assert b.req("GET", "/app/api/connectors")[0] == 200
    assert b.req("GET", "/app/api/search?q=x")[0] == 200
    assert b.req("POST", "/app/api/browser/install", json_body={}, headers={"X-Anvil-Token": token})[0] == 404
    assert b.req("POST", "/app/api/connectors/notion/login", json_body={}, headers={"X-Anvil-Token": token})[0] == 404


def test_chat_only_for_chat_emails(chat):
    _, port, _ = chat
    b = Browser(port)
    st, _, _ = b.sign_in(FRIEND)
    assert st == 303
    assert "href=/app/" not in b.req("GET", "/")[2]
    assert b.req("GET", "/app/api/state")[0] == 403


def test_chat_runs_agent_with_cloud_settings_and_approvals(chat, project):
    app, port, fake = chat
    fake.script = [tools(("write_file", {"path": "hello.py", "content": "print('hi')\n"})), text("Done.")]
    b = Browser(port)
    b.sign_in()
    token = page_token(b.req("GET", "/app/")[2])
    hdr = {"X-Anvil-Token": token}
    sid = json.loads(b.req("POST", "/app/api/sessions", json_body={}, headers=hdr)[2])["meta"]["id"]
    events = app.chat.app.hub.subscribe()
    assert b.req("POST", f"/app/api/sessions/{sid}/send", json_body={"text": "make hello.py"}, headers=hdr)[0] == 200
    while True:  # "ask" mode: the write waits for Allow
        ev = events.get(timeout=10)
        if ev["type"] == "confirm":
            break
    assert not (project / "hello.py").exists()
    assert b.req("POST", f"/app/api/sessions/{sid}/approve", json_body={"req_id": ev["req_id"], "decision": "yes"},
                 headers=hdr)[0] == 200
    d = wait_idle(b, sid, token)
    assert (project / "hello.py").read_text() == "print('hi')\n"
    assert d["items"][-1] == {"kind": "assistant", "text": "Done."}
    assert fake.requests[0]["headers"].get("x-api-key") == "sk-cloud"  # the key saved on the server
    # Settings changed in the chat are saved to the account on the server.
    assert b.req("POST", "/app/api/config", json_body={"model": "claude-sonnet-5-5"}, headers=hdr)[0] == 200
    saved = app.store.get_settings(OWNER)
    assert saved["model"] == "claude-sonnet-5-5" and saved["api_key"] == "sk-cloud"
    # ...and changes on the settings page reach the chat.
    app.store.put_settings(OWNER, {**saved, "model": "claude-haiku-5-5"})
    assert json.loads(b.req("GET", "/app/api/state")[2])["config"]["model"] == "claude-haiku-5-5"


def test_chat_events_stream_through_cloud_server(chat):
    _, port, _ = chat
    import http.client
    b = Browser(port)
    b.sign_in()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/app/api/events", headers={"Cookie": f"anvil_cloud={b.cookies['anvil_cloud']}"})
    r = conn.getresponse()
    assert r.status == 200 and r.headers["Content-Type"] == "text/event-stream"
    assert r.fp.readline() == b": connected\n"
    conn.close()


def test_chat_off_by_default(tmp_path):
    cfg = CloudConfig(public_url="http://127.0.0.1:1", google_client_id="c", google_client_secret="s",
                      allowed_emails=[OWNER])
    app = CloudApp(cfg, Store(tmp_path / "d.sqlite", load_master_key(tmp_path / "k")), exchange=lambda *a: {})
    assert app.chat is None
    bad = CloudConfig(public_url="https://x", google_client_id="c", google_client_secret="s",
                      allowed_emails=[OWNER], chat=True, chat_workspace=str(tmp_path / "missing"))
    assert any("chat_workspace" in p for p in bad.problems())


def test_chat_in_a_real_phone_browser(chat, project):
    from test_browser import _browser_works
    if not _browser_works():
        pytest.skip("Playwright/Chromium not installed")
    from playwright.sync_api import sync_playwright
    app, port, fake = chat
    fake.script = [tools(("write_file", {"path": "hi.txt", "content": "hi\n"})), text("All **done**.")]
    b = Browser(port)
    b.sign_in()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(**pw.devices["iPhone 13"])
        ctx.add_cookies([{"name": "anvil_cloud", "value": b.cookies["anvil_cloud"], "url": f"http://127.0.0.1:{port}"}])
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{port}/app/")
        page.wait_for_selector("body[data-ready='1']")
        assert not page.is_visible("#token-screen")
        page.fill("#input", "write hi.txt")
        page.press("#input", "Enter")
        page.wait_for_selector(".confirm")
        page.click('.confirm [data-d="yes"]')
        page.wait_for_function("document.querySelector('#transcript').innerText.includes('All done')")
        assert (project / "hi.txt").read_text() == "hi\n"
        page.click("#show-side")
        assert page.locator(".sess:has-text('write hi.txt')").count() == 1
        # The extras panels work on the phone too.
        page.click("#x-nav [data-panel=scheduled]")
        page.wait_for_selector("button:text-is('New scheduled task')")
        page.keyboard.press("Escape")
        page.click("#show-side")
        page.click("#x-nav [data-panel=connectors]")
        page.wait_for_selector(".x-tile:has-text('Notion')")
        page.click(".x-tile:has-text('Notion')")
        page.wait_for_selector(".x-note:has-text('on the server')")
        browser.close()
    assert errors == []


def test_projects_and_threads_in_cloud_chat(chat, project):
    from test_browser import _browser_works
    if not _browser_works():
        pytest.skip("Playwright/Chromium not installed")
    from playwright.sync_api import sync_playwright
    from fakes import tool
    from test_projects import RoleScript
    app, port, fake = chat
    rs = RoleScript(fake)
    rs.chat = [tool("start_thread", title="Write notes", task="Write notes.txt."), text("Started a thread.")]
    rs.thread = [tool("update_checklist", items=[{"text": "Write notes", "state": "doing"}]),
                 tool("ask_user", question="Which format?", options=[{"label": "Plain"}, {"label": "Markdown"}]),
                 tool("set_status", status="done", summary="Notes written."), text("Notes written.")]
    b = Browser(port)
    b.sign_in()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 820})
        ctx.add_cookies([{"name": "anvil_cloud", "value": b.cookies["anvil_cloud"], "url": f"http://127.0.0.1:{port}"}])
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{port}/app/")
        page.wait_for_selector("body[data-ready='1']")
        page.click("#new-proj-btn")
        assert not page.is_visible("#pm-browse")  # no folder picker on the server
        page.fill("#pm-name", "Cloud proj")
        page.fill("#pm-path", str(project))
        page.click("#pm-save")
        page.wait_for_selector("body.in-project")
        page.fill("#input", "write notes")
        page.press("#input", "Enter")
        page.wait_for_selector(".tool.thread-link")
        page.click(".tool.thread-link summary")
        page.wait_for_selector("body.panel-open .decision")
        page.click(".decision .opt:has-text('Markdown')")
        page.wait_for_function("document.querySelector('#tp-status').textContent === 'Done'")
        page.wait_for_selector(".thr.active .sdot.done")
        browser.close()
    assert errors == []
