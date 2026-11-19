from fakes import tool

from anvil.config import Config
from anvil.doctor import run_checks


def statuses(checks):
    return {c.name: c.status for c in checks}


def cfg(fake, provider="anthropic", **kw):
    base = fake.url + ("/v1" if provider == "openai" else "")
    return Config(provider=provider, base_url=base, api_key="k", **kw)


def test_healthy_official_endpoint(anthropic, project):
    fake = anthropic()
    fake.script = [tool("list_dir", path="."), tool("list_dir", path=".")]
    checks, spent = run_checks(cfg(fake), project, include_browser=False)
    s = statuses(checks)
    assert s["Settings"] == s["API connection"] == s["Tool calling"] == s["Prompt cache"] == s["Cache keep-alive"] == "PASS"
    assert spent > 0


def test_strict_gateway_is_flagged_but_caching_still_passes(anthropic, project):
    fake = anthropic("strict")
    fake.script = [tool("list_dir", path="."), tool("list_dir", path=".")]
    s = statuses(run_checks(cfg(fake), project, include_browser=False)[0])
    assert s["API connection"] == "WARN" and s["Prompt cache"] == "PASS"


def test_no_cache_gateway_warns(anthropic, project):
    fake = anthropic("nocache")
    fake.script = [tool("list_dir", path="."), tool("list_dir", path=".")]
    s = statuses(run_checks(cfg(fake), project, include_browser=False)[0])
    assert s["Prompt cache"] == "WARN" and s["Cache keep-alive"] == "SKIP"


def test_model_without_tool_calling_fails(anthropic, project):
    fake = anthropic()  # default replies are plain text
    s = statuses(run_checks(cfg(fake), project, include_browser=False)[0])
    assert s["Tool calling"] == "FAIL"


def test_openai_format(openai_fake, project):
    fake = openai_fake()
    fake.script = [tool("list_dir", path="."), tool("list_dir", path=".")]
    s = statuses(run_checks(cfg(fake, provider="openai", model="gw/model"), project, include_browser=False)[0])
    assert s["Tool calling"] == "PASS" and s["Prompt cache"] == "PASS" and s["Cache keep-alive"] == "SKIP"


def test_unreachable_endpoint_fails_clearly(project):
    c = Config(provider="anthropic", base_url="http://127.0.0.1:9", api_key="k")
    checks = run_checks(c, project, include_browser=False)[0]
    assert checks[-1].name == "API connection" and checks[-1].status == "FAIL" and "couldn't reach" in checks[-1].detail


def test_missing_key(project, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    checks = run_checks(Config(provider="anthropic", api_key=""), project, include_browser=False)[0]
    assert checks[0].status == "FAIL"
