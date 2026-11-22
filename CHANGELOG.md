# Changelog

## Unreleased

- **Simpler terminal, and Hinglish**: approvals take one key (`y` / `a` / `n`, no Enter), show a one-line plain description of each command, and collapse long commands (`v` shows all). Command output shows ✓/✗ and the first few lines. Shift+Tab or `/auto` switches between ask, edits and auto. `"language": "hinglish"` (in `--setup`, `/lang`, the desktop app and Anvil cloud settings) makes Anvil reply in short, simple Hinglish and translates the terminal prompts.
- Fixed: "always" now covers both file-edit tools, and when the machine's permission limit stops "always" from applying, Anvil says so instead of silently asking again.
- **Browser chat on Anvil cloud** (`anvil cloud chat on`): the desktop app's chat at `<server>/app/`, behind Google sign-in, runs the agent on the server from any browser or phone, with streaming, approvals, permission modes and settings saved on the server. Version 0.3.0, so `pip install --upgrade` picks it up.
- **Stronger command safety**: commands are now parsed like a shell would (quotes, `$'...'`, brace expansion, variables, `sudo`/`env`/`xargs`/`find -exec` wrappers, `bash -c`, `eval`, heredocs, `cmd /c`, PowerShell `-Command`/`-EncodedCommand`) instead of matched with regexes, and interpreter one-liners (`python -c`, `node -e`, `perl -e`, `ruby -e`, `php -r`, `awk`) are checked for deleting files, running shell commands or hidden code. Piping downloaded or decoded text into a shell always asks. Protected paths now cover Linux, macOS and Windows (system folders, startup locations, credentials, the project's `.anvil/`), and cmd.exe/PowerShell destructive commands are detected.
- **Optional sandbox** for the bash tool (`"sandbox": {"mode": "auto"}`, per user or per project): bubblewrap or firejail on Linux, sandbox-exec on macOS, Docker/Podman anywhere. Only the project folder is writable, secrets are hidden, network can be cut, and admins can require it in the policy file.

- **Anvil cloud**: a self-hosted server (`anvil cloud serve`) where you sign in with Google and keep your provider, base URL, model, API key and settings, encrypted at rest. `anvil login <server>` (device-code sign-in) and the desktop app's Settings fetch them, so new machines need no `anvil --setup`. The key stays in memory on clients; sign-ins can be revoked per machine.

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
