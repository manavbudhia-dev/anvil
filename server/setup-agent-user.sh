#!/usr/bin/env bash
# One-time server hardening before letting Anvil run commands. Run as root:
#
#   sudo bash server/setup-agent-user.sh
#
# What it does:
#   1. Creates a separate, non-root user for the agent (default: anvil), with no password.
#   2. Gives that user sudo for an explicit list of commands only: restart/reload of the services
#      you name, and the anvil-caddy-apply helper (backup + validate + reload + rollback for Caddy).
#   3. Writes /etc/anvil/policy.json so Anvil stays in "Ask before edits" mode for every user on
#      this machine. The agent can't change it because it's owned by root.
#   4. Installs Anvil root-owned in /opt/anvil, so the agent can't edit Anvil's own safety code.
#
# Settings (environment variables):
#   AGENT_USER=anvil                 user to create
#   SERVICES="caddy"                 services the agent may restart/reload, space-separated (e.g. "caddy myapp")
#   CADDYFILE=/etc/caddy/Caddyfile   Caddy config path
#   CEILING=ask                      most permissive Anvil mode allowed: ask | edits | auto
#   COPY_SSH_FROM=$SUDO_USER         copy this user's authorized_keys so you can ssh in as the agent user
#   APP_DIRS=""                      folders to give the agent user ownership of (e.g. "/srv/myapp")
#   INSTALL_ANVIL=1                  install Anvil into /opt/anvil (needs install.sh in the parent folder)
set -Eeuo pipefail

AGENT_USER="${AGENT_USER:-anvil}"
SERVICES="${SERVICES:-caddy}"
CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"
CEILING="${CEILING:-ask}"
COPY_SSH_FROM="${COPY_SSH_FROM:-${SUDO_USER:-}}"
APP_DIRS="${APP_DIRS:-}"
INSTALL_ANVIL="${INSTALL_ANVIL:-1}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root: sudo bash $0"
case "$CEILING" in ask|edits|auto) ;; *) die "CEILING must be ask, edits or auto";; esac
[[ "$AGENT_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "invalid user name: $AGENT_USER"
command -v sudo >/dev/null || die "sudo is not installed"
command -v visudo >/dev/null || die "visudo is not installed"

# 1. The user -----------------------------------------------------------------
if id "$AGENT_USER" >/dev/null 2>&1; then
  say "User $AGENT_USER already exists"
else
  say "Creating user $AGENT_USER"
  useradd --create-home --shell /bin/bash "$AGENT_USER"
fi
passwd -l "$AGENT_USER" >/dev/null 2>&1 || true   # no password login; use SSH keys or sudo -iu
HOME_DIR="$(getent passwd "$AGENT_USER" | cut -d: -f6)"

if [ -n "$COPY_SSH_FROM" ] && [ "$COPY_SSH_FROM" != "$AGENT_USER" ]; then
  SRC_KEYS="$(getent passwd "$COPY_SSH_FROM" | cut -d: -f6)/.ssh/authorized_keys"
  if [ -f "$SRC_KEYS" ]; then
    install -d -m 700 -o "$AGENT_USER" -g "$AGENT_USER" "$HOME_DIR/.ssh"
    install -m 600 -o "$AGENT_USER" -g "$AGENT_USER" "$SRC_KEYS" "$HOME_DIR/.ssh/authorized_keys"
    say "Copied SSH keys from $COPY_SSH_FROM (ssh $AGENT_USER@this-server now works with the same key)"
  fi
fi

# Logs without sudo.
for g in systemd-journal adm; do getent group "$g" >/dev/null && usermod -aG "$g" "$AGENT_USER"; done

# 2. Caddy helper and sudo rules ------------------------------------------------
HELPER=/usr/local/sbin/anvil-caddy-apply
if command -v caddy >/dev/null || [ -f "$CADDYFILE" ]; then
  say "Installing $HELPER (Caddyfile: $CADDYFILE)"
  sed "s|@CADDYFILE@|$CADDYFILE|" "$HERE/anvil-caddy-apply" > "$HELPER.tmp"
  chown root:root "$HELPER.tmp"; chmod 755 "$HELPER.tmp"; mv "$HELPER.tmp" "$HELPER"
  HAVE_CADDY=1
else
  warn "Caddy not found; skipping the Caddy helper"
  HAVE_CADDY=0
fi

SYSTEMCTL="$(command -v systemctl || echo /usr/bin/systemctl)"
SUDOERS="/etc/sudoers.d/anvil-agent-$AGENT_USER"
TMP="$(mktemp)"
{
  echo "# Managed by Anvil's setup-agent-user.sh. Only these exact commands are allowed."
  echo "Defaults:$AGENT_USER secure_path=\"/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\""
  for svc in $SERVICES; do
    [[ "$svc" =~ ^[A-Za-z0-9@._-]+$ ]] || die "invalid service name: $svc"
    echo "$AGENT_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart $svc, $SYSTEMCTL reload $svc, $SYSTEMCTL reload-or-restart $svc"
  done
  if [ "$HAVE_CADDY" = 1 ]; then
    echo "$AGENT_USER ALL=(root) NOPASSWD: $HELPER"
    echo "$AGENT_USER ALL=(root) NOPASSWD: $HELPER *"
  fi
} > "$TMP"
visudo -cf "$TMP" >/dev/null || { rm -f "$TMP"; die "generated sudo rules failed validation; nothing changed"; }
install -m 440 -o root -g root "$TMP" "$SUDOERS"; rm -f "$TMP"
say "Sudo rules written to $SUDOERS"

# Make sure the agent user isn't in an all-powerful group.
for g in wheel sudo admin; do
  if id -nG "$AGENT_USER" | tr ' ' '\n' | grep -qx "$g"; then
    gpasswd -d "$AGENT_USER" "$g" >/dev/null && warn "removed $AGENT_USER from group $g (full sudo)"
  fi
done

# 3. Anvil policy -----------------------------------------------------------------
install -d -m 755 -o root -g root /etc/anvil
printf '{\n  "permission_ceiling": "%s"\n}\n' "$CEILING" > /etc/anvil/policy.json
chown root:root /etc/anvil/policy.json; chmod 644 /etc/anvil/policy.json
say "Anvil permission limit set to '$CEILING' in /etc/anvil/policy.json"

for d in $APP_DIRS; do
  if [ -d "$d" ]; then chown -R "$AGENT_USER:$AGENT_USER" "$d"; say "Gave $AGENT_USER ownership of $d"
  else warn "$d doesn't exist; skipped"; fi
done

# 4. Install Anvil root-owned, so the agent can run it but not edit its code ------
if [ "$INSTALL_ANVIL" = "1" ] && [ -f "$HERE/../install.sh" ]; then
  say "Installing Anvil into /opt/anvil (owned by root, usable by everyone)"
  ( umask 022; ANVIL_PREFIX=/opt/anvil ANVIL_NO_SETUP=1 bash "$HERE/../install.sh" 2>&1 | grep -E "==>|warning|error" ) || true
  if [ -d "$HOME_DIR/.anvil/venv" ]; then
    warn "$AGENT_USER also has a personal install in ~/.anvil that it could modify; remove it with: rm -rf $HOME_DIR/.anvil $HOME_DIR/.local/bin/anvil*"
  fi
fi

echo
say "Done. Allowed sudo commands for $AGENT_USER:"
sudo -l -U "$AGENT_USER" | sed -n '/may run the following/,$p' | tail -n +2
cat <<EOF

Next:
  sudo -iu $AGENT_USER          # switch to the agent user (or: ssh $AGENT_USER@this-server)
  anvil --setup                 # connect your API
  cd ~/your-project && anvil

For Caddy, the agent edits a copy (cp $CADDYFILE ~/Caddyfile), then runs:
  sudo anvil-caddy-apply apply ~/Caddyfile     # backs up, validates, reloads, rolls back on failure
  sudo anvil-caddy-apply rollback              # undo the last change
EOF
