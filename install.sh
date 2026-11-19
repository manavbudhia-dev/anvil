#!/usr/bin/env bash
# Anvil installer for Linux (Oracle Linux, RHEL, Fedora, Ubuntu, Debian) and macOS.
#
# From GitHub (one line):
#   curl -fsSL https://raw.githubusercontent.com/OWNER/REPO/main/install.sh | bash
#   (set DEFAULT_REPO below first, or pass ANVIL_REPO=https://github.com/OWNER/REPO.git)
# From an unzipped copy:
#   bash anvil/install.sh
#
# Running it again updates Anvil. Optional settings (environment variables):
#   ANVIL_REPO      git URL to install from (https or git@ for private repos)
#   ANVIL_REF       branch or tag to install (default: the repo's default branch)
#   ANVIL_PREFIX    install folder (default: ~/.anvil)
#   ANVIL_DESKTOP=1 also install the native-window desktop app (pywebview)
#   ANVIL_BROWSER=1 also install the browser agent (Playwright + Chromium)
#   ANVIL_PROVIDER, ANVIL_BASE_URL, ANVIL_MODEL, ANVIL_API_KEY
#                   save these settings and skip the interactive setup
#   ANVIL_NO_SETUP=1 don't run the setup wizard at the end
set -Eeuo pipefail

# After you push Anvil to GitHub, put its URL here so the one-liner needs no ANVIL_REPO.
DEFAULT_REPO=""

PREFIX="${ANVIL_PREFIX:-$HOME/.anvil}"
SRC="$PREFIX/src"
VENV="$PREFIX/venv"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -eq 0 ]; then SUDO=""; elif command -v sudo >/dev/null; then SUDO="sudo"; else SUDO=""; fi
OS="$(uname -s)"

find_python() {
  for py in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$py" >/dev/null && "$py" -c 'import sys, venv; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
      command -v "$py"; return 0
    fi
  done
  return 1
}

install_packages() {
  if [ "$OS" = "Darwin" ]; then
    command -v brew >/dev/null || die "Install Homebrew first (https://brew.sh), then run this again."
    say "Installing packages with Homebrew"
    brew install python@3.12 git ripgrep tmux >/dev/null
    return
  fi
  if [ -z "$SUDO" ] && [ "$(id -u)" -ne 0 ]; then
    warn "not root and no sudo; skipping system packages (need python3.10+, git)"
    return
  fi
  if command -v dnf >/dev/null || command -v yum >/dev/null; then
    local pm; pm="$(command -v dnf || command -v yum)"
    say "Installing packages with $(basename "$pm")"
    $SUDO "$pm" install -y git tar unzip tmux >/dev/null
    if ! find_python >/dev/null; then
      $SUDO "$pm" install -y python3.12 python3.12-pip >/dev/null 2>&1 \
        || $SUDO "$pm" install -y python3.11 python3.11-pip >/dev/null 2>&1 \
        || $SUDO "$pm" install -y python3 python3-pip >/dev/null
    fi
    $SUDO "$pm" install -y ripgrep >/dev/null 2>&1 || true   # optional; needs EPEL on Oracle Linux/RHEL
  elif command -v apt-get >/dev/null; then
    say "Installing packages with apt"
    export DEBIAN_FRONTEND=noninteractive
    $SUDO apt-get update -qq >/dev/null
    $SUDO apt-get install -y -qq git unzip tmux python3 python3-venv python3-pip >/dev/null
    $SUDO apt-get install -y -qq ripgrep >/dev/null 2>&1 || true
  elif command -v apk >/dev/null; then
    say "Installing packages with apk"
    $SUDO apk add --no-cache git bash tmux python3 py3-pip ripgrep >/dev/null
  else
    warn "unknown package manager; make sure python3.10+ and git are installed"
  fi
}

# Where the source comes from: an explicit repo, else the folder this script sits in.
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
REPO="${ANVIL_REPO:-}"
if [ -z "$REPO" ] && { [ -z "$SCRIPT_DIR" ] || [ ! -f "$SCRIPT_DIR/pyproject.toml" ]; }; then REPO="$DEFAULT_REPO"; fi
if [ -z "$REPO" ] && [ -d "$SRC/.git" ]; then
  REPO="$(git -C "$SRC" remote get-url origin 2>/dev/null || true)"
fi
if [ -z "$REPO" ] && { [ -z "$SCRIPT_DIR" ] || [ ! -f "$SCRIPT_DIR/pyproject.toml" ]; }; then
  die "set ANVIL_REPO to Anvil's git URL, or run install.sh from an unzipped copy of Anvil"
fi

install_packages
PY="$(find_python)" || die "Python 3.10 or newer is required (found: $(python3 --version 2>&1 || echo none))"
say "Using $($PY --version) at $PY"

mkdir -p "$PREFIX"
if [ -n "$REPO" ]; then
  if [ -d "$SRC/.git" ]; then
    say "Updating Anvil from $REPO"
    git -C "$SRC" fetch -q --depth 1 origin ${ANVIL_REF:+"$ANVIL_REF"}
    git -C "$SRC" reset -q --hard FETCH_HEAD
  else
    say "Downloading Anvil from $REPO"
    rm -rf "$SRC"
    git clone -q --depth 1 ${ANVIL_REF:+--branch "$ANVIL_REF"} "$REPO" "$SRC"
  fi
else
  say "Installing Anvil from $SCRIPT_DIR"
  if [ "$SCRIPT_DIR" != "$SRC" ]; then
    rm -rf "$SRC" && mkdir -p "$SRC"
    (cd "$SCRIPT_DIR" && tar --exclude .git --exclude '*.egg-info' --exclude __pycache__ --exclude build --exclude dist -cf - .) | tar -xf - -C "$SRC"
  fi
fi

if [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  say "Creating Python environment in $VENV"
  rm -rf "$VENV"
  "$PY" -m venv "$VENV"
fi
say "Installing Python packages (this can take a minute)"
LOG="$PREFIX/install.log"
pipi() { "$VENV/bin/python" -m pip install --upgrade "$@" >>"$LOG" 2>&1; }
: > "$LOG"
pipi pip || { tail -20 "$LOG" >&2; die "pip upgrade failed (full log: $LOG)"; }
pipi "$SRC" || { tail -20 "$LOG" >&2; die "installing Anvil failed (full log: $LOG)"; }
if [ "${ANVIL_BROWSER:-0}" = "1" ]; then
  say "Installing the browser agent (Playwright + Chromium)"
  "$VENV/bin/anvil" --install-browser >>"$LOG" 2>&1 || warn "browser tools didn't install (see $LOG); try later: anvil --install-browser"
fi
if [ "${ANVIL_DESKTOP:-0}" = "1" ]; then
  pipi pywebview || warn "pywebview didn't install; anvil-desktop will open in your browser instead"
fi

# Put the commands on PATH.
if [ "$(id -u)" -eq 0 ]; then BIN="/usr/local/bin"; else BIN="$HOME/.local/bin"; fi
mkdir -p "$BIN"
ln -sf "$VENV/bin/anvil" "$BIN/anvil"
ln -sf "$VENV/bin/anvil-desktop" "$BIN/anvil-desktop"
case ":$PATH:" in
  *":$BIN:"*) PATH_NOTE="" ;;
  *)
    for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
      if [ -f "$rc" ] || [ "$rc" = "$HOME/.bashrc" ]; then
        grep -qs "anvil installer" "$rc" || printf '\nexport PATH="%s:$PATH"  # added by anvil installer\n' "$BIN" >> "$rc"
      fi
    done
    PATH_NOTE="Open a new terminal (or run: export PATH=\"$BIN:\$PATH\") so the anvil command is found."
    ;;
esac

# Settings: save the given ones, or run the wizard if we can talk to the user.
if [ -n "${ANVIL_API_KEY:-}" ] || [ -n "${ANVIL_BASE_URL:-}" ] || [ -n "${ANVIL_MODEL:-}" ]; then
  "$VENV/bin/python" - <<'PYEOF'
import os
from anvil.config import Config, resolve_model
c = Config.load()
if os.environ.get("ANVIL_PROVIDER"):
    c.provider = "openai" if os.environ["ANVIL_PROVIDER"].lower().startswith(("openai", "oai")) else "anthropic"
if os.environ.get("ANVIL_BASE_URL"):
    url = os.environ["ANVIL_BASE_URL"].rstrip("/")
    c.base_url = url[:-3] if c.provider == "anthropic" and url.endswith("/v1") else url
if os.environ.get("ANVIL_MODEL"):
    c.model = resolve_model(os.environ["ANVIL_MODEL"])
if os.environ.get("ANVIL_API_KEY"):
    c.api_key = os.environ["ANVIL_API_KEY"]
c.save()
print("==> Saved settings to ~/.config/anvil/config.json")
PYEOF
elif [ "${ANVIL_NO_SETUP:-0}" != "1" ] && ! "$VENV/bin/python" -c 'from anvil.config import Config; import sys; sys.exit(0 if Config.load().resolved_key() else 1)' 2>/dev/null; then
  if [ -r /dev/tty ] && ( : < /dev/tty ) 2>/dev/null; then
    say "Let's connect Anvil to your API"
    "$VENV/bin/anvil" --setup < /dev/tty || warn "setup didn't finish; run: anvil --setup"
  else
    warn "no terminal available for setup; run: anvil --setup"
  fi
fi

echo
say "Anvil $("$VENV/bin/python" -c 'import anvil; print(anvil.__version__)') is installed."
[ -n "${PATH_NOTE:-}" ] && echo "    $PATH_NOTE"
echo "    Start it in a project folder:  cd your-project && anvil"
echo "    Desktop app:                    anvil-desktop   (on a server: anvil-desktop --browser --port 8790)"
echo "    Update later:                   run this installer again"
