import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from ..config import Config
from .server import App, load_token, print_qr, start_server

DEFAULT_REMOTE_PORT = 8790


def say(msg: str) -> None:
    if sys.stdout is not None:  # a windowed .exe has no console
        print(msg, flush=True)


def tailscale_name() -> str:
    """This machine's Tailscale DNS name, e.g. myserver.tail1234.ts.net."""
    exe = shutil.which("tailscale")
    if not exe:
        raise RuntimeError("Tailscale isn't installed. Install it: curl -fsSL https://tailscale.com/install.sh | sh, "
                           "then run: sudo tailscale up")
    proc = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        raise RuntimeError(f"tailscale status failed: {proc.stderr.strip() or proc.stdout.strip()}. Is it running? sudo tailscale up")
    name = (json.loads(proc.stdout).get("Self") or {}).get("DNSName", "").rstrip(".")
    if not name:
        raise RuntimeError("couldn't find this machine's Tailscale name; run: sudo tailscale up")
    return name


def setup_tailscale(config: Config, port: int) -> str:
    """Serve the local app on this machine's Tailscale HTTPS address (only reachable inside your tailnet)."""
    name = tailscale_name()
    exe = shutil.which("tailscale")
    proc = subprocess.run([exe, "serve", "--bg", f"http://127.0.0.1:{port}"], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip()
        hint = ""
        if "access denied" in msg.lower() or "permission" in msg.lower():
            hint = f"\nAllow your user to manage Tailscale once: sudo tailscale set --operator={os.environ.get('USER', '$USER')}"
        elif "https" in msg.lower() or "cert" in msg.lower():
            hint = "\nTurn on HTTPS certificates for your tailnet: Tailscale admin console > DNS > Enable HTTPS (and MagicDNS)."
        raise RuntimeError(f"tailscale serve failed: {msg}{hint}")
    url = f"https://{name}/"
    if name not in config.web_allowed_hosts:
        config.web_allowed_hosts.append(name)
    config.web_public_url = url
    config.save()
    return url


def install_service(root: Path, port: int) -> Path:
    """A systemd user service that keeps the web app running, also after reboots."""
    exe = shutil.which("anvil-desktop") or f"{sys.executable} -m anvil.gui.app"
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "anvil-web.service"
    unit.write_text(f"""[Unit]
Description=Anvil web app (phone and remote access)
After=network-online.target

[Service]
ExecStart={exe} --browser --no-open --port {port} -C {root}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
""")
    for cmd in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", "anvil-web.service"]):
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd)} failed: {proc.stderr.strip()}")
    return unit


def main() -> None:
    parser = argparse.ArgumentParser(prog="anvil-desktop", description="Anvil desktop and web app")
    parser.add_argument("-C", "--cwd", default=None, help="Default project folder")
    parser.add_argument("--browser", action="store_true", help="Open in your web browser instead of a native window")
    parser.add_argument("--no-open", action="store_true", help="Don't open a browser (for servers)")
    parser.add_argument("--port", type=int, default=0, help=f"Local port (default: random, or {DEFAULT_REMOTE_PORT} for remote access)")
    parser.add_argument("--tailscale", action="store_true", help="Make the app reachable from your phone over Tailscale")
    parser.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                        help="Accept requests for this host name (for a reverse proxy or tunnel). Repeatable; saved.")
    parser.add_argument("--public-url", help="The URL phones use to reach this app (shown in the QR code); saved")
    parser.add_argument("--install-service", action="store_true", help="Run the web app as a background service (systemd)")
    parser.add_argument("--rotate-token", action="store_true", help="Make a new access token; paired phones and apps must reconnect")
    parser.add_argument("--qr", action="store_true", help="Print the connect QR code and exit")
    parser.add_argument("--browser-check", metavar="FILE", help="Start the browser agent's browser, write the result to FILE, and exit")
    args = parser.parse_args()

    config = Config.load()
    from ..cloud.client import load_cloud_settings
    if load_cloud_settings(config, notice=say):
        say(f"Settings from Anvil cloud ({config.cloud_email})")
    if args.browser_check:
        from ..browser import self_test
        try:
            result, code = self_test(config), 0
        except Exception as e:  # report any failure in the file; a windowed exe has no console
            result, code = f"FAILED: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}", 1
        Path(args.browser_check).write_text(result + "\n")
        say(result)
        sys.exit(code)
    remote = args.tailscale or args.install_service or args.allow_host or args.public_url
    port = args.port or (DEFAULT_REMOTE_PORT if remote or config.web_public_url else 0)

    if args.rotate_token:
        load_token(rotate=True)
        say("Made a new access token. Reconnect your phone or app with the new QR code (anvil-desktop --qr).")
    for h in args.allow_host:
        if h.lower() not in config.web_allowed_hosts:
            config.web_allowed_hosts.append(h.lower())
    if args.public_url:
        config.web_public_url = args.public_url.rstrip("/") + "/"
    if args.allow_host or args.public_url:
        config.save()

    if args.cwd:
        root = Path(args.cwd).expanduser().resolve()
    elif getattr(sys, "frozen", False):
        # Double-clicked .exe: the working directory is wherever the exe lives, so use the
        # last project folder instead (or the home folder the first time).
        last = Path(config.last_root) if config.last_root else None
        root = last if last and last.is_dir() else Path.home()
    else:
        root = Path.cwd()

    if args.tailscale:
        try:
            say(f"Phone address: {setup_tailscale(config, port)}")
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            say(f"Tailscale setup failed: {e}")
            sys.exit(1)

    if args.install_service:
        try:
            unit = install_service(root, port)
        except (RuntimeError, OSError) as e:
            say(f"Couldn't install the service: {e}")
            sys.exit(1)
        user = os.environ.get("USER", "$USER")
        say(f"Installed and started {unit}.\nTo keep it running after you log out and after reboots, run once:\n"
            f"  sudo loginctl enable-linger {user}")
        args.qr = True

    if args.qr:
        app = App(config, root)
        app.port = port
        say(f"Scan with your phone (Tailscale must be on there too):\n{app.connect_url()}")
        print_qr(app.connect_url())
        return

    app = App(config, root)
    server = start_server(app, port)
    url = f"http://127.0.0.1:{server.server_address[1]}/#token={app.token}"

    webview = None
    if not args.browser and not args.no_open:
        try:
            import webview  # pywebview: optional native window
        except ImportError:
            say("pywebview isn't installed, so opening in your browser instead.\n"
                "For a native window: pip install pywebview")
    if webview is not None:
        window = webview.create_window("Anvil", url, width=1280, height=840, min_size=(760, 520))

        def pick_folder():
            result = window.create_file_dialog(webview.FOLDER_DIALOG)
            return result[0] if result else None

        app.pick_folder = pick_folder
        webview.start()
        return

    say(f"Anvil is running at {url}")
    if app.public_url():
        say(f"From your phone: {app.connect_url()}")
        if sys.stdout is not None and sys.stdout.isatty():
            print_qr(app.connect_url())
    say("Press Ctrl+C to quit.")
    if not args.no_open:
        webbrowser.open(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
