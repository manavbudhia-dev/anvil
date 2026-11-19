"""The desktop/phone web app in a real browser, against the real server and a fake model."""

import pytest
from fakes import text, tools

from anvil.config import Config
from anvil.gui.server import App, start_server
from test_browser import _browser_works

pytestmark = pytest.mark.skipif(not _browser_works(), reason="Playwright/Chromium not installed")


@pytest.fixture
def app_url(anthropic, project):
    fake = anthropic()
    cfg = Config(provider="anthropic", base_url=fake.url, api_key="k", auto_compact_tokens=0,
                 cache_keepalive_minutes=0, browser_enabled=False)
    app = App(cfg, project)
    srv = start_server(app, 0)
    yield f"http://127.0.0.1:{srv.server_address[1]}/#token={app.token}", fake
    srv.shutdown()


def _page(pw, mobile=False):
    browser = pw.chromium.launch()
    ctx = browser.new_context(**(pw.devices["iPhone 13"] if mobile else {"viewport": {"width": 1280, "height": 820}}))
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    return browser, page, errors


def test_desktop_task_with_approvals(app_url, project):
    from playwright.sync_api import sync_playwright
    url, fake = app_url
    fake.script = [tools(("write_file", {"path": "hello.py", "content": "print('hi')\n"})),
                   tools(("bash", {"command": "rm -rf build"})), text("Done! Created **hello.py**.")]
    with sync_playwright() as pw:
        browser, page, errors = _page(pw)
        page.goto(url)
        page.wait_for_selector("body[data-ready='1']")
        page.fill("#input", "make hello.py")
        page.press("#input", "Enter")
        page.wait_for_selector(".confirm:not(.danger)")
        page.click('.confirm [data-d="yes"]')
        page.wait_for_selector(".confirm.danger")  # destructive command: red card, Deny is the default
        page.wait_for_function("document.activeElement && document.activeElement.textContent.trim().startsWith('Deny')")
        assert page.locator('.confirm.danger [data-d="always"]').count() == 0
        page.click('.confirm [data-d="no"]')
        page.wait_for_function("document.querySelector('#transcript').innerText.includes('Done!')")
        page.wait_for_function("!document.querySelector('#send').classList.contains('stop')")  # turn finished
        assert (project / "hello.py").exists()
        page.reload()
        page.wait_for_selector("body[data-ready='1']")
        page.click(".sess:has-text('make hello.py')")
        page.wait_for_selector(".tool")
        assert page.locator(".tool").count() == 2
        browser.close()
    assert errors == []


def test_phone_layout_and_token_memory(app_url):
    from playwright.sync_api import sync_playwright
    url, fake = app_url
    base = url.split("#")[0]
    with sync_playwright() as pw:
        browser, page, errors = _page(pw, mobile=True)
        page.goto(base)
        page.wait_for_selector("#token-screen.open")  # no token yet
        page.goto(url)
        page.wait_for_selector("body[data-ready='1']")
        assert "#token" not in page.url  # removed from the address bar
        page.goto(base)
        page.wait_for_selector("body[data-ready='1']")
        assert page.locator("#token-screen.open").count() == 0  # remembered, like a home-screen icon
        layout = page.evaluate("""() => ({ sidebarRight: document.querySelector('#sidebar').getBoundingClientRect().right,
            scrollW: document.documentElement.scrollWidth, vw: innerWidth,
            font: getComputedStyle(document.querySelector('#input')).fontSize })""")
        assert layout["sidebarRight"] <= 0 and layout["scrollW"] <= layout["vw"] and layout["font"] == "16px"
        page.tap("#show-side")
        page.wait_for_timeout(300)
        assert page.evaluate("document.querySelector('#sidebar').getBoundingClientRect().left") >= 0
        browser.close()
    assert errors == []


def test_skill_picker(app_url):
    from playwright.sync_api import sync_playwright
    url, fake = app_url
    with sync_playwright() as pw:
        browser, page, errors = _page(pw)
        page.goto(url)
        page.wait_for_selector("body[data-ready='1']")
        page.wait_for_timeout(300)
        page.type("#input", "/rev")
        page.wait_for_selector("#skill-pop.open")
        page.keyboard.press("Enter")
        assert page.input_value("#input") == "/review "
        page.type("#input", "src/")
        page.keyboard.press("Enter")
        page.wait_for_function("document.querySelector('#transcript').innerText.includes('done')")
        assert page.inner_text(".msg.user .bubble") == "/review src/"
        first = fake.requests[0]["body"]["messages"][0]["content"][0]["text"]
        assert '<skill name="review"' in first
        browser.close()
    assert errors == []
