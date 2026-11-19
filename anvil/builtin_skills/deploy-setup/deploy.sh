#!/usr/bin/env bash
# Deploys this repository on the server it lives on: fetch, build, restart, health check.
# If any step fails, it rolls back to the commit that was running before and restarts that.
#
#   ./deploy.sh            deploy the latest commit of $BRANCH
#   ./deploy.sh <commit>   deploy a specific commit (GitHub Actions passes the pushed commit)
#
# Settings come from deploy.env next to this script (see deploy.env.example).
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"
[ -f deploy.env ] && . ./deploy.env

BRANCH="${BRANCH:-main}"
REMOTE="${REMOTE:-origin}"
BUILD_CMD="${BUILD_CMD:-auto}"          # auto | none | any shell command
RESTART="${RESTART:-auto}"              # auto | none | systemd:<unit> | pm2:<name> | docker | any shell command
HEALTHCHECK_URL="${HEALTHCHECK_URL:-}"  # e.g. http://127.0.0.1:3000/health
HEALTHCHECK_CMD="${HEALTHCHECK_CMD:-}"  # alternative to the URL: any command that exits 0 when healthy
HEALTHCHECK_TIMEOUT="${HEALTHCHECK_TIMEOUT:-60}"
LOG_FILE="${LOG_FILE:-$APP_DIR/.deploy.log}"

log() { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_FILE" >&2; }
die() { log "ERROR: $*"; exit 1; }

# One deploy at a time.
exec 9>"$APP_DIR/.deploy.lock"
command -v flock >/dev/null && { flock -n 9 || die "another deploy is already running"; }

[ -d .git ] || die "$APP_DIR is not a git checkout"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  die "there are uncommitted changes on the server; commit or discard them first (git status)"
fi

compose() { if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi; }
has_compose() { ls docker-compose.yml docker-compose.yaml compose.yml compose.yaml >/dev/null 2>&1; }

build() {
  case "$BUILD_CMD" in
    none) return 0 ;;
    auto) ;;
    *) log "build: $BUILD_CMD"; bash -c "$BUILD_CMD" || return 1; return 0 ;;
  esac
  if has_compose; then
    log "build: docker compose build"; compose build
  elif [ -f package.json ]; then
    if [ -f pnpm-lock.yaml ]; then log "build: pnpm"; pnpm install --frozen-lockfile; pnpm run --if-present build
    elif [ -f yarn.lock ]; then log "build: yarn"; yarn install --frozen-lockfile; if grep -q '"build"[[:space:]]*:' package.json; then yarn run build; fi
    else log "build: npm"; if [ -f package-lock.json ]; then npm ci; else npm install; fi; npm run build --if-present; fi
  elif [ -f requirements.txt ] || [ -f pyproject.toml ]; then
    [ -d .venv ] || python3 -m venv .venv
    log "build: python (.venv)"
    .venv/bin/pip install -q --upgrade pip
    if [ -f requirements.txt ]; then .venv/bin/pip install -q -r requirements.txt; else .venv/bin/pip install -q .; fi
  elif [ -f go.mod ]; then
    log "build: go"; go build ./...
  else
    log "build: nothing to build (no compose file, package.json, requirements.txt, pyproject.toml or go.mod)"
  fi
}

restart() {
  case "$RESTART" in
    none) log "restart: skipped (RESTART=none)" ;;
    auto)
      if has_compose; then log "restart: docker compose up -d"; compose up -d --remove-orphans
      else log "restart: nothing to restart. Set RESTART in deploy.env (systemd:<unit>, pm2:<name>, docker or a command)."; fi ;;
    docker) log "restart: docker compose up -d"; compose up -d --remove-orphans ;;
    systemd:*) log "restart: systemctl restart ${RESTART#systemd:}"; sudo systemctl restart "${RESTART#systemd:}" ;;
    pm2:*) log "restart: pm2 reload ${RESTART#pm2:}"; pm2 reload "${RESTART#pm2:}" --update-env ;;
    *) log "restart: $RESTART"; bash -c "$RESTART" ;;
  esac
}

healthy() {
  if [ -z "$HEALTHCHECK_URL" ] && [ -z "$HEALTHCHECK_CMD" ]; then return 0; fi
  local waited=0
  log "health check (up to ${HEALTHCHECK_TIMEOUT}s)"
  while [ "$waited" -lt "$HEALTHCHECK_TIMEOUT" ]; do
    if [ -n "$HEALTHCHECK_URL" ] && curl -fsS -o /dev/null --max-time 5 "$HEALTHCHECK_URL"; then return 0; fi
    if [ -n "$HEALTHCHECK_CMD" ] && bash -c "$HEALTHCHECK_CMD" >/dev/null 2>&1; then return 0; fi
    sleep 3; waited=$((waited + 3))
  done
  return 1
}

PREVIOUS="$(git rev-parse HEAD)"
log "fetching $REMOTE/$BRANCH"
git fetch --prune "$REMOTE" "+refs/heads/$BRANCH:refs/remotes/$REMOTE/$BRANCH"
TARGET="$(git rev-parse --verify "${1:-$REMOTE/$BRANCH}^{commit}")" || die "unknown commit ${1:-$REMOTE/$BRANCH}"

if [ "$TARGET" = "$PREVIOUS" ] && [ "${FORCE:-0}" != "1" ]; then
  log "already at $(git log -1 --format='%h %s' "$TARGET"); nothing to do (FORCE=1 to redeploy anyway)"
  exit 0
fi

rollback() {
  trap - ERR
  log "deploy of ${TARGET:0:7} failed; rolling back to ${PREVIOUS:0:7}"
  git checkout -q -B "$BRANCH" "$PREVIOUS"
  if build 9>&- && restart 9>&- && healthy 9>&-; then log "rolled back to ${PREVIOUS:0:7}; the previous version is running"
  else log "ROLLBACK ALSO FAILED: the app may be down. Check the log above."; fi
  exit 1
}
trap rollback ERR

log "deploying $(git log -1 --format='%h %s' "$TARGET") (was ${PREVIOUS:0:7})"
git checkout -q -B "$BRANCH" "$TARGET"
# 9>&-: processes started by the build or restart (pm2's daemon, `nohup app &`) must not inherit
# the deploy lock, or they would hold it forever and block every later deploy.
build 9>&-
restart 9>&-
healthy 9>&- || false
trap - ERR
log "deployed ${TARGET:0:7} successfully"
