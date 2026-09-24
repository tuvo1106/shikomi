#!/usr/bin/env bash
# Bring up the local dev environment. Safe to run repeatedly:
#   - Containers (shikomi-pg, shikomi-redis) are reused if running, started if
#     stopped, created if missing — NEVER removed, so data is preserved.
#   - App servers (API, worker, web) are killed and restarted cleanly.
set -euo pipefail
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
cd "$(dirname "$0")/.."

log() { echo "▶ $*"; }

if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon not reachable. Start it first (e.g. 'colima start')." >&2
  exit 1
fi

ensure_container() {
  local name=$1; shift
  if [ -n "$(docker ps -q -f name="^${name}$")" ]; then
    log "container ${name} already running"
  elif [ -n "$(docker ps -aq -f name="^${name}$")" ]; then
    log "starting existing container ${name}"; docker start "$name" >/dev/null
  else
    log "creating container ${name}"; docker run -d --name "$name" "$@" >/dev/null
  fi
}

ensure_container shikomi-pg \
  -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app -e POSTGRES_DB=shikomi \
  -p 5432:5432 postgres:16
ensure_container shikomi-redis -p 6379:6379 redis:7

log "waiting for postgres..."
until docker exec shikomi-pg pg_isready -U app >/dev/null 2>&1; do sleep 1; done

# Test database (used by the pytest suite).
docker exec shikomi-pg psql -U app -d shikomi -tc \
  "SELECT 1 FROM pg_database WHERE datname='shikomi_test'" | grep -q 1 \
  || docker exec shikomi-pg psql -U app -d shikomi -c "CREATE DATABASE shikomi_test;" >/dev/null

log "migrating + seeding (idempotent)"
( cd backend
  uv run alembic upgrade head >/dev/null
  uv run python -m app.cli seed >/dev/null
  uv run python -m app.cli seed-dev-user )

LOGDIR=/tmp/shikomi-dev; mkdir -p "$LOGDIR"
log "restarting app servers"
pkill -f "uvicorn app.main:app" 2>/dev/null || true
pkill -f "arq worker.main"     2>/dev/null || true  # judge + accounts workers
pkill -f "vite"                2>/dev/null || true
sleep 1
# The e2e suite signs in/registers many times from one IP, which the production per-IP
# auth limit (10/min) would throttle; loosen it for the local dev API only.
( cd backend && AUTH_RATE_LIMIT_PER_MINUTE=1000 nohup uv run uvicorn app.main:app --port 8000 --reload >"$LOGDIR/api.log" 2>&1 & )
( cd backend && nohup uv run arq worker.main.WorkerSettings   >"$LOGDIR/worker.log" 2>&1 & )
# Account email (console backend prints links here) + signup purge, on their own queue.
( cd backend && nohup uv run arq worker.main.AccountsWorkerSettings >"$LOGDIR/accounts-worker.log" 2>&1 & )
( cd frontend && nohup pnpm dev                               >"$LOGDIR/web.log" 2>&1 & )

echo
echo "dev environment up (logs in $LOGDIR):"
echo "  web:       http://localhost:5173"
echo "  api docs:  http://localhost:8000/docs"
echo "  dev login: dev@example.com / devpassword"
echo "  stop with: scripts/dev-down.sh"
