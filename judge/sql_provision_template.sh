#!/bin/bash
# Build-time only (judge/Dockerfile.sql-mysql) — boots the freshly
# mariadb-install-db'd template once, creates the low-privilege `judge`
# account the harness connects as at runtime, then shuts down cleanly so the
# grant lands in the template baked into the image layer. Never runs at
# submission time (ADR-0002's pre-baked-datadir technique — see
# docs/adr/0002-sql-judge-engine-mysql-vs-mariadb.md).
#
# `judge`@`localhost` is scoped to `case\_%` databases only — the harness
# creates/drops exactly `case_<test_case_id>` per test case (harness_sql.py)
# — so it never holds a global grant, and specifically never FILE/SUPER/
# PROCESS/RELOAD, which a root-authenticated connection would have. This is
# the account both seeding (trusted problem author content) and the
# submitted query (untrusted) connect as; only the client-side
# CLIENT_MULTI_STATEMENTS capability flag (set only on the seed connection,
# never the query one — harness_sql.py) distinguishes the two.
set -euo pipefail

DATADIR="$1"
SOCK=/tmp/provision.sock

# shellcheck source=sql_innodb_layout_flags.sh
source /opt/judge/sql_innodb_layout_flags.sh

# --user=root: this script only ever runs as root during `docker build`
# (before the image's USER runner switch); mariadbd otherwise refuses to
# start as root as a safety default that doesn't apply to a throwaway
# build-time provisioning step.
mariadbd \
  --user=root \
  --datadir="$DATADIR" \
  --socket="$SOCK" \
  --pid-file=/tmp/provision.pid \
  --log-error=/tmp/provision.log \
  --skip-networking \
  --innodb-buffer-pool-size=32M \
  "${INNODB_LAYOUT_FLAGS[@]}" \
  --performance-schema=OFF \
  &
SERVER_PID=$!

for i in $(seq 1 200); do
  mariadb-admin --socket="$SOCK" -u root ping >/dev/null 2>&1 && break
  sleep 0.02
done

mariadb --socket="$SOCK" -u root <<'SQL'
CREATE USER 'judge'@'localhost';
GRANT ALL PRIVILEGES ON `case\_%`.* TO 'judge'@'localhost';
FLUSH PRIVILEGES;
SQL

mariadb-admin --socket="$SOCK" -u root shutdown
wait "$SERVER_PID"
