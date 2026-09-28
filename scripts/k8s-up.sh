#!/usr/bin/env bash
# Stand up the local Kubernetes stack on a kind cluster (DESIGN.md §9).
# Mirrors the compose stack as a Helm release: postgres,
# redis, a migrate hook (Alembic + seed), api, and Caddy web.
#
# Idempotent: reuses the cluster if it exists, reloads images, upgrades the release.
# Reach the app with:  kubectl port-forward svc/web 8080:80   → http://localhost:8080
#
# Pass --keda to also install KEDA and enable worker autoscaling on queue depth.
set -euo pipefail
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"
# Resolve PROBLEMS_DIR against the caller's directory before the cd below changes it.
if [ -n "${PROBLEMS_DIR:-}" ]; then
  PROBLEMS_DIR=$(cd "$PROBLEMS_DIR" 2>/dev/null && pwd) || { echo "PROBLEMS_DIR is not a directory" >&2; exit 1; }
fi
cd "$(dirname "$0")/.."
PROBLEMS_DIR=${PROBLEMS_DIR:-$PWD/seed/problems}
ls "$PROBLEMS_DIR"/*.json >/dev/null 2>&1 || { echo "no *.json problem files in $PROBLEMS_DIR" >&2; exit 1; }

CLUSTER=shikomi
REL=shikomi
KEDA=false
[ "${1:-}" = "--keda" ] && KEDA=true

if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon not reachable. Start it first (e.g. 'colima start')." >&2
  exit 1
fi

# 1. Cluster
if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "▶ creating kind cluster '$CLUSTER'"
  kind create cluster --name "$CLUSTER" --wait 60s
fi
kubectl config use-context "kind-$CLUSTER" >/dev/null

# Colima (with network.address:false) doesn't always forward kind's published
# API-server port to the host, so kubectl can't reach the cluster. If so, open a
# one-off SSH tunnel into the Colima VM for that port. No-op on setups that
# already forward it (e.g. Docker Desktop, or `colima restart` with auto-forward).
if ! kubectl get --raw='/readyz' >/dev/null 2>&1; then
  SSHCFG="$HOME/.colima/_lima/colima/ssh.config"
  APIPORT=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' | sed 's#.*:##')
  if [ -f "$SSHCFG" ] && [ -n "$APIPORT" ]; then
    echo "▶ kubectl can't reach the API server; opening an SSH tunnel to the Colima VM (:$APIPORT)"
    ssh -F "$SSHCFG" -fN -L "${APIPORT}:127.0.0.1:${APIPORT}" lima-colima 2>/dev/null || true
    sleep 2
  fi
  kubectl get --raw='/readyz' >/dev/null 2>&1 || {
    echo "Still can't reach the cluster API. Try 'colima restart' (re-enables port forwarding)." >&2
    exit 1; }
fi

# 2. Images — build if missing, then load into the kind node (no registry needed)
echo "▶ building images if missing"
docker image inspect shikomi-api:local     >/dev/null 2>&1 || docker build --target base   -t shikomi-api:local    backend/
docker image inspect shikomi-worker:local  >/dev/null 2>&1 || docker build --target worker -t shikomi-worker:local backend/
docker image inspect shikomi-web:local     >/dev/null 2>&1 || docker build                 -t shikomi-web:local    frontend/
docker image inspect shikomi-judge:latest  >/dev/null 2>&1 || docker build                 -t shikomi-judge:latest judge/
docker image inspect shikomi-judge-js:latest >/dev/null 2>&1 || docker build -f judge/Dockerfile.js -t shikomi-judge-js:latest judge/
docker image inspect shikomi-judge-rust:latest >/dev/null 2>&1 || docker build -f judge/Dockerfile.rust -t shikomi-judge-rust:latest judge/
docker image inspect shikomi-judge-sql:latest >/dev/null 2>&1 || docker build -f judge/Dockerfile.sql-mysql -t shikomi-judge-sql:latest judge/
echo "▶ loading images into kind"
# judge:latest, judge-js:latest, judge-rust:latest, and judge-sql:latest too — the worker
# launches whichever a problem's `language` selects as a per-submission Pod
# (pull policy Never), so all four must be present on the node (DESIGN.md §13).
for img in shikomi-api:local shikomi-worker:local shikomi-web:local shikomi-judge:latest shikomi-judge-js:latest shikomi-judge-rust:latest shikomi-judge-sql:latest; do
  kind load docker-image --name "$CLUSTER" "$img"
done

# 3. Seed data → an image (the JSON lives at the repo root, outside the app images).
#    A tiny image holding PROBLEMS_DIR/*.json at /problems, which the migrate hook's
#    init container copies out before seeding. It replaced a ConfigMap, which caps at
#    1MiB: problem JSON with big hidden test cases outgrew even a gzipped tarball of it.
#    The tag is the chart's default, shikomi-seed:local, so a bare `helm upgrade` finds
#    it too; rebuilding under the same tag also keeps old images from piling up. A hash
#    of the problem set and of the recipe below is stored as a label, and a rebuild is
#    skipped when it matches.
echo "▶ building the problems image from $PROBLEMS_DIR"
SEED_IMAGE=shikomi-seed:local
SEED_DOCKERFILE='FROM busybox:1.36
COPY *.json /problems/
'
# Per-file checksums (name + digest, one line each, so where one file ends is never
# ambiguous) plus the Dockerfile: a renamed file, an edited one or a changed recipe
# all change the hash. sha256sum on Linux, shasum on macOS.
SHA256=$(command -v sha256sum || echo "shasum -a 256")
SEED_HASH=$( { (cd "$PROBLEMS_DIR" && $SHA256 -- *.json); printf '%s' "$SEED_DOCKERFILE"; } | $SHA256 | cut -c1-16)
if [ "$(docker image inspect -f '{{ index .Config.Labels "shikomi.seed-hash" }}' "$SEED_IMAGE" 2>/dev/null)" != "$SEED_HASH" ]; then
  # Build from a temporary context holding only the JSON: the problem directory may be a
  # whole repo (a .git, fixtures) that needn't be sent to the daemon, and its own
  # .dockerignore can't then drop a file the hash counted.
  SEED_CTX=$(mktemp -d)
  trap 'rm -rf "$SEED_CTX"' EXIT
  cp -- "$PROBLEMS_DIR"/*.json "$SEED_CTX"/
  printf '%s' "$SEED_DOCKERFILE" \
    | docker build -q --label "shikomi.seed-hash=$SEED_HASH" -t "$SEED_IMAGE" -f - "$SEED_CTX" >/dev/null
  # The previous build lost its tag to this one; drop it (only seed images carry the label).
  docker image prune -f --filter "label=shikomi.seed-hash" >/dev/null
fi
# Runs every time, but kind skips an image the node already has (same ID).
kind load docker-image --name "$CLUSTER" "$SEED_IMAGE"
# The ConfigMap this script published before the seed image (added 2026-09-28); nothing
# reads it now. Safe to delete this line once no cluster predates that.
kubectl delete configmap shikomi-seed --ignore-not-found >/dev/null

# 4. Optional: KEDA (event-driven autoscaling for the worker)
HELM_ARGS=()
if [ "$KEDA" = true ]; then
  if ! kubectl get ns keda >/dev/null 2>&1; then
    echo "▶ installing KEDA"
    helm repo add kedacore https://kedacore.github.io/charts >/dev/null 2>&1 || true
    helm repo update kedacore >/dev/null
    helm install keda kedacore/keda --namespace keda --create-namespace --wait --timeout 5m
  fi
  HELM_ARGS+=(--set keda.enabled=true)
fi

# 5. Release
. scripts/_local_secrets.sh   # the api refuses the dev secrets under ENV=prod; stable across runs
HELM_ARGS+=(--set "secrets.jwtSecret=$JWT_SECRET" --set "secrets.totpEncryptionKey=$TOTP_ENCRYPTION_KEY")
echo "▶ helm upgrade --install"
# ${HELM_ARGS[@]+"${HELM_ARGS[@]}"} (not "${HELM_ARGS[@]}") — macOS ships bash 3.2,
# where `set -u` treats expanding an empty array as an unbound variable. This
# form is the portable workaround: expands to nothing when empty, to the
# properly-quoted elements otherwise.
helm upgrade --install "$REL" deploy/helm/shikomi ${HELM_ARGS[@]+"${HELM_ARGS[@]}"} --wait --timeout 5m

echo
echo "k8s stack up. Reach it with:"
echo "  kubectl port-forward svc/web 8080:80   # → http://localhost:8080"
echo "  kubectl get pods                       # status"
echo "  helm uninstall $REL                    # tear down the release"
echo "  kind delete cluster --name $CLUSTER    # remove the whole cluster"
