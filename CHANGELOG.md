# Changelog

## 0.2.0

- **`anvil --doctor`**: checks a real setup end to end (gateway, tool calling, prompt cache, keep-alive, browser, safety, phone access, files) and explains how to fix what fails. `--json` for sharing.
- **Test suite and CI**: about 100 tests covering the agent loop, approvals, safety rules, gateways, caching, the web app (desktop and phone, in a real browser), the browser agent, skills, `deploy.sh` and the Caddy helper. Runs on Linux, Windows and macOS on every push.
- Prompt cache engine: caching through gateways, keep-alive, 1-hour TTL option, auto-compact, savings stats.
- Fixed: the app could drop a message typed right after opening it.
- Fixed (Windows, found by CI): protected-path rules didn't match Windows paths, so edits to `.ssh` or Anvil's own settings didn't always ask; adding a skills repo from a local Windows folder failed.
- Fixed: on Windows, saving settings while they were being read could fail or make a reader fall back to defaults.
- Fixed 12 critical bugs and race conditions (settings could be erased by concurrent writes, browser approvals could act on a changed page, two browser threads, background commands blocking for the full timeout, double sends, deleted sessions coming back, the Caddy helper ignoring custom paths and its lock being held by Caddy, `deploy.sh` lock held by restarted apps, git option injection, unbounded event queues, dropped connections).

## 0.1.0

First version: terminal agent, desktop/phone web app, Windows exe build, gateways (Anthropic and OpenAI formats), skills, deploy kit, server safety, Tailscale phone access, Android client, browser agent.
