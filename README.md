# Anvil

A terminal coding agent, in the spirit of Claude Code and OpenCode, that runs on **your own API key**: the official Anthropic API or any third-party gateway (OpenAI-compatible or Anthropic-compatible). No subscription: you pay only for the tokens you use.

## Quick install (one line)

**Linux server (Oracle Linux, RHEL, Ubuntu, Debian) or macOS.** Push this folder to a GitHub repo, set `DEFAULT_REPO` in `install.sh` to its URL, then on any machine:

```bash
curl -fsSL https://raw.githubusercontent.com/OWNER/REPO/main/install.sh | bash
```

It installs Python 3.10+, git and tmux with your package manager, installs Anvil into `~/.anvil`, puts `anvil` and `anvil-desktop` on your PATH, and starts the setup wizard. Run the same line again to update.

- **Private repo** (the server has a GitHub deploy key):

  ```bash
  git clone -q git@github.com:OWNER/REPO.git ~/.anvil/src && bash ~/.anvil/src/install.sh
  ```

  Update later with `bash ~/.anvil/src/install.sh`.
- **Without GitHub**, after copying `anvil.zip` to the machine:

  ```bash
  unzip -oq anvil.zip && bash anvil/install.sh
  ```

- **No questions asked** (for scripts and new servers):

  ```bash
  curl -fsSL https://raw.githubusercontent.com/OWNER/REPO/main/install.sh | ANVIL_PROVIDER=openai ANVIL_BASE_URL=https://your-gateway/v1 ANVIL_MODEL=your-model ANVIL_API_KEY=your-key bash
  ```

  A key typed on the command line lands in your shell history; run `history -d $(history 1)` afterwards, or leave `ANVIL_API_KEY` out and use `anvil --setup`.
- Other options: `ANVIL_DESKTOP=1` also installs the native-window desktop app, `ANVIL_REF=v0.2.0` installs a specific branch or tag, and `ANVIL_PREFIX` changes the install folder.

On Windows, use `Anvil.exe` (see Windows app below) or WSL.

## Check your setup: `anvil --doctor`

After installing (and on each new machine or server), run:

```bash
anvil --doctor
```

It sends a few small real requests (a few cents) and reports PASS, WARN or FAIL, with a fix for each problem:

- **Connection:** settings, the API connection, and whether the gateway forced compatibility mode.
- **Tool calling:** whether your model can call tools at all. Without it, Anvil can't edit files or run commands.
- **Prompt cache:** whether caching actually saves money through your gateway, and whether keep-alive refreshes are accepted.
- **Everything else:** the browser agent, running as root, the permission limit, phone access, and file permissions.

`anvil --doctor --json` prints the same as JSON to paste into a bug report.

## Install

Needs Python 3.10+.

```bash
cd anvil
pip install -e .
```

(Or `pipx install .` to keep it isolated.) Optional but recommended: install [ripgrep](https://github.com/BurntSushi/ripgrep) for faster search.

## Desktop app

Anvil also has a desktop app: a sessions sidebar, a chat transcript with tool cards and diffs, inline Allow / Deny approvals, a permission-mode picker, model and effort pickers, and a Settings screen for your gateway.

```bash
pip install -e ".[desktop]"   # adds pywebview for a native window
anvil-desktop                 # or: anvil-desktop -C path/to/project
```

- Windows and macOS: the native window works out of the box after the install above.
- Linux: pywebview also needs Qt or GTK (`pip install "pywebview[qt]"`). Without it, or with `anvil-desktop --browser`, the app opens in your web browser instead. It works the same either way.
- Open Settings (bottom left) to choose the API format, base URL, model and key. On first launch it opens automatically if no key is saved.
- Permission modes: **Ask before edits** (default), **Accept edits** (file changes run without asking, commands still ask), **Bypass permissions** (nothing asks).
- Shortcuts: Enter sends, Shift+Enter adds a new line, Esc stops, Ctrl/Cmd+N starts a new session.
- The app runs a server on 127.0.0.1 only, protected by a random token generated at each launch, so websites can't control it.
- Sessions are shared with the terminal version: `anvil -c` continues a desktop session and vice versa.

## Windows app (Anvil.exe)

You can turn the desktop app into a single `Anvil.exe` you double-click, with no terminal needed. Two ways to build it:

**On your Windows PC.** Install Python 3.10+ from python.org (tick "Add python.exe to PATH"), then double-click `build_windows.bat` in this folder. After a few minutes you get `dist\Anvil.exe`. Move it anywhere (your Desktop, for example) and double-click it.

**On GitHub, with no Python needed.** Push this folder to a GitHub repository, open the **Actions** tab, choose **Build Windows app**, then **Run workflow**. When it finishes, download `Anvil-windows` from the run's Artifacts. It also builds automatically when you push a tag like `v0.1.0`.

Notes:
- The exe opens in its own window using Microsoft Edge WebView2, which Windows 10 and 11 already include.
- The browser agent is built in and drives the Microsoft Edge already on your PC. Because it bundles Playwright, the exe is about 120 MB and takes a few seconds to start. The GitHub workflow checks that it really starts Edge.
- On first launch, Settings opens so you can enter your gateway, model and key. Settings and sessions are stored in `%USERPROFILE%\.config\anvil`.
- Windows SmartScreen may warn about an unsigned app the first time. Click **More info**, then **Run anyway**.
- Commands run in `cmd.exe` (Anvil tells the model so). If you prefer bash, use the terminal version from Git Bash or WSL.

## Skills

Skills are reusable instructions Anvil loads when a task matches, like a checklist or a house style. Anvil sees each skill's name and description, and loads the full instructions only when needed. You can also run one directly:

- Terminal: type `/` and Tab to see skills, then e.g. `/review src/` or `/deploy-setup`. `/skills` lists them. One-shot: `anvil "/review"`.
- Desktop app: type `/` in the message box to pick one, or open **Skills** in the sidebar.

Built-in skills: `/deploy-setup` (GitHub to server auto-deploy, below), `/review` (bug and security review), `/commit` (safe commit with a good message).

**Write your own.** Make a folder with a `SKILL.md`:

```
~/.config/anvil/skills/<name>/SKILL.md       # personal, all projects
<project>/.anvil/skills/<name>/SKILL.md      # project, shared with your team through git
```

```markdown
---
name: fastapi
description: Conventions for our FastAPI services; use when adding endpoints.
---
Instructions here. Helper files in the same folder are available to Anvil too.
```

A project skill overrides a personal one with the same name, which overrides a built-in.

**Sync across machines (laptop and server) with git.** Put your skill folders in a GitHub repo, then on each machine:

```bash
anvil --add-skills https://github.com/you/anvil-skills.git
```

Later, `anvil --sync-skills` (or `/skills sync`, or **Sync from Git** in the app) pulls updates.

## Auto-deploy from GitHub to your server

In a project, run `/deploy-setup`. Anvil looks at how the app builds and runs, adds three files, and gives you the one-time setup commands filled in for your project:

- `deploy.sh`, which runs on the server: fetch, build, restart, health check. If any step fails, it **rolls back** to the previous commit and restarts that. It refuses to run if the server checkout has uncommitted changes.
- `deploy.env.example`, with the build command, restart method (`docker`, `systemd:<unit>`, `pm2:<name>` or a command) and health check. It detects Docker Compose, npm/pnpm/yarn, Python and Go automatically.
- `.github/workflows/deploy.yml`, which SSHes to your server on every push to `main` and runs `./deploy.sh <commit>`. It needs the repository secrets `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS` and `DEPLOY_PATH`.

The kit is in `anvil/builtin_skills/deploy-setup/` if you want to copy it by hand.

## Install on a Linux server (e.g. Oracle Cloud)

Needs Python 3.10+. On Oracle Linux 9 (on Oracle Linux 8 use `python3.11`):

```bash
sudo dnf install -y python3.12 python3.12-pip git unzip tmux
```

On Ubuntu:

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip git unzip tmux ripgrep
```

Then (on Ubuntu, use `python3` instead of `python3.12`):

```bash
unzip anvil.zip && cd anvil && python3.12 -m venv ~/.anvil-venv && ~/.anvil-venv/bin/pip install -e .
```

```bash
echo 'export PATH="$HOME/.anvil-venv/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc && anvil --setup
```

Run it inside `tmux new -s anvil` so it keeps running after you disconnect (detach with Ctrl+B then D, come back with `tmux attach -t anvil`).

To use the desktop app against the server from your laptop, run `anvil-desktop --browser --port 8790 -C ~/project` on the server, then on your laptop:

```bash
ssh -i your-key -L 8790:127.0.0.1:8790 opc@SERVER_IP
```

Open the printed link (with its `#token=`) in your laptop's browser. Don't open port 8790 to the internet; the tunnel is the secure way in.

## Browser agent

Anvil can drive a real Chromium browser: open pages, read them, click, type, pick from dropdowns, scroll, switch tabs and take screenshots. Use it to check a deployed site, test a web app's flow, fill in a form, or read a dashboard or docs page. Install it once:

```bash
anvil --install-browser
```

(or `pip install playwright && python -m playwright install chromium`, or `ANVIL_BROWSER=1` with the one-line installer). The browser tools then appear automatically, in the terminal, the desktop app and on your phone. Try `/browse check that https://your-site.com loads and the signup form works`.

- **How it reads pages:** each page comes back as text plus a numbered list of buttons, links and fields, so the model acts on numbers ("click 7"). Screenshots are sent to the model as images and shown in the app's tool card.
- **Approvals:** reading, scrolling and screenshots run freely. Clicks, typing, dropdowns and key presses ask first in Ask mode, like commands. Typing into password, one-time-code or payment fields, and clicking buttons like Buy, Pay, Place order, Delete, Send or Publish, always ask, even in Bypass mode.
- **Untrusted content:** page text is marked as data from the website, and the agent is told never to follow instructions found in pages. Hidden text isn't read.
- **Blocked:** only `http` and `https` pages open. `file://` and cloud metadata addresses like `169.254.169.254` are refused.
- **Logins:** the browser keeps its own profile in `~/.config/anvil/browser-profile`, so logins are remembered. On a desktop the window is visible, so you can log in yourself and let the agent continue. On a server it runs headless.
- **Settings** (`~/.config/anvil/config.json`): `browser_enabled`, `browser_headless` (`auto`, `true` or `false`), `browser_executable` (use an installed Chrome).
- On Oracle Linux/RHEL, Chromium needs some system libraries; `anvil --install-browser` prints the `dnf` command if they're missing.
- **Which browser:** on Windows, Anvil uses the Microsoft Edge that comes with Windows, so nothing extra is downloaded. Then it tries Chrome, and as a last resort downloads Playwright's Chromium once. On macOS it tries Chrome first, on Linux Playwright's Chromium first. Force one with `browser_channel` (`msedge`, `chrome` or `chromium`) in the config.

## Use it from your phone (Tailscale)

Run Anvil's web app on your server and open it on your phone over Tailscale, a free private network. Type a task on the phone; it runs on the server, and approvals come to the phone. Nothing is exposed to the public internet.

On the server, as the user Anvil runs as:

```bash
sudo tailscale up && sudo tailscale set --operator=$USER
```

```bash
anvil-desktop --tailscale --install-service -C ~/your-project
```

```bash
sudo loginctl enable-linger $USER
```

This publishes the app at `https://<server>.<tailnet>.ts.net/` (reachable only inside your tailnet, with a real certificate), keeps it running as a background service, and prints a QR code. On the phone, install Tailscale, scan the QR code, then choose **Add to Home Screen**.

- `anvil-desktop --qr` prints the QR code again. **Settings > Connect a phone** in the app shows it too.
- The link contains an access token that survives restarts. If it leaks, run `anvil-desktop --rotate-token` and scan again.
- Using another tunnel or reverse proxy (Cloudflare Tunnel, Caddy...)? Use `--allow-host your.domain --public-url https://your.domain/` instead of `--tailscale`. Put a login in front of it (Cloudflare Access can require your Google account); the token alone shouldn't be your only protection on the public internet.
- The server's safety rules still apply: the permission lock, and confirmation for destructive commands.

**Your own Android app:** see `android/README.md`. You can embed Anvil's screen in a WebView, or use `android/AnvilClient.kt` (Kotlin, OkHttp) to build your own UI on Anvil's API.

## Server safety (read before giving the agent a server)

Anvil has guard rails for running on a real server. Some are always on, and the rest come from a one-time setup script.

**Always on, in the terminal and the desktop app:**
- **Destructive commands ask every time**, even in Bypass mode or after "always allow". This covers `rm -rf`, firewall changes (`ufw`, `firewall-cmd`, `iptables`, `nft`), `systemctl stop/disable`, reboots, disk tools, force-pushes, `git reset --hard`, dropping databases, `docker system prune`, `curl | sh`, editing `/etc`, SSH keys and sudoers, and changing Anvil's own settings. The terminal makes you type `yes`; the app shows a red card where Enter means Deny. In one-shot mode (`anvil -y "..."`) they're always refused.
- **Commands are parsed, not pattern-matched.** Anvil reads each command the way the shell would, so quoting tricks (`r""m`, `$'\x72m'`, `{rm,-rf,/}`), variables set earlier in the line, wrappers (`sudo`, `env`, `xargs`, `timeout`, `find -exec`, `ssh host '...'`), nested shells (`bash -c`, `eval`, `cmd /c`, `powershell -Command` and `-EncodedCommand`), heredocs, and interpreter one-liners (`python -c "shutil.rmtree(...)"`, `node -e`, `perl -e`, `ruby -e`, `php -r`, `awk 'BEGIN{system(...)}'`) are all checked. Piping downloaded or decoded text into a shell or interpreter (`curl … | sh`, `base64 -d | bash`, `iwr … | iex`) always asks. cmd.exe and PowerShell commands (`del /s /q`, `rd /s`, `format`, `reg delete`, `Remove-Item -Recurse`, Defender and firewall changes) are covered on Windows.
- **Protected paths on every OS**: system folders (`/etc`, `/usr`, `/boot`, `/System`, `C:\Windows`, `C:\Program Files`), startup locations (shell rc files, LaunchAgents, the Windows Startup folder, git hooks), SSH keys, cloud credentials, Anvil's own settings and the project's `.anvil/` folder. Writing to them from a command or an edit asks every time; reading keys and credentials asks too.
- **Reloading Caddy or nginx without validating first** is flagged and needs a yes.
- **Edits to the live Caddy config, system files and SSH config** always ask.
- **Automatic backups:** before Anvil edits a config file (`Caddyfile`, `*.conf`, `*.service`, `.env`, YAML/TOML/INI) or any file outside the project, it saves a copy in `~/.config/anvil/backups/` and tells the model where.

**Optional sandbox for commands.** With the sandbox on, the bash tool runs commands with only the project folder (and a private temp dir) writable, the rest of the disk read-only, and SSH keys, cloud credentials and Anvil's settings hidden. Turn it on in `~/.config/anvil/config.json` or per project in `.anvil/settings.json`:

```json
{"sandbox": {"mode": "auto", "network": true, "writable": ["~/.cache/pip"]}}
```

- `mode`: `off` (default), `auto`, or a backend: `bwrap` (bubblewrap) or `firejail` on Linux, `sandbox-exec` on macOS (built in), `docker`/`podman` anywhere (`"image"` picks the container image). `auto` tries them in that order for your OS.
- `network: false` cuts network access.
- A project's settings can turn the sandbox on or make it stricter, but can't turn off a sandbox your own config turned on, and its extra writable paths must stay inside the project.
- Admins can force it in `/etc/anvil/policy.json` with `{"sandbox": "required"}`: commands are then refused when no backend works, instead of running unsandboxed. `ANVIL_SANDBOX=auto` turns it on for one run. `anvil --doctor` shows which backend is in use.
- Permission prompts work the same with the sandbox on; it's an extra layer.

**One-time setup on the server (as root):**

```bash
sudo SERVICES="caddy myapp" bash anvil/server/setup-agent-user.sh
```

- Creates a separate `anvil` user with no password, and copies your SSH key so `ssh anvil@server` works.
- Gives it `sudo` for **only** `systemctl restart|reload <your services>` and the Caddy helper below. No root shell, no `systemctl stop`, nothing else.
- Writes `/etc/anvil/policy.json` (root-owned) to lock Anvil to **Ask before edits** for every user. Neither Bypass mode, "always allow", the user's config nor environment variables can loosen it. Use `CEILING=edits` or `CEILING=auto` later when you trust it more.
- Installs Anvil root-owned in `/opt/anvil`, so the agent can run Anvil but can't edit its safety code.
- Installs `anvil-caddy-apply`, which the agent uses instead of touching `/etc/caddy` directly:

```bash
sudo anvil-caddy-apply diff ~/Caddyfile.new       # show the change
sudo anvil-caddy-apply apply ~/Caddyfile.new      # backup, validate, install, reload
sudo anvil-caddy-apply rollback                   # undo the last change
```

`apply` refuses invalid configs without changing anything. If Caddy rejects the reload, the helper restores the backup and restarts Caddy cleanly. Backups go to `/etc/caddy/anvil-backups/` (last 30) and a log to `/var/log/anvil-caddy.log`. The built-in `/caddy` skill teaches the agent this workflow.

Then work as the agent user: `sudo -iu anvil`, `anvil --setup`, `cd ~/project && anvil`. If an app folder should be managed by the agent, pass `APP_DIRS="/srv/myapp"` to give the `anvil` user ownership of it.

## Configure your endpoint

Run the setup wizard once:

```bash
anvil --setup
```

It asks for four things:
1. **API format.** Pick *OpenAI-compatible* if your gateway's docs show `/v1/chat/completions` (OpenRouter, LiteLLM, Together, Groq, vLLM, most proxies). Pick *Anthropic-compatible* if they show `/v1/messages`. Pick *official* for api.anthropic.com.
2. **Base URL**, as your gateway's docs give it. For OpenAI-format this usually ends in `/v1` (e.g. `https://openrouter.ai/api/v1`).
3. **Model name**, exactly as your gateway spells it (e.g. `anthropic/claude-opus-4.8`).
4. **API key.**

Everything is saved to `~/.config/anvil/config.json` (permissions 600). Rerun `anvil --setup` or type `/setup` inside Anvil to change it.

You can also use environment variables instead, which override the saved config:

```bash
export ANVIL_PROVIDER=openai          # or anthropic
export ANVIL_BASE_URL=https://your-gateway.example.com/v1
export ANVIL_API_KEY=your-key
anvil -m your-model-name
```

Notes for gateways:
- The key is sent as both `Authorization: Bearer` and `x-api-key`, so it works whichever header your gateway reads.
- With Anthropic-format gateways, Anvil first sends the full request (thinking, prompt caching, effort). If the gateway rejects it, Anvil switches to a basic request automatically and tells you. Start in basic mode with `--compat`.
- Tool calling must be supported by the model behind your gateway. Most current Claude, GPT, Gemini, Qwen, DeepSeek and Llama models support it.
- `/cost` always shows token counts. Dollar estimates appear only for known Claude models; for others, set `price_input` and `price_output` (USD per million tokens) in the config file.
- On OpenAI-format endpoints, `/effort low|medium|high|off` sets `reasoning_effort` (not sent by default).

## Anvil cloud: sign in with Google, settings everywhere

Instead of running `anvil --setup` on every machine, keep your provider, base URL, model, API key and other settings on your own server. Sign in once with Google and the terminal and desktop app fetch them.

**On a new machine:**

```bash
anvil login anvil.example.com   # shows a code, opens the browser, you approve with Google
anvil                           # settings come from the server
anvil whoami                    # which account and server this machine uses
anvil logout                    # sign out and revoke this machine's sign-in
```

In the desktop app: Settings → Anvil cloud → enter the server → Sign in with Google. While signed in, changes you make in Settings (or with `anvil --setup`) are saved to the server, so every machine gets them on its next start.

**What is stored where:** the server keeps your settings encrypted (Fernet, AES + HMAC) with a master key in a separate `0600` file. Each machine saves only the server address and a sign-in token in `~/.config/anvil/cloud.json` (readable only by you). The API key is fetched at startup and kept in memory, never written to disk. Sign-in tokens last 30 days from last use, short-lived access tokens 1 hour; revoke any machine from the server's web page. If the server is unreachable, Anvil falls back to the local settings and says so. Set `ANVIL_CLOUD=0` to ignore cloud settings for one run.

**Run the server** (e.g. on your Oracle Ubuntu box, behind Caddy for HTTPS):

1. Install with the cloud extra: `pip install 'anvil-code[cloud]'` (or `pip install cryptography` next to an existing install).
2. Create a Google OAuth client: in [Google Cloud Console](https://console.cloud.google.com/), open Google Auth Platform (APIs & Services → OAuth consent screen), set it up as External, and add your Gmail under Audience → Test users. Then Clients → Create client → **Web application**, with authorized redirect URI `https://anvil.example.com/auth/callback`. Only the `openid email profile` scopes are used, so no Google review is needed.
3. Run `anvil cloud init` and enter the public URL, client ID, client secret and the Google emails allowed to sign in. It writes `~/.config/anvil/cloud-server/server.json` (`0600`); the client secret can instead come from `ANVIL_GOOGLE_CLIENT_SECRET`.
4. Point Caddy at it (Caddy gets the HTTPS certificate):
   ```
   anvil.example.com {
       reverse_proxy 127.0.0.1:8787
   }
   ```
5. Start it: `anvil cloud serve`, or as a service with [`server/anvil-cloud.service`](server/anvil-cloud.service).
6. Open `https://anvil.example.com`, sign in, and save your provider, base URL, model and key.

Only emails in `allowed_emails` can sign in. Back up `master.key` together with `anvil-cloud.db`: without the key the saved settings can't be decrypted.

### Chat in the browser (run the agent on the server)

The server can also host the desktop app's chat at `https://anvil.example.com/app/`, behind the same Google sign-in, so you can run Anvil on the server from any browser or phone: sessions, streaming replies, Allow/Deny approvals, permission modes, skills and settings. It is off by default.

```bash
anvil cloud chat on --workspace ~/projects   # writes chat + chat_workspace to server.json
systemctl --user restart anvil-cloud         # or however you run `anvil cloud serve`
```

- The agent runs on the server **as the user the server runs as**, starting in `chat_workspace` (default: the home folder; a new session can use any folder). Treat chat access like shell access to that account.
- Who may chat: everyone in `allowed_emails`, or only the emails in `chat_emails` if you set it.
- It uses the settings saved for your account on the server; changing them in the chat's Settings saves them there too. Bash safety rules, `permission_ceiling`, `auto_approve` and the `sandbox` come from the server account's own `~/.config/anvil/config.json`, as in the terminal. Sessions are saved in `~/.config/anvil/sessions/`, shared with the terminal on that machine.
- Requests need the sign-in cookie, and anything that changes state also needs a per-sign-in token sent in a header, so other sites can't drive it. Sign-ins last 12 hours.
- If the server runs under systemd with `ProtectSystem=strict`, the agent can only write to `ReadWritePaths`: add `~/.config/anvil` and your workspace (see [`server/anvil-cloud.service`](server/anvil-cloud.service)). `NoNewPrivileges=true` also stops `sudo` in agent commands.
- Use Add to Home Screen on the phone to open it like an app.

## Use

```bash
cd your-project
anvil                        # interactive session
anvil "fix the failing test" # one-shot (edits/commands declined unless --yolo)
anvil -y "add a --verbose flag and run the tests"
anvil -c                     # continue the last session in this directory
anvil -m sonnet              # cheaper model for this run
cat error.log | anvil "why is this failing?"
```

The agent can read, search, create and edit files, and run shell commands. Before each edit it shows a diff, and before each command it shows a one-line plain description and the command (long ones are collapsed; press `v` to see all). Then press one key, no Enter needed: `y` (yes), `a` (yes, and stop asking for commands, or for file edits, this session), `n` (no), or just start typing to tell it what to do instead. Risky commands (deleting files, force pushes, protected paths) and writes outside the project always ask, even with `a` or `--yolo`.

**Modes:** press **Shift+Tab** at the prompt (or type `/auto`) to switch between *ask* (asks before every change), *edits* (file edits run by themselves, commands ask) and *auto* (everything runs, risky commands still ask). The current mode is shown under the prompt.

**Hinglish:** set `"language": "hinglish"` (asked in `anvil --setup`, or `/lang hinglish`, or in the desktop app and Anvil cloud settings) and Anvil replies in short, simple Hinglish, says in one line what it's doing and why, and the terminal prompts switch to Hinglish too. English is the default.

### Keys
- **Enter** sends, **Alt+Enter** (or Esc then Enter) adds a newline
- **Ctrl+C** interrupts the agent mid-turn; your next message redirects it
- **Shift+Tab** switches mode: ask → edits → auto
- **Ctrl+D**, `exit` (also `quit`, `bye`, `band karo`) or **Ctrl+C twice** at the prompt quits; the session is auto-saved

### Commands

| Command | What it does |
|---|---|
| `/model opus\|sonnet\|haiku\|fable\|<id>` | Switch model (saved as default) |
| `/effort low…max` | How hard the model thinks; lower is cheaper and faster |
| `/cost` | Spend and context size so far |
| `/compact` | Summarize the conversation to cut context (and cost) |
| `/clear` | New conversation |
| `/save [name]`, `/load name`, `/sessions` | Manage sessions |
| `/yolo` | Toggle auto-approve |
| `/auto [ask\|edits\|auto]` | Switch permission mode (same as Shift+Tab) |
| `/lang english\|hinglish` | Reply language (saved) |
| `/web` | Toggle web search (official Anthropic API only) |
| `/setup`, `/provider` | Change or show endpoint, model and key |
| `/skills` | List skills; `/skills sync`, `/skills add <git-url>` |
| `/<skill>` | Run a skill, e.g. `/review src/` |
| `/edit` | Write a long prompt in `$EDITOR` |
| `/init` | Have the agent write an `AGENTS.md` for the project |
| `!cmd` | Run a shell command yourself |

### Project instructions
If the project root has `AGENTS.md`, `CLAUDE.md` or `ANVIL.md`, its contents are added to the system prompt.

## Keeping costs down (prompt cache engine)

An agent re-sends the whole conversation on every request, so most of what you pay for is input that was already sent. Anvil's cache engine makes that repeated part cheap, and `/cost` (or the app's header) shows the hit rate and how much it saved.

- **Prompt caching, always on.** History that was already sent is billed as a cache read: 5% of the input price on Opus 5.5 and Sonnet 5.5, 2.5% on Fable 5.2, 10% on older models. On the official API Anvil puts a fixed cache point on the system prompt and tools, plus automatic caching that follows the conversation.
- **Works through gateways.** If an Anthropic-format gateway rejects the newer request fields, Anvil falls back to per-block cache markers (which most gateways accept) before giving up on caching. On OpenAI-format gateways, Claude models (`anthropic/...`, `claude-...`) get cache markers in the form OpenRouter and LiteLLM pass through. GPT, DeepSeek and Gemini cache automatically when the start of the prompt doesn't change, which Anvil keeps stable. Set `openai_cache_control` to `on` or `off` to override.
- **Keep-alive while you're away.** The cache expires after 5 minutes. If you take longer to reply or to approve something, Anvil refreshes it just before it expires with a zero-output request that bills only a cache read, instead of paying to rewrite the whole conversation (1.25x). It stops after `cache_keepalive_minutes` (default 30) of inactivity. Set it to 0 to turn this off.
- **Optional 1-hour cache.** `"cache_ttl": "1h"` keeps the cache for an hour. Writes then cost 2x instead of 1.25x, so it only pays off if you often pause longer than the keep-alive window.
- **Auto-compact.** When the context passes `auto_compact_tokens` (default 200,000), Anvil summarizes the conversation at the end of the turn, so long sessions don't get more expensive with every request. `/compact` does it by hand. Set it to 0 to turn this off.

What breaks the cache (each costs one full re-read): switching model, changing effort mid-conversation, or editing `AGENTS.md`/skills before starting a new session. Anvil tells you when you do the first two.

Other tips: use `-m sonnet` or `-m haiku` for routine work and `/effort medium` or `low`; start a new session (`/clear`) for unrelated tasks. Prices per million tokens (input / output): Opus 5.5 $4 / $20, Sonnet 5.5 $2 / $10, Haiku 5.5 $0.10 / $0.50.

## Development

```bash
pip install -e ".[browser,dev]" && python -m playwright install chromium
```

```bash
python -m pytest -q
```

The tests use fake model servers, so they need no API key. Browser and web-app tests run in real Chromium and skip themselves if it isn't installed. CI (`.github/workflows/ci.yml`) runs them on Linux, Windows and macOS on every push. `build-windows.yml` builds `Anvil.exe` on every push to `main` and checks that its browser agent starts Edge.

## Layout

- `anvil/agent.py`: the agent loop (tool calls, approvals, compaction)
- `anvil/backends.py`: Anthropic-format and OpenAI-format API clients
- `anvil/tools.py`: tool definitions and implementations
- `anvil/cli.py`: terminal UI, slash commands, sessions
- `anvil/config.py`: settings, model aliases, pricing
- `anvil/skills.py` and `anvil/builtin_skills/`: skills
- `anvil/browser.py`: browser agent tools (Playwright)
- `anvil/safety.py`: destructive-command detection, protected paths, policy limits, backups
- `anvil/shellparse.py`: the POSIX shell and cmd.exe parsers the safety checks use
- `anvil/sandbox.py`: the optional command sandbox (bubblewrap, firejail, sandbox-exec, Docker/Podman)
- `server/`: agent user setup, the Caddy helper and the Anvil cloud systemd unit
- `anvil/cloud/`: Anvil cloud server (Google sign-in, encrypted settings) and the `anvil login` client
- `android/`: Kotlin client and guide for Android apps
- `anvil/gui/`: desktop and web app (local server in `server.py`, interface in `static/index.html`)

Settings live in `~/.config/anvil/config.json`; sessions in `~/.config/anvil/sessions/`.
