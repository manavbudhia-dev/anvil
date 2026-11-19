import json
import time

from fakes import text, tool

from anvil.backends import OpenAIBackend, Usage, to_openai_messages
from anvil.config import Config, cost_breakdown


def markers(body):
    sys_marker = isinstance(body["system"], list) and "cache_control" in body["system"][-1]
    msg = [i for i, m in enumerate(body["messages"])
           if isinstance(m["content"], list) and any("cache_control" in b for b in m["content"])]
    return body.get("cache_control"), sys_marker, msg


def test_official_api_caches_system_and_conversation(anthropic, make_agent):
    fake = anthropic()
    a = make_agent(fake)
    a.send("first")
    a.send("second")
    top, sys_marker, _ = markers(fake.requests[-1]["body"])
    assert top == {"type": "ephemeral"} and sys_marker
    assert "thinking" in fake.requests[-1]["body"]
    st = a.cache_stats()
    assert st["read"] == 9000 and st["saved_usd"] > 0


def test_one_hour_ttl(anthropic, make_agent):
    fake = anthropic()
    make_agent(fake, cache_ttl="1h").send("q")
    body = fake.requests[-1]["body"]
    assert body["cache_control"]["ttl"] == "1h" and body["system"][-1]["cache_control"]["ttl"] == "1h"


def test_strict_gateway_keeps_caching_with_block_markers(anthropic, make_agent):
    fake = anthropic("strict")
    a = make_agent(fake)
    a.send("hello")
    a.send("again")
    top, sys_marker, msg = markers(fake.requests[-1]["body"])
    assert a.backend.level == "blocks" and top is None and sys_marker and msg
    assert "cache_control" not in json.dumps(a.messages)  # stored history stays clean
    assert any("prompt caching kept" in n for n in a.ui.notices)


def test_gateway_without_cache_support_still_works(anthropic, make_agent):
    fake = anthropic("nocache")
    a = make_agent(fake)
    a.send("hi")
    assert a.backend.level == "none" and a.messages[-1]["role"] == "assistant"


def test_gateway_auth_headers(anthropic, make_agent):
    fake = anthropic()
    make_agent(fake).send("hi")
    h = {k.lower(): v for k, v in fake.requests[0]["headers"].items()}
    assert h["authorization"] == "Bearer k" and h["x-api-key"] == "k"


def test_keepalive_refreshes_cache_with_identical_prefix(anthropic, make_agent):
    fake = anthropic()
    a = make_agent(fake, cache_keepalive_minutes=30)
    a.KEEPALIVE_CHECK_SECONDS = 0.1
    a.send("first")
    a.send("second")
    a.backend.last_start -= 290  # the last request was almost 5 minutes ago
    deadline = time.time() + 5
    while time.time() < deadline and not any(r["body"].get("max_tokens") == 0 for r in fake.requests):
        time.sleep(0.1)
    pings = [r["body"] for r in fake.requests if r["body"].get("max_tokens") == 0]
    real = [r["body"] for r in fake.requests if r["body"].get("max_tokens") != 0][-1]
    assert pings, "no keep-alive ping was sent"
    p = pings[0]
    assert "stream" not in p and p["messages"][-1]["role"] == "user"
    assert (p["system"], p["tools"], p["messages"]) == (real["system"], real["tools"], real["messages"])
    # Gone longer than the keep-alive window: no more pings.
    n = len(pings)
    a._last_activity -= 31 * 60
    a.backend.last_start -= 290
    time.sleep(0.6)
    assert len([r for r in fake.requests if r["body"].get("max_tokens") == 0]) == n


def test_openai_format_tool_loop(openai_fake, make_agent, project):
    fake = openai_fake()
    fake.script = [tool("write_file", path="hi.txt", content="yo\n"), text("All done.")]
    a = make_agent(fake, provider="openai", model="gw/some-model")
    a.auto_approve_all = True
    a.send("make hi.txt")
    assert (project / "hi.txt").read_text() == "yo\n"
    msgs = fake.requests[1]["body"]["messages"]
    assert msgs[-1]["role"] == "tool" and msgs[-2]["tool_calls"][0]["function"]["name"] == "write_file"
    assert a.cache_stats()["read"] == 2000  # cached_tokens is reported (1000 per request)


def test_openai_cache_markers_for_claude_and_fallback(openai_fake, make_agent):
    fake = openai_fake(reject_markers=True)
    a = make_agent(fake, provider="openai", model="anthropic/claude-opus-4.8")
    a.send("hi")
    first, rest = fake.requests[0]["body"], fake.requests[1:]
    assert "cache_control" in json.dumps(first)
    assert all("cache_control" not in json.dumps(r["body"]) for r in rest)
    assert any("rejected cache markers" in n for n in a.ui.notices)


def test_openai_markers_only_for_claude_by_default():
    gpt = OpenAIBackend(Config(provider="openai", model="openai/gpt-5", base_url="http://x/v1", api_key="k"), None)
    claude = OpenAIBackend(Config(provider="openai", model="claude-sonnet-5-5", base_url="http://x/v1", api_key="k"), None)
    assert not gpt.cache_markers and claude.cache_markers


def test_screenshots_become_image_messages_in_openai_format():
    msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "browser_screenshot", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": [
                {"type": "text", "text": "Screenshot"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}}]}]}]
    out = to_openai_messages("sys", msgs)
    assert out[3]["role"] == "tool"
    assert out[4]["content"][1]["image_url"]["url"] == "data:image/jpeg;base64,AAAA"


def test_cost_math_uses_model_cache_prices():
    u = Usage(input_tokens=100, output_tokens=200, cache_read_input_tokens=9000, cache_creation_input_tokens=1000)
    actual, uncached = cost_breakdown("claude-opus-5-5", u)
    assert abs(actual - (100 * 4 + 9000 * 4 * 0.05 + 1000 * 4 * 1.25 + 200 * 20) / 1e6) < 1e-12
    assert abs(uncached - (10100 * 4 + 200 * 20) / 1e6) < 1e-12
    assert abs(cost_breakdown("claude-fable-5-2", Usage(cache_read_input_tokens=1_000_000))[0] - 0.2) < 1e-9
    assert cost_breakdown("some/unknown-model", u) is None
