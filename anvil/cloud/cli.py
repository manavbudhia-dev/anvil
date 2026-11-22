"""`anvil login`, `anvil logout`, `anvil whoami` and `anvil cloud ...` commands."""

import argparse
import json
import os
import sys
from pathlib import Path

from ..config import CONFIG_DIR
from . import client

SERVER_CONFIG = CONFIG_DIR / "cloud-server" / "server.json"

USAGE = """\
anvil login [SERVER]       sign this machine in to your Anvil cloud server (e.g. anvil login anvil.example.com)
anvil logout               sign out and forget the saved sign-in
anvil whoami               show which account and server this machine uses
anvil cloud init           create the server config (run on the server)
anvil cloud serve          run the Anvil cloud server
"""


def cmd_login(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="anvil login")
    p.add_argument("server", nargs="?", help="Server address, e.g. https://anvil.example.com")
    p.add_argument("--no-browser", action="store_true", help="Just print the link")
    a = p.parse_args(argv)
    server = a.server or os.environ.get("ANVIL_CLOUD_URL") or (client.load_creds() or {}).get("server")
    if not server:
        server = input("Anvil cloud server address (e.g. anvil.example.com): ").strip()
    if not server:
        print("No server given.", file=sys.stderr)
        return 1
    try:
        email = client.login(server, open_browser=not a.no_browser)
        settings = client.fetch_settings()
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
    except client.CloudError as e:
        print(f"Login failed: {e}", file=sys.stderr)
        return 1
    print(f"Signed in as {email}.")
    if settings.get("api_key"):
        print(f"Settings loaded: {settings.get('model', '?')} via {settings.get('base_url') or 'api.anthropic.com'}. "
              "Run anvil in a project folder.")
    else:
        print("No API key is saved on the server yet. Add one in the server's web page, "
              "or run anvil --setup here and it will be saved to the server.")
    return 0


def cmd_logout(argv: list[str]) -> int:
    print("Signed out." if client.logout() else "This machine wasn't signed in.")
    return 0


def cmd_whoami(argv: list[str]) -> int:
    creds = client.load_creds()
    if not creds:
        print("Not signed in. Run: anvil login <server>")
        return 1
    try:
        me = client.whoami()
    except client.CloudError as e:
        print(f"{creds.get('email')} on {creds['server']} (couldn't check: {e})")
        return 1
    print(f"{me.get('email')} on {creds['server']}")
    return 0


def cmd_cloud_init(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="anvil cloud init")
    p.add_argument("--config", default=str(SERVER_CONFIG))
    a = p.parse_args(argv)
    path = Path(a.config).expanduser()
    cur = json.loads(path.read_text()) if path.exists() else {}

    def ask(key, prompt, default=""):
        v = input(f"{prompt} [{cur.get(key, default)}]: ").strip()
        return v or cur.get(key, default)

    print("Create a Google OAuth client first (see README, 'Anvil cloud'). Its redirect URI must be\n"
          "<public url>/auth/callback.\n")
    cfg = {
        "public_url": ask("public_url", "Public https URL of this server").rstrip("/"),
        "google_client_id": ask("google_client_id", "Google OAuth client ID"),
        "google_client_secret": ask("google_client_secret", "Google OAuth client secret"),
        "allowed_emails": [e.strip().lower() for e in ask(
            "allowed_emails_text", "Allowed Google emails (comma separated)",
            ",".join(cur.get("allowed_emails", []))).split(",") if e.strip()],
        "host": cur.get("host", "127.0.0.1"),
        "port": int(ask("port", "Local port (Caddy proxies to it)", 8787)),
        "data_dir": cur.get("data_dir", str(path.parent)),
    }
    from ..fsutil import atomic_write_text
    atomic_write_text(path, json.dumps(cfg, indent=2), private=True)
    print(f"\nSaved {path} (readable only by you).")
    print(f"Start it with: anvil cloud serve --config {path}")
    print(f"Caddy: {cfg['public_url'].split('://')[-1]} {{ reverse_proxy 127.0.0.1:{cfg['port']} }}")
    return 0


def cmd_cloud_serve(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="anvil cloud serve")
    p.add_argument("--config", default=os.environ.get("ANVIL_CLOUD_CONFIG", str(SERVER_CONFIG)))
    a = p.parse_args(argv)
    import importlib.util
    if importlib.util.find_spec("cryptography") is None:
        print("The server needs the cryptography package: pip install 'anvil-code[cloud]'", file=sys.stderr)
        return 1
    from .server import CloudConfig, serve
    path = Path(a.config).expanduser()
    if not path.exists():
        print(f"No server config at {path}. Run: anvil cloud init", file=sys.stderr)
        return 1
    cfg = CloudConfig.load(path)
    if cfg.data_dir == "":
        cfg.data_dir = str(path.parent)
    problems = cfg.problems()
    if problems:
        for msg in problems:
            print(f"config error: {msg}", file=sys.stderr)
        return 1
    srv = serve(cfg)
    print(f"Anvil cloud listening on http://{cfg.host}:{cfg.port}, public at {cfg.public_url}")
    print(f"Allowed: {', '.join(cfg.allowed_emails)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def dispatch(argv: list[str]) -> int | None:
    """Handle a cloud subcommand, or return None if argv isn't one."""
    if not argv:
        return None
    cmd, rest = argv[0], argv[1:]
    if cmd == "login":
        return cmd_login(rest)
    if cmd == "logout":
        return cmd_logout(rest)
    if cmd == "whoami":
        return cmd_whoami(rest)
    if cmd == "cloud":
        sub = rest[0] if rest else ""
        if sub == "init":
            return cmd_cloud_init(rest[1:])
        if sub == "serve":
            return cmd_cloud_serve(rest[1:])
        print(USAGE)
        return 0 if sub in ("", "help", "-h", "--help") else 1
    return None
