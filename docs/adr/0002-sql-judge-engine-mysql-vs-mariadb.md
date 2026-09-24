# ADR-0002: SQL judge engine — MariaDB, MySQL, or SQLite

- **Status:** Accepted — Phase 0 spike run 2026-09-01, all kill-criteria cleared (see Decision)
- **Date:** 2026-09-01

## Context

Shikomi is adding SQL problem judging (a new `kind: "sql"` judge mode, parallel to today's Python/JS function-call judging). The immediate unlock was a backlog of 20 SQL-only problems the function-call judge couldn't take; the longer-term prize is SQL as a category of problem in its own right.

Every judge submission runs inside a one-shot, heavily locked-down container (`backend/worker/docker_runner.py`, `build_run_args`, lines 36-73 — **verified by direct read**):

```
--rm -i --network=none
--memory=<problem's memory_limit_mb> --memory-swap=<same>   # swap disabled
--cpus=1
--pids-limit=64
--read-only --tmpfs /tmp:size=16m
--security-opt=no-new-privileges --cap-drop=ALL
--user 1000:1000
```

This is comfortable today because both existing images (`judge/Dockerfile` = bare `python:3.12-slim`, `judge/Dockerfile.js` = bare `node:20-slim`) run a stateless interpreter with negligible startup cost and footprint.

A **client-server** SQL engine is a different proposition: the container must boot a database server, initialize a data directory, accept connections, seed, serve a query, and tear down — inside that same capped, read-only, network-less container. An **embedded** engine (SQLite) is not: it's a library call in the harness process, with no server, no data directory to initialize, and no new image.

That distinction — server vs. embedded — matters more to cost and risk than the MySQL-vs-MariaDB dialect question that originally prompted this ADR, so all three options are treated as first-class below.

### Evidence — verified in-session

- **Dialect fit does not force MySQL.** All 20 skipped SQL directories were grepped for MySQL-family functions (`TO_DAYS|DATEDIFF|IFNULL|GROUP_CONCAT|STR_TO_DATE|DATE_FORMAT`). **Exactly 1 of 20** (`0197_rising_temperature`) matches, using `TO_DAYS()` and `DATEDIFF()`. The other 19 are plain `SELECT`/`JOIN`/`GROUP BY`/`LIMIT`/subquery SQL that runs unchanged on any of the three engines. MariaDB implements both matched functions identically to MySQL.
- **Source shape.** Most directories ship `schema.sql` (DDL + seed `INSERT`s) and `solution.sql` (reference query); one (`1179_reformat_department_table`) bundles both into a single file. Engine-independent.
- **`sqlite3` is stdlib.** `import sqlite3` works out of the box on Python 3.12 (checked locally: SQLite 3.51.0). Modern enough for window functions (3.25+) and `RIGHT`/`FULL OUTER JOIN` (3.39+).

### Evidence — from knowledge, **confirm before relying on it**

The footprint claims below drive the recommendation but were *not* measured in-session (Docker/colima was not running). Each is a ~10-minute check:

- **MySQL 8** carries a mandatory InnoDB data-dictionary tablespace (`mysql.ibd`), separate undo tablespaces, and a default redo capacity around 100MB. A fresh data directory is expected to land near 100MB+, with a dictionary floor (~24MB) that no startup flag tunes away.
- **MariaDB** keeps its system tables in Aria/MyISAM (small files) and uses a single, freely tunable InnoDB redo log file, so its tuned floor is expected to be substantially smaller.

## Options

| | **A: MariaDB** | **B: MySQL 8** | **C: SQLite (embedded)** |
|---|---|---|---|
| New Docker image | Yes (`Dockerfile.sql-mysql`) | Yes | **No** — reuses existing `python:3.12-slim` judge image |
| Deploy wiring (compose, prod-up, k8s-up, Helm values + ConfigMap, 3 CI spots) | Yes | Yes | **None** |
| Phase 0 feasibility spike | Required | Required | **Not needed** |
| Fits `--tmpfs 16m` as-is | Tight, plausibly tunable | Very unlikely (see floor above) | Trivially |
| `--pids-limit=64` risk | Real (multi-threaded server; cgroups count threads) | Real | **None** (no server process) |
| Cold start per submission | Server boot (mitigable — see below) | Server boot, slower | **Zero** |
| Dialect fidelity to MySQL-family SQL (the dialect the backlog was written in) | High (1/20 known gap: none) | Highest | Moderate — no `ENUM`, dynamic typing, different date functions (no `TO_DAYS`/`DATEDIFF`; `julianday()`/`date()` instead) |
| Unlocks the 20 backlog problems | 20/20 | 20/20 | 19/20 as-written (the `TO_DAYS` one needs a rewrite) |

### The technique that changes the cold-start math (applies to A and B)

Do **not** run `mariadb-install-db` / `mysqld --initialize` on the per-submission path. Bake an already-initialized data directory into the image at build time, and at run time copy it into the tmpfs (`cp -a /opt/judge/datadir-template /tmp/mysql`) before starting the server. Initialization — the expensive part — moves to image build; per-submission cost drops to a tmpfs copy plus server start.

This is the standard approach for ephemeral database startup, and it materially changes what Phase 0 should measure. Measuring cold start *without* it would produce misleadingly bad numbers and could wrongly sink the whole server-engine approach.

## Decision

**Recommended: MariaDB (Option A), bounded by an explicit kill-criterion, with SQLite (Option C) as the pre-committed fallback.**

Rationale: this is interview-prep tooling, so dialect fidelity carries real pedagogical value, and SQL is a vertical that plausibly grows well past the current 20-problem unlock. Paying once for a real server engine is defensible **if** it fits the existing sandbox envelope cheaply. It is **not** defensible if it forces loosening a sandbox whose entire job is containing arbitrary user code execution.

MySQL 8 is dropped rather than spiked: its expected data-directory floor is structurally incompatible with the current tmpfs budget, and the dialect advantage it would buy over MariaDB is worth ~1 problem out of 20.

### Course of action

1. ~~Confirm MySQL 8's datadir floor~~ — **done.** `docker run --rm mysql:8 bash -c "mysqld --initialize-insecure --datadir=/tmp/d"` produces a **190MB** data directory (101MB `#innodb_redo`, 29MB `mysql.ibd` dictionary tablespace) before a single user table exists. Confirms the expectation above; MySQL 8 is dropped, not reopened.
2. **Run Phase 0 against MariaDB only**, using the pre-baked datadir technique, **time-boxed to one working session**. (Requires `colima start` — the daemon was not running when this was written.)
3. **Kill-criteria — if *any* fails, stop and switch to Option C:**
   - cold start > 1.5s p50 (with pre-baked datadir)
   - cannot fit a 64MB tmpfs
   - cannot fit 256MB memory
   - needs more than a modest bump above `--pids-limit=64`
4. **If it fails: ship SQLite v1** on the existing Python image, and revisit a server engine only once the SQL problem set is large enough that dialect divergence actually bites.

### Phase 0 results (2026-09-01) — MariaDB 10.11.18 (Debian 12 `mariadb-server`)

All four kill-criteria **cleared**, with wide margins:

| Kill-criterion | Threshold | Result |
|---|---|---|
| Cold start (post-pre-bake) | > 1.5s p50 fails | **~0.05–0.06s**, 5-run spread 0.052–0.064s (≈25–30x margin) |
| tmpfs fit | can't fit 64MB fails | Fits in **32MB** with room to spare (used 22MB fresh, 23MB after 5 sequential test-case cycles) |
| Memory fit | can't fit 256MB fails | **128MB sufficient** (didn't need to push to 192/256) |
| pids-limit=64 | needs more than a modest bump fails | Peaks at **15–16** pids (server is multi-threaded but nowhere near the ceiling) |

Also confirmed:
- **Unix-socket connectivity works under `--network=none`** exactly as expected (`--skip-networking` + socket in tmpfs; no TCP needed at all).
- **16MB tmpfs fails cleanly** — a predictable `cp: No space left on device` mid-copy, not a hang, corruption, or partial-boot state. Safe to treat as a hard "doesn't fit" rather than something needing more debugging.
- **The multi-test-case pattern holds**: booted the server once, then ran 5 `CREATE DATABASE case_N → seed → query → DROP DATABASE case_N` cycles (Phase 1's actual per-submission design) inside one container run. Datadir grew from 22MB to 23MB total — `DROP DATABASE` reclaims/reuses space rather than leaking it — and pids stayed flat. No evidence of accumulation across test cases.
- **`ENUM` columns work fine** — the spike's fixture schema has one, and the query result came back correct.

**What made this work — three tuning fixes beyond the plan's original sketch, recorded here since they're not obvious and the numbers above depend on them:**
1. **Pre-baked datadir must pass matching flags at *both* install time and server-start time.** `mariadb-install-db --innodb-log-file-size=4M` alone wasn't enough — starting the server without repeating that flag made MariaDB detect a mismatch against its own *default* (96MB) and silently re-create the redo log at 96MB on every boot, ballooning the datadir from 22MB to 125MB. Every InnoDB sizing flag baked into the template must be repeated verbatim at runtime.
2. **`innodb_autoextend_increment` defaults to 64MB per extend step.** Shrinking `ibdata1`/`ibtmp1` to `1M:autoextend` without also setting `--innodb-autoextend-increment=1` caused the very first extend to jump straight to 65MB each (1MB + one 64MB step) — ballooning the datadir to 139MB. Small initial file sizes are meaningless without a small increment to match.
3. With both fixed, the baked template is **~21MB**, dominated by `ibdata1` (10MB, InnoDB system tablespace floor) and the stock `mysql.help_topic` table (2.3MB, built-in help data no problem needs) — a further trim to comfortably clear the *existing* 16MB tmpfs (avoiding a SQL-specific override) is possible by skipping/truncating help-table population at install time, but wasn't needed to clear this ADR's kill-criteria and is left as a Phase 1b nice-to-have, not required.

**Recommended defaults for the SQL judge image, carried into Phase 2's config wiring:** `--tmpfs /tmp:size=32m` (not the global 16MB — this is the one flag that's genuinely hardcoded today and needs a per-image parameter per the plan), `--pids-limit=64` unchanged (no override needed), `memory_limit_mb` starting around 192MB for SQL problems (128MB covers the bare server; this leaves headroom for actual problem data/indexes/joins beyond the toy 12-row fixture used here — revisit with a larger seeded dataset in Phase 1 if a real problem needs more).

This inverts the risk in the current plan: Phase 0 stops being an open-ended feasibility question that could sink the effort, and becomes a bounded bet with a known-good fallback already in hand.

### Cost framing this decision sits inside

The full server-engine build is ~6 phases touching ~20 files (harness, image, data model, migration, deploy wiring in 6 places, authoring skill, 4 frontend files, 2 test files, CI in 3 spots) to unlock 20 problems — under 4% of the problem set at the time, 19 of them dialect-neutral basic joins. Option C delivers most of that unlock for roughly half the plan. The case for A over C is strategic (the vertical, and dialect fidelity), not immediate.

## Alternatives considered

| Option | Why not |
|---|---|
| MySQL 8 | Expected data-directory floor (~100MB, with a non-tunable ~24MB dictionary component) is structurally incompatible with the sandbox's tmpfs budget; buys dialect coverage worth ~1 of 20 known problems over MariaDB. Reopen only if step 1 above contradicts the floor estimate. |
| SQLite as the *primary* choice | Cheapest by a wide margin and genuinely tempting, but embedded-engine dialect divergence (no `ENUM`, dynamic typing, different date functions) teaches SQL that doesn't transfer to the MySQL/Postgres dialects interviews actually test — a real cost for a learning product. Held as the fallback rather than the default. |
| Postgres | Deferred by an earlier decision to a fast-follow after the MySQL-family path ships; it needs its own footprint spike (its `initdb` profile doesn't inherit MariaDB's numbers) and a driver swap, but reuses the harness protocol, data model, and deploy-wiring patterns wholesale. |

## Consequences

The choice is well-isolated: it affects only the judge image's packages, the server-startup incantation, and tuning flags — not the harness protocol, data model, authoring workflow, or frontend, all of which are engine-agnostic. Switching later is a contained change to one Dockerfile plus one startup script.

The one asymmetry worth noting: choosing **C** and later moving to **A/B** means re-authoring any SQLite-dialect problems' seed scripts and expected rows, so if SQLite ships as v1, its problems should stay on dialect-neutral SQL wherever practical to keep that migration cheap.
