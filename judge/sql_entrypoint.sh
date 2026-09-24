#!/bin/bash
# ENTRYPOINT for the SQL judge sandbox image (judge/Dockerfile.sql-mysql).
# Boots the pre-baked template into tmpfs (ADR-0002's technique — the
# expensive mariadb-install-db step already happened at image build time, so
# this is a `cp -a` plus a server start, ~50ms per docs/adr/0002-...), waits
# for it to accept connections, then hands off to harness_sql.py, which
# speaks the same stdin-JSON-in/stdout-JSON-out protocol as harness.py/
# harness.js (DESIGN.md §5.3).
#
# The `trap` below only protects the pre-`exec` failure path (mariadbd never
# becomes ready, `exit 2`) — `exec` replaces this script's own process image,
# discarding bash's trap table, so it does *not* fire on the normal
# hand-off-to-the-harness path. That's fine: Docker tears down the whole
# container (and every process in its cgroup, mariadbd included) the moment
# PID 1 exits, whichever process PID 1 currently is — the trap is a
# belt-and-suspenders for the one path where bash itself is still PID 1 when
# it exits, not a general "always kill the server" guarantee.
set -euo pipefail

RUNDIR=/tmp/mysql-run
DATADIR=/tmp/mysql
mkdir -p "$RUNDIR"

# set -e means a failed/partial copy (e.g. the tmpfs is too small — ADR-0002
# saw this fail as a mid-copy "No space left on device") stops the script
# here with a clear error, instead of falling through to start mariadbd
# against a corrupt datadir and only surfacing the problem via a confusing
# readiness-timeout later.
cp -a /opt/judge/datadir-template "$DATADIR"

# shellcheck source=sql_innodb_layout_flags.sh
source /opt/judge/sql_innodb_layout_flags.sh

# LOAD_FILE()/INTO OUTFILE protection: the `judge` user's grant
# (sql_provision_template.sh) is scoped to `case\_%.*`, which structurally
# excludes the global-only FILE privilege those need — confirmed empirically
# (LOAD_FILE('/etc/passwd') returns NULL, not file contents). A
# `--secure-file-priv=NULL` belt-and-suspenders layer was tried and dropped:
# this MariaDB build only accepts that special "fully disabled" value via
# my.cnf, not as a literal CLI argument (`Failed to normalize the argument`
# at startup) — not worth the fragility for a redundant second layer on top
# of an already-proven mitigation.
mariadbd \
  --datadir="$DATADIR" \
  --socket="$RUNDIR/mysqld.sock" \
  --pid-file="$RUNDIR/mysqld.pid" \
  --log-error="$RUNDIR/error.log" \
  --skip-networking \
  --innodb-buffer-pool-size=32M \
  "${INNODB_LAYOUT_FLAGS[@]}" \
  --performance-schema=OFF \
  &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null' EXIT

READY=0
for i in $(seq 1 200); do
  if mariadb-admin --socket="$RUNDIR/mysqld.sock" -u judge ping >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 0.02
done

if [ "$READY" -ne 1 ]; then
  echo "harness: mariadbd did not become ready" >&2
  cat "$RUNDIR/error.log" >&2 2>/dev/null
  exit 2
fi

exec python3 /opt/judge/harness_sql.py
