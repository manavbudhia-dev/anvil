---
name: deploy-setup
description: Set up automatic redeploys of this project to a Linux server (e.g. Oracle Cloud) on every GitHub push, with build, restart, health check and rollback.
---
# Deploy setup

Goal: after this, every push to the main branch redeploys the app on the user's server, and a failed deploy rolls back automatically.

The kit is bundled with this skill:
- `deploy.sh`: runs on the server inside the app's git checkout. Fetches, builds, restarts, health-checks, rolls back on failure.
- `deploy.env.example`: per-server settings for deploy.sh.
- `deploy.yml`: GitHub Actions workflow that SSHes to the server and runs deploy.sh.

## Steps

1. Inspect the project to learn how it builds and runs: look for docker-compose/compose files, package.json scripts, requirements.txt/pyproject.toml, go.mod, a Procfile, systemd unit files, pm2 ecosystem files, and the README. Identify the default branch (`git symbolic-ref refs/remotes/origin/HEAD` or ask).
2. Copy the kit into the project:
   - `deploy.sh` to the project root, keep it executable (`chmod +x deploy.sh`).
   - `deploy.env.example` to the project root.
   - `deploy.yml` to `.github/workflows/deploy.yml`. If the default branch is not `main`, change `branches: [main]`.
   Read the bundled files and write them with write_file; do not rewrite their logic.
3. Fill in `deploy.env.example` for this project: the right BUILD_CMD (keep `auto` if detection fits), RESTART (`docker` for compose apps, `systemd:<unit>` or `pm2:<name>` otherwise) and a HEALTHCHECK_URL if the app has a health or home endpoint. Add `deploy.env`, `.deploy.log` and `.deploy.lock` to `.gitignore`.
4. If the app has no process manager yet and isn't using Docker, offer a systemd unit file (e.g. `deploy/myapp.service`) with the right working directory, start command and `Restart=on-failure`, and explain how to install it on the server. Don't install anything on a server yourself unless the user asks and you are running on that server.
5. Commit only if the user asks.
6. Finish by giving the user the one-time server and GitHub setup below, filled in with this project's details (repo URL, app folder, branch, service name). Keep it as copy-pasteable commands.

## One-time setup to give the user

On the server (user `opc` on Oracle Linux, `ubuntu` on Ubuntu):
1. Let the server pull the repo. For a private repo, create a read-only deploy key:
   `ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N ""`, add `~/.ssh/github_deploy.pub` under the GitHub repo's Settings > Deploy keys, and add to `~/.ssh/config`:
   ```
   Host github.com
     IdentityFile ~/.ssh/github_deploy
   ```
2. Clone once: `git clone git@github.com:OWNER/REPO.git ~/REPO`, then `cd ~/REPO && cp deploy.env.example deploy.env`, edit deploy.env, and run `./deploy.sh` once by hand (FORCE=1 ./deploy.sh if it says nothing to do).
3. If RESTART uses systemd, allow the restart without a password prompt: `echo "$USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart UNIT" | sudo tee /etc/sudoers.d/deploy-UNIT`.

Key for GitHub Actions to reach the server (run on the user's own computer):
4. `ssh-keygen -t ed25519 -f deploy_actions -N ""`, then append `deploy_actions.pub` to the server's `~/.ssh/authorized_keys`.
5. In the GitHub repo, Settings > Secrets and variables > Actions, add: `DEPLOY_HOST` (server IP), `DEPLOY_USER`, `DEPLOY_SSH_KEY` (contents of `deploy_actions`), `DEPLOY_KNOWN_HOSTS` (output of `ssh-keyscan -t ed25519 SERVER_IP`), `DEPLOY_PATH` (e.g. `/home/opc/REPO`).
6. Push to main, or run the Deploy workflow from the Actions tab. Logs are in the Actions run and in `.deploy.log` on the server.

Optional: under Settings > Environments > production, add required reviewers so each deploy waits for approval.

## Safety
- deploy.sh refuses to run if the server checkout has uncommitted changes, so it never silently discards work.
- Never put secrets in deploy.env or the workflow; use GitHub secrets or the server's own environment.
