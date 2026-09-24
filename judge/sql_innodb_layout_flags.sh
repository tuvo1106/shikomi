# Shared, sourced (never copy-pasted) InnoDB *layout* flags for the SQL judge
# image. These must be byte-identical everywhere mariadbd or mariadb-install-db
# touches this datadir — judge/Dockerfile.sql-mysql's install step,
# sql_provision_template.sh, and sql_entrypoint.sh — or MariaDB detects a
# mismatch against the on-disk files and silently re-creates them at
# whatever size is *currently* configured. ADR-0002's "Phase 0 results"
# documents exactly this: the datadir ballooned from 22MB to 125MB the one
# time the log-file-size flag was passed at install time but not repeated at
# server start. `--innodb-buffer-pool-size` and `--performance-schema` are
# deliberately NOT here — they're pure runtime tuning that doesn't affect
# on-disk layout, so they don't need to match the install-time invocation.
INNODB_LAYOUT_FLAGS=(
  --innodb-log-file-size=4M
  --innodb-data-file-path=ibdata1:1M:autoextend
  --innodb-temp-data-file-path=ibtmp1:1M:autoextend
  --innodb-autoextend-increment=1
)
