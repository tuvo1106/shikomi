#!/usr/bin/env bash
# Run CI's jobs on this machine, so a push doesn't spend GitHub Actions minutes finding
# what a local run would have (DESIGN.md §10.4, CONTRIBUTING.md). Each job below mirrors
# the job of the same name in .github/workflows/ci.yml; keep them in step with it.
#
#   scripts/ci-local.sh                  the fast jobs: lint harness backend frontend
#   scripts/ci-local.sh --changed        only the fast jobs your diff vs main touches
#                                        (what the pre-push hook runs)
#   scripts/ci-local.sh full             everything, incl. sandbox, e2e and playwright
#   scripts/ci-local.sh backend lint     just the named jobs
#
# Jobs: lint  harness  backend  frontend   (fast: no dev servers needed beyond Postgres/Redis)
#       sandbox  e2e  playwright           (slow: real judge containers / a full dev stack)
#
# The backend job needs the dev Postgres + Redis containers (`scripts/dev-up.sh` starts
# them). e2e and playwright start their own API/worker on a throwaway database (they stop a
# running dev stack to free its ports; run scripts/dev-up.sh afterwards).
set -euo pipefail
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
cd "$(dirname "$0")/.."

ALL_FAST="lint harness backend frontend"
DB_URL_BASE="postgresql+asyncpg://app:app@localhost:5432"

# Database names carry a short hash of this checkout's path. The backend suite drops and
# recreates its schema per test, so two worktrees (or two sessions) sharing one `shikomi_test`
# would deadlock on each other's locks or wipe each other's tables mid-run. One database per
# checkout means concurrent runs in different worktrees can't collide. (They still share the
# dev ports for e2e/playwright, so don't run those two at once from different checkouts.)
WT_ID=$(printf '%s' "$PWD" | shasum | cut -c1-8)
TEST_DB="shikomi_test_$WT_ID"
MIGCHECK_DB="shikomi_migcheck_$WT_ID"
CI_DB="shikomi_ci_$WT_ID"

log() { printf '\n\033[1;36m▶ ci-local: %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m✗ ci-local: %s\033[0m\n' "$*" >&2; exit 1; }

need_docker() {
  docker info >/dev/null 2>&1 || die "Docker daemon not reachable (e.g. 'colima start')."
}

need_infra() {
  need_docker
  for c in shikomi-pg shikomi-redis; do
    [ -n "$(docker ps -q -f name="^${c}$")" ] || die "container $c isn't running: run scripts/dev-up.sh first."
  done
  docker exec shikomi-pg psql -U app -d shikomi -tc \
    "SELECT 1 FROM pg_database WHERE datname='$TEST_DB'" | grep -q 1 \
    || docker exec shikomi-pg psql -U app -d shikomi -c "CREATE DATABASE $TEST_DB;" >/dev/null
}

# --- jobs (each mirrors the CI job of the same name) ---------------------------------

job_lint() {
  log "lint: ruff"
  (cd backend && uv run ruff check . ../judge)
}

job_harness() {
  log "harness: judge protocol tests"
  uv run --project backend pytest -m "not docker" --ignore=judge/tests/test_seed_solutions.py
  # Same rule as CI: the slow seed-solution validation only when seed/judge changed
  # (or when we can't tell).
  if [ "${CI_LOCAL_SEED:-auto}" = "yes" ] || seed_or_judge_changed; then
    log "harness: validating seed solutions (seed/problems or judge/ changed)"
    uv run --project backend pytest judge/tests/test_seed_solutions.py -v
  fi
}

job_backend() {
  need_infra
  log "backend: migrations apply cleanly on a fresh database"
  docker exec shikomi-pg psql -U app -d shikomi -c "DROP DATABASE IF EXISTS $MIGCHECK_DB;" >/dev/null
  docker exec shikomi-pg psql -U app -d shikomi -c "CREATE DATABASE $MIGCHECK_DB;" >/dev/null
  # Drop the scratch DB even if the upgrade fails.
  ( cd backend && DATABASE_URL="$DB_URL_BASE/$MIGCHECK_DB" uv run alembic upgrade head >/dev/null ) \
    && rc=0 || rc=$?
  docker exec shikomi-pg psql -U app -d shikomi -c "DROP DATABASE IF EXISTS $MIGCHECK_DB;" >/dev/null
  [ "$rc" -eq 0 ] || die "alembic upgrade head failed on a fresh database."
  log "backend: tests + coverage gate"
  (cd backend && TEST_DATABASE_URL="$DB_URL_BASE/$TEST_DB" uv run pytest)
}

job_frontend() {
  log "frontend: install, lint, test, build"
  (cd frontend && pnpm install --frozen-lockfile >/dev/null \
    && pnpm exec oxlint \
    && pnpm test \
    && pnpm build)
}

job_sandbox() {
  need_docker
  log "sandbox: build judge images, run the real-container tests"
  docker build -q -t shikomi-judge:latest judge/ >/dev/null
  docker build -q -f judge/Dockerfile.js -t shikomi-judge-js:latest judge/ >/dev/null
  docker build -q -f judge/Dockerfile.sql-mysql -t shikomi-judge-sql:latest judge/ >/dev/null
  uv run --project backend pytest -m docker
}

# The e2e and playwright jobs need a live API + worker. Like CI (a fresh, ephemeral
# Postgres per run) they get their own database and Redis DB, never your dev data, so a
# dev DB migrated by another branch can't break them and they can't dirty it. The servers
# are stopped again on exit. This stops a running dev stack first (same ports as scripts/dev-up.sh):
# run scripts/dev-up.sh afterwards to bring it back.
STACK_UP=""
stack_down() { scripts/dev-down.sh >/dev/null 2>&1 || true; }

ensure_stack() {
  [ -z "$STACK_UP" ] || return 0
  need_infra
  log "starting an isolated stack (database $CI_DB, redis db 1)"
  stack_down                      # free :8000 / :5173
  trap stack_down EXIT
  STACK_UP=1
  docker exec shikomi-pg psql -U app -d shikomi -c "DROP DATABASE IF EXISTS $CI_DB;" >/dev/null
  docker exec shikomi-pg psql -U app -d shikomi -c "CREATE DATABASE $CI_DB;" >/dev/null
  docker exec shikomi-redis redis-cli -n 1 flushdb >/dev/null
  # Exported so the servers, the CLI and the specs' own `app.cli` calls all agree. The
  # auth limit is loosened for the same reason as in ci.yml (specs sign in many times).
  export DATABASE_URL="$DB_URL_BASE/$CI_DB" REDIS_URL="redis://localhost:6379/1" \
         AUTH_RATE_LIMIT_PER_MINUTE=1000
  local logs=/tmp/shikomi-ci-local; mkdir -p "$logs"
  ( cd backend
    uv run alembic upgrade head >/dev/null
    uv run python -m app.cli seed >/dev/null
    uv run python -m app.cli seed-dev-user >/dev/null
    nohup uv run uvicorn app.main:app --port 8000 >"$logs/api.log" 2>&1 &
    nohup uv run arq worker.main.WorkerSettings >"$logs/worker.log" 2>&1 &
    # Present once account mail has its own queue; without it the job would just be absent.
    if grep -q "class AccountsWorkerSettings" worker/main.py; then
      nohup uv run arq worker.main.AccountsWorkerSettings >"$logs/accounts-worker.log" 2>&1 &
    fi )
  local i
  for i in $(seq 1 60); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/api/v1/healthz)" = "200" ] && return 0
    sleep 1
  done
  die "the API didn't come up; see $logs/api.log"
}

job_e2e() {
  ensure_stack
  log "e2e: full submit pipeline against a live API + worker"
  (cd backend && uv run python ../scripts/e2e_submit.py)
}

job_playwright() {
  ensure_stack
  log "playwright: browser E2E"
  (cd frontend && pnpm install --frozen-lockfile >/dev/null \
    && pnpm exec playwright install chromium >/dev/null \
    && pnpm exec playwright test)
}

# --- picking jobs from the diff (--changed) ------------------------------------------

# The branch point against main; falls back to origin/main, then main.
base_ref() {
  git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD main 2>/dev/null || true
}

# The changed-file list, computed once in the main shell (so the seed check inside a job
# sees it too). "__all__" when there's no base to diff against: run everything rather than
# silently skip a check.
CHANGED=""
load_changed() {
  local base; base=$(base_ref)
  if [ -z "$base" ]; then CHANGED="__all__"; else
    CHANGED=$( { git diff --name-only "$base" HEAD; git diff --name-only; \
      git ls-files --others --exclude-standard; } | sort -u )
  fi
}

# here-string, not `printf | grep -q`: under pipefail, grep exiting early can SIGPIPE the
# printf and turn a match into a failure.
touches() { [ "$CHANGED" = "__all__" ] || grep -qE "$1" <<<"$CHANGED"; }
seed_or_judge_changed() { touches '^(seed/problems/|judge/)'; }

pick_changed_jobs() {
  local jobs=""
  touches '\.py$|^backend/pyproject\.toml$|^backend/uv\.lock$' && jobs="$jobs lint"
  touches '^(judge/|seed/problems/|pyproject\.toml)' && jobs="$jobs harness"
  touches '^backend/|^seed/' && jobs="$jobs backend"
  touches '^frontend/' && jobs="$jobs frontend"
  echo "$jobs"
}

# --- main ----------------------------------------------------------------------------

mode="${1:-fast}"
load_changed
case "$mode" in
  --changed)
    jobs=$(pick_changed_jobs)
    if [ -z "${jobs// /}" ]; then
      echo "ci-local: no backend/frontend/judge changes vs main; nothing to run."
      exit 0
    fi
    ;;
  fast) jobs="$ALL_FAST" ;;
  full) jobs="$ALL_FAST sandbox e2e playwright" ;;
  -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
  *) jobs="$*" ;;
esac

start=$(date +%s)
for j in $jobs; do
  case "$j" in
    lint|harness|backend|frontend|sandbox|e2e|playwright) "job_$j" ;;
    *) die "unknown job '$j' (jobs: lint harness backend frontend sandbox e2e playwright)" ;;
  esac
done
printf '\n\033[1;32m✓ ci-local: passed (%s) in %ss\033[0m\n' "$(echo $jobs)" "$(( $(date +%s) - start ))"
if [ "$mode" = "--changed" ] || [ "$mode" = "fast" ]; then
  echo "  Not run locally (CI still runs them): sandbox, e2e, playwright — 'scripts/ci-local.sh full' runs everything."
fi
