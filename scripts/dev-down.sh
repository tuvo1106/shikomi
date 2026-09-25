#!/usr/bin/env bash
# Stop the dev app servers. Containers are left running so data is preserved.
set -euo pipefail
pkill -f "uvicorn app.main:app" 2>/dev/null || true
pkill -f "arq worker.main"     2>/dev/null || true
pkill -f "vite"                2>/dev/null || true
echo "stopped API, workers, web."
echo "containers (shikomi-pg, shikomi-redis) left running — 'docker stop shikomi-pg shikomi-redis' to stop them too."
