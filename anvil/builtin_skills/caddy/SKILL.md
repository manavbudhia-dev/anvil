---
name: caddy
description: Change the Caddy web server config (sites, reverse proxies, TLS, redirects) safely, with backup, validation, reload and rollback.
---
# Changing Caddy safely

Never edit the live Caddyfile in place, and never reload without validating.

1. Find the live config: usually `/etc/caddy/Caddyfile` (`systemctl cat caddy` shows the `--config` path). Read it, and any files it imports.
2. Work on a copy in your home folder: `cp /etc/caddy/Caddyfile ~/Caddyfile.new`, then edit `~/Caddyfile.new` with edit_file.
3. If the helper exists (`command -v anvil-caddy-apply`):
   - `sudo anvil-caddy-apply diff ~/Caddyfile.new` and show the user the change.
   - `sudo anvil-caddy-apply validate ~/Caddyfile.new`. Fix any errors and repeat.
   - `sudo anvil-caddy-apply apply ~/Caddyfile.new`. It backs up the current config, installs the new one, reloads Caddy, and restores the backup automatically if the reload fails.
   - To undo later: `sudo anvil-caddy-apply rollback`.
4. Without the helper:
   - Back up first: `sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak.$(date +%Y%m%d-%H%M%S)`.
   - Validate the copy: `caddy validate --config ~/Caddyfile.new --adapter caddyfile`.
   - Only if valid, install it and reload (prefer reload over restart): `sudo systemctl reload caddy`.
   - If the reload fails, put the backup back and reload again.
5. Verify: `systemctl status caddy --no-pager`, then `curl -sSI https://<site>` for each site you touched. Report the result.

Tips: `caddy fmt --overwrite ~/Caddyfile.new` tidies formatting. A new domain needs DNS pointing at the server and ports 80/443 open before Caddy can get a certificate. Changing the firewall is a separate, destructive step: ask the user first.
