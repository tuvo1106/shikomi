#!/usr/bin/env bash
# Bring up the production-shaped compose stack locally (DESIGN.md §9).
#
# Unlike dev-up.sh (hot-reload processes on the host), this runs the whole system
# as containers behind Caddy, exactly as it would on a VPS. Use it to verify the
# prod shape — built frontend, ENV=prod (Secure cookies, no CORS), one origin.
set -euo pipefail
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
cd "$(dirname "$0")/.."

if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon not reachable. Start it first (e.g. 'colima start')." >&2
  exit 1
fi

# The worker launches judge sandboxes via `docker run` on the host daemon, so the
# judge images must live on the host (not inside a compose service). Build them
# here; the worker (host socket mounted) then finds them by tag. Must match
# JUDGE_IMAGE / JUDGE_IMAGE_JS / JUDGE_IMAGE_SQL.
echo "▶ building judge images (shikomi-judge:latest, shikomi-judge-js:latest, shikomi-judge-sql:latest)"
docker build -t shikomi-judge:latest judge/
docker build -f judge/Dockerfile.js -t shikomi-judge-js:latest judge/
docker build -f judge/Dockerfile.sql-mysql -t shikomi-judge-sql:latest judge/

. scripts/_local_secrets.sh   # ENV=prod needs real JWT/TOTP secrets; kept stable across runs

echo "▶ building + starting the stack"
docker compose up --build -d

echo
echo "prod-shaped stack up:"
echo "  app:   http://localhost:${WEB_PORT:-8080}   (use 'localhost' so Secure cookies work)"
echo "  logs:  docker compose logs -f"
echo "  stop:  docker compose down        (add -v to wipe the database)"
