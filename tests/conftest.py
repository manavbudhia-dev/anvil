import os
import sys
import tempfile
from pathlib import Path

import pytest

# Isolate Anvil's settings, sessions and policy before any anvil module is imported.
_HOME = tempfile.mkdtemp(prefix="anvil-test-home-")
os.environ["ANVIL_HOME"] = _HOME
os.environ["ANVIL_POLICY_FILE"] = os.path.join(_HOME, "no-policy.json")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
for var in ("ANVIL_BASE_URL", "ANVIL_PROVIDER", "ANVIL_API_KEY", "ANVIL_PERMISSION_CEILING"):
    os.environ.pop(var, None)
_no_proxy = "127.0.0.1,localhost"
os.environ["NO_PROXY"] = os.environ["no_proxy"] = _no_proxy

sys.path.insert(0, str(Path(__file__).resolve().parent))  # for `import fakes`

from fakes import FakeAnthropic, FakeOpenAI  # noqa: E402

from anvil.agent import Agent  # noqa: E402
from anvil.config import Config  # noqa: E402


class RecordingUI:
    """Answers approvals from `answers` (default "yes") and records everything shown."""

    interactive = True

    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.asked: list[tuple[str, str, list]] = []
        self.notices: list[str] = []
        self.results: list[tuple[str, str, bool]] = []
        self.images: list[str] = []
        self.text = ""

    def confirm(self, name, args, preview, dangers=None):
        self.asked.append((name, preview, list(dangers or [])))
        return self.answers.pop(0) if self.answers else "yes"

    def notice(self, msg):
        self.notices.append(msg)

    def tool_result(self, name, args, result, is_error):
        self.results.append((name, result, is_error))

    def tool_image(self, name, data_url):
        self.images.append(data_url)

    def text_delta(self, t):
        self.text += t

    def __getattr__(self, name):  # text_start, thinking_delta, tool_running, ...
        return lambda *a, **k: None


@pytest.fixture
def anthropic():
    started = []

    def make(mode="official"):
        f = FakeAnthropic(mode).start()
        started.append(f)
        return f

    yield make
    for f in started:
        f.stop()


@pytest.fixture
def openai_fake():
    started = []

    def make(reject_markers=False):
        f = FakeOpenAI(reject_markers).start()
        started.append(f)
        return f

    yield make
    for f in started:
        f.stop()


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "project"
    p.mkdir()
    return p


@pytest.fixture
def make_agent(project):
    agents = []

    def make(fake, answers=None, provider="anthropic", **cfg):
        cfg.setdefault("auto_compact_tokens", 0)
        cfg.setdefault("cache_keepalive_minutes", 0)
        cfg.setdefault("browser_enabled", False)
        base = fake.url + ("/v1" if provider == "openai" else "")
        config = Config(provider=provider, base_url=base, api_key="k", **cfg)
        a = Agent(config, project, RecordingUI(answers))
        agents.append(a)
        return a

    yield make
    for a in agents:
        a.stop_keepalive()
