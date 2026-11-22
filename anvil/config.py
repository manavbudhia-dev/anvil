import json
import os
import sys
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .fsutil import atomic_write_text, read_text

_SAVE_LOCK = threading.Lock()
CONFIG_DIR = Path(os.environ.get("ANVIL_HOME", Path.home() / ".config" / "anvil"))
CONFIG_FILE = CONFIG_DIR / "config.json"
SESSIONS_DIR = CONFIG_DIR / "sessions"
HISTORY_FILE = CONFIG_DIR / "history"

# USD per million tokens: (input, output). Cache reads bill at 10% of input,
# cache writes at 125%.
PRICING = {
    "claude-fable-5-2": (8.0, 40.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.0, 5.0),
}

# Cache reads as a fraction of the input price (from Anthropic's pricing). Others: 0.1.
CACHE_READ_RATE = {
    "claude-fable-5-2": 0.025, "claude-fable-5-1": 0.025,
    "claude-opus-5-5": 0.05, "claude-sonnet-5-5": 0.05,
}
CACHE_WRITE_RATE = {"5m": 1.25, "1h": 2.0}

MODEL_ALIASES = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
    "fable": "claude-fable-5-2",
}

# Models that accept the server-side refusal fallback beta.
FALLBACK_MODELS = {
    "claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5",
    "claude-opus-5", "claude-sonnet-5-5",
}

EFFORTS = ("low", "medium", "high", "xhigh", "max")


PROVIDERS = ("anthropic", "openai")


@dataclass
class Config:
    provider: str = "anthropic"   # "anthropic" (messages format) or "openai" (chat-completions format)
    base_url: str = ""            # empty = official Anthropic API
    api_key: str = ""
    model: str = "claude-opus-5-5"
    effort: str = "high"
    max_tokens: int = 0           # 0 = backend default
    compat_mode: bool = False     # send only basic request fields (for strict gateways)
    reasoning_effort: str = ""    # OpenAI-format only, e.g. low/medium/high; empty = don't send
    price_input: float = 0.0      # USD per 1M tokens, for cost estimates on gateway models
    price_output: float = 0.0
    web_search: bool = False
    last_root: str = ""
    permission_ceiling: str = ""      # "", "ask", "edits" or "auto": the most permissive mode allowed
    web_allowed_hosts: list[str] = field(default_factory=list)  # extra Host names for the web app
    web_public_url: str = ""          # how phones reach the web app, e.g. https://server.tailnet.ts.net/
    browser_enabled: bool = True      # offer browser tools when Playwright is installed
    browser_headless: str = "auto"    # auto (headless when there's no display), true or false
    browser_executable: str = ""      # optional path to a Chrome/Chromium binary
    browser_channel: str = "auto"     # auto, msedge, chrome or chromium
    cache_ttl: str = "5m"             # prompt cache lifetime: 5m, or 1h (writes cost 2x instead of 1.25x)
    cache_keepalive_minutes: int = 30 # while you're away, keep the cache warm this long (0 = off)
    auto_compact_tokens: int = 200000 # summarize the conversation when context grows past this (0 = off)
    openai_cache_control: str = "auto"  # OpenAI-format gateways: send cache markers (auto = for Claude models)
    auto_approve: list[str] = field(default_factory=list)
    sandbox: dict = field(default_factory=dict)  # optional bash sandbox, see anvil/sandbox.py

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        if CONFIG_FILE.exists():
            try:
                data = json.loads(read_text(CONFIG_FILE))
                for k, v in data.items():
                    if hasattr(cfg, k):
                        setattr(cfg, k, v)
            except json.JSONDecodeError:
                # Keep the unreadable file instead of silently overwriting it with defaults later.
                bad = CONFIG_FILE.with_suffix(".json.corrupt")
                try:
                    CONFIG_FILE.replace(bad)
                    print(f"warning: {CONFIG_FILE} was unreadable; moved it to {bad}", file=sys.stderr)
                except OSError:
                    pass
            except OSError:
                pass
        return cfg

    def save(self) -> None:
        data = asdict(self)
        # Settings fetched from Anvil cloud (API key included) stay in memory only: write back
        # this machine's own values for those fields.
        data.update(getattr(self, "_cloud_local", None) or {})
        with _SAVE_LOCK:
            atomic_write_text(CONFIG_FILE, json.dumps(data, indent=2), private=True)

    @property
    def cloud_email(self) -> str:
        return getattr(self, "_cloud_email", "") or ""

    def resolved_key(self) -> str | None:
        return (os.environ.get("ANVIL_API_KEY") or self.api_key
                or (os.environ.get("ANTHROPIC_API_KEY") if self.resolved_provider() == "anthropic"
                    else os.environ.get("OPENAI_API_KEY")) or None)

    def resolved_base_url(self) -> str | None:
        return os.environ.get("ANVIL_BASE_URL") or self.base_url or None

    def resolved_provider(self) -> str:
        p = (os.environ.get("ANVIL_PROVIDER") or self.provider or "anthropic").lower()
        return "openai" if p in ("openai", "openai-compatible", "oai") else "anthropic"


def resolve_model(name: str) -> str:
    return MODEL_ALIASES.get(name.strip().lower(), name.strip())


def price_for(model: str, config: "Config | None" = None) -> tuple[float, float] | None:
    if config and (config.price_input or config.price_output):
        return config.price_input, config.price_output
    if model in PRICING:
        return PRICING[model]
    # Gateway names like "anthropic/claude-opus-4.8" or "claude-opus-4-8-20260101"
    base = model.split("/")[-1].replace(".", "-")
    for known in sorted(PRICING, key=len, reverse=True):
        if base.startswith(known):
            return PRICING[known]
    return None


def cache_read_rate(model: str) -> float:
    base = model.split("/")[-1].replace(".", "-")
    for known in sorted(CACHE_READ_RATE, key=len, reverse=True):
        if base.startswith(known):
            return CACHE_READ_RATE[known]
    return 0.1


def cost_breakdown(model: str, usage, config: "Config | None" = None) -> tuple[float, float] | None:
    """(actual cost, what the same request would have cost without caching), in USD."""
    price = price_for(model, config)
    if price is None:
        return None
    inp, out = price
    fresh = getattr(usage, "input_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    write_1h = min(getattr(usage, "cache_write_1h", 0) or 0, write)
    output = getattr(usage, "output_tokens", 0) or 0
    actual = (fresh * inp + read * inp * cache_read_rate(model)
              + (write - write_1h) * inp * CACHE_WRITE_RATE["5m"] + write_1h * inp * CACHE_WRITE_RATE["1h"]
              + output * out) / 1_000_000
    uncached = ((fresh + read + write) * inp + output * out) / 1_000_000
    return actual, uncached


def cost_usd(model: str, usage, config: "Config | None" = None) -> float | None:
    b = cost_breakdown(model, usage, config)
    return None if b is None else b[0]
