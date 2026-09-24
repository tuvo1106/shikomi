#!/usr/bin/env python3
"""Judge harness (SQL/MariaDB) — runs a submitted SQL query against a seeded
schema inside the sandbox. Protocol mirrors judge/harness.py exactly
(DESIGN.md §5.3): stdin (or JUDGE_PAYLOAD_FILE) carries the payload JSON,
stdout carries exactly one result JSON document `{"results": [...]}` with the
same per-case shape (test_case_id/status/runtime_ms/output/stdout/error) —
the aggregator downstream (backend/worker/aggregate.py) doesn't know or care
which language judged a submission.

Unlike harness.py/harness.js, `user_code` is not executed as a program — it's
a single SQL query run against a database the harness itself seeds. There's
no function to call, so `kind` must be `"sql"`; anything else is rejected as
a single runtime_error the same way harness.js rejects a `kind` it doesn't
support.

Test-case shape (see judge/tests/test_sql_protocol.py for worked examples):
  input:    ["<DDL + seed DML, one string, may be multiple statements>"]
  expected: [[<row 0 cell 0>, <row 0 cell 1>, ...], [<row 1 ...>], ...]
`input` stays a JSON array (matching every other harness's convention — a
1-element array whose single element is the seed script) rather than a bare
string, so `TestCaseIn.input: list[Any]` needs no schema change for SQL.

Server lifecycle is NOT this file's job — judge/sql_entrypoint.sh boots
mariadbd from the pre-baked datadir template (ADR-0002's technique: the
expensive mariadb-install-db step already ran at image build time, so this
container's startup is just a tmpfs copy + server start, ~50ms) and only
execs this script once the server is confirmed ready. This file connects to
that already-running server once, then loops test cases against it — paying
the server's cold-start cost once per submission, not once per test case.

Security posture (ADR-0002's course of action, "built in from day one"):
both connections below authenticate as `judge`@`localhost`
(judge/sql_provision_template.sh), a low-privilege account scoped to
`case\_%` databases only — never FILE/SUPER/PROCESS/RELOAD, which a
root-authenticated connection would carry. The *seed* connection opens with
the CLIENT_MULTI_STATEMENTS capability (seed scripts are trusted problem-
author content, and legitimately need multiple DDL/DML statements per
script); the *query* connection deliberately does not — sending
semicolon-joined statements over a connection without that capability is
rejected by the server as a syntax error near the second statement, which
naturally becomes this test case's runtime_error. That's the harness's only
defense against a stacked-query submission, and it's sufficient: nothing
else about the protocol lets a submission's query see more than one
statement's worth of round trip.
"""
import datetime
import decimal
import json
import sys
import time
from collections import Counter

import pymysql
from pymysql.constants import CLIENT

TRUNC = 4096  # per-field cap for output/stdout/error, matching harness.py/js
SOCKET_PATH = "/tmp/mysql-run/mysqld.sock"
DB_USER = "judge"

# MariaDB's ER_STATEMENT_TIMEOUT — raised when a query exceeds the session's
# `max_statement_time` (set per test case from time_limit_ms, below). This is
# the SQL harness's equivalent of harness.py's per-case SIGALRM: there's no
# way to interrupt a query mid-execution from Python itself, so the timeout
# has to be enforced server-side and recognized by its error code here.
ER_STATEMENT_TIMEOUT = 1969


def _truncate(text):
    if text is None:
        return None
    if len(text) <= TRUNC:
        return text
    return text[:TRUNC] + "…(truncated)"


# --- comparison modes -------------------------------------------------------
# Duplicated from harness.py/harness.js rather than shared: the three
# harnesses each run in their own image with no filesystem in common at
# build or run time (established precedent — harness.js already carries its
# own copy of this exact logic for the same reason). Row-set comparison
# reuses the existing `comparison: {"mode": "unordered"}` config verbatim —
# no new comparison mode was needed for SQL (DESIGN.md §5.4's four modes
# already cover "row order doesn't matter").

def _is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _float_equal(a, b, eps):
    if _is_number(a) and _is_number(b):
        return abs(a - b) <= eps
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_float_equal(x, y, eps) for x, y in zip(a, b))
    return a == b


def _hashable_key(value):
    """Recursively convert `value` to something hashable, for `_multiset_equal`'s
    `Counter` fast path — a row (a list of cells) becomes a tuple of its own
    converted cells. See harness.py's copy of this same helper for the full
    rationale (this file is a deliberate, self-contained duplicate — see the
    "Duplicated from harness.py/harness.js" note above)."""
    if isinstance(value, list):
        return tuple(_hashable_key(v) for v in value)
    return value


def _multiset_equal(a, b):
    """Order-insensitive row-set equality. Fast path: every row's cells are
    hashable after `_normalize_cell` (str/int/float/bool/None), so `_hashable_
    key` turns each row into a hashable tuple and this is an O(n) `Counter`
    comparison rather than the O(n²) fallback loop — see harness.py's copy of
    this same fast path for why that matters (an untimed comparison still
    bounded by the worker's outer wall clock)."""
    if not isinstance(a, list) or not isinstance(b, list):
        return a == b
    if len(a) != len(b):
        return False
    try:
        return Counter(_hashable_key(x) for x in a) == Counter(_hashable_key(x) for x in b)
    except TypeError:
        pass
    remaining = list(b)
    for item in a:
        for i, candidate in enumerate(remaining):
            if candidate == item:
                del remaining[i]
                break
        else:
            return False
    return True


def compare(actual, expected, comparison):
    mode = (comparison or {}).get("mode", "exact")
    if mode == "unordered":
        return _multiset_equal(actual, expected)
    if mode == "float_tolerance":
        return _float_equal(actual, expected, (comparison or {}).get("epsilon", 1e-6))
    if mode == "any_of":
        candidates = expected if isinstance(expected, list) else [expected]
        return any(actual == candidate for candidate in candidates)
    return actual == expected


# --- row type normalization --------------------------------------------------
# The harness's own trusted code decoding its own trusted driver's typed
# values — not user input — so there's no injection concern here, only a
# JSON-representability one: PyMySQL hands back native Python types
# (Decimal/date/datetime/bytes) that json.dumps can't serialize as-is.

def _normalize_cell(value):
    if isinstance(value, decimal.Decimal):
        # String, not float: a float round-trip can perturb the value enough
        # to fail "exact" comparison on money-shaped numbers. A problem that
        # wants numeric slack opts into the existing "float_tolerance" mode
        # instead, unchanged by this choice.
        return str(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return repr(value)
    return value


def _normalize_row(row):
    return [_normalize_cell(v) for v in row]


# --- execution ---------------------------------------------------------------

def _case_error(test_case_id, message, elapsed_ms=0):
    return {
        "test_case_id": test_case_id,
        "status": "runtime_error",
        "runtime_ms": elapsed_ms,
        "output": None,
        "stdout": "",
        "error": _truncate(str(message)),
    }


def _run_case(seed_conn, query_conn, tc, user_code, comparison, time_limit_ms):
    """Seed a fresh `case_<id>` database, run the submitted query once against
    it, compare, then drop the database. One test case = one instantiate-and-
    query cycle, matching the per-test-case reveal unit `build_verdict_results`
    (backend/worker/judging.py) already expects — the server itself stays up
    across every case in this loop.
    """
    tc_id = tc.get("id", 0)
    seed_script = (tc.get("input") or [""])[0] or ""
    expected = tc.get("expected")
    db_name = "case_%d" % tc_id

    start = time.perf_counter()
    # Everything — seeding, the query, and building the result — runs inside
    # one try whose finally always attempts the DROP DATABASE, regardless of
    # which stage failed or succeeded. A seed-only try/except that returned
    # directly on failure (the original shape here) skipped cleanup on that
    # path entirely, since the cleanup lived in a separate try/finally that
    # only wrapped the *query* step — a seed script whose CREATE DATABASE
    # succeeds but a later statement fails would leak case_<id> for the rest
    # of the submission.
    try:
        try:
            with seed_conn.cursor() as cur:
                cur.execute("DROP DATABASE IF EXISTS `%s`" % db_name)
                cur.execute("CREATE DATABASE `%s`" % db_name)
                cur.execute("USE `%s`" % db_name)
                cur.execute(seed_script)
                while cur.nextset():  # drain every statement's result in the seed script
                    pass
        except pymysql.Error as exc:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 3)
            return _case_error(tc_id, "seed error: %s" % (exc,), elapsed_ms)

        try:
            with query_conn.cursor() as cur:
                cur.execute("USE `%s`" % db_name)
                # Per-case statement timeout — the SQL harness's analogue of
                # harness.py's SIGALRM (see ER_STATEMENT_TIMEOUT above).
                cur.execute("SET SESSION max_statement_time = %f" % (time_limit_ms / 1000.0))
                cur.execute(user_code)
                rows = [_normalize_row(row) for row in cur.fetchall()] if cur.description else []
        except pymysql.Error as exc:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 3)
            if exc.args and exc.args[0] == ER_STATEMENT_TIMEOUT:
                return {
                    "test_case_id": tc_id,
                    "status": "time_limit_exceeded",
                    "runtime_ms": time_limit_ms,
                    "output": None,
                    "stdout": "",
                    "error": None,
                }
            return _case_error(tc_id, exc, elapsed_ms)

        elapsed_ms = round((time.perf_counter() - start) * 1000, 3)
        passed = compare(rows, expected, comparison)
        return {
            "test_case_id": tc_id,
            "status": "passed" if passed else "wrong_answer",
            "runtime_ms": elapsed_ms,
            "output": _truncate(json.dumps(rows, separators=(",", ":"))),
            "stdout": "",
            "error": None,
        }
    finally:
        # Best-effort: the tmpfs vanishes with the container regardless, this
        # just keeps datadir growth flat across cases within one submission
        # (verified in the Phase 0 spike: 22MB -> 23MB across 5 cases, not
        # 22MB -> 110MB).
        try:
            with seed_conn.cursor() as cur:
                cur.execute("DROP DATABASE IF EXISTS `%s`" % db_name)
        except pymysql.Error:
            pass


def run(payload):
    """Connect once, then run every test case against the same server.

    Mirrors harness.py's `run()` shape (compile/connect once, loop cases) and
    harness.js's `kind` guard (an unsupported `kind` is a single runtime_error,
    not a crash) — `"sql"` is the only kind this harness ever receives, since
    ProblemIn's validator (backend/app/schemas/problem.py) enforces
    `kind=="sql" <=> language=="mysql"` at authoring time; the guard here is
    defense in depth, not the primary enforcement.
    """
    kind = payload.get("kind", "function")
    test_cases = payload.get("test_cases", [])
    first_id = test_cases[0].get("id", 0) if test_cases else 0

    if kind != "sql":
        return [_case_error(first_id, "kind '%s' is not supported by the SQL harness" % kind)]

    user_code = payload.get("user_code", "")
    comparison = payload.get("comparison", {"mode": "exact"})
    time_limit_ms = int(payload.get("time_limit_ms", 2000))
    stop_on_first_failure = bool(payload.get("stop_on_first_failure", False))

    try:
        seed_conn = pymysql.connect(
            unix_socket=SOCKET_PATH, user=DB_USER, autocommit=True,
            client_flag=CLIENT.MULTI_STATEMENTS,
        )
        query_conn = pymysql.connect(unix_socket=SOCKET_PATH, user=DB_USER, autocommit=True)
    except pymysql.Error as exc:  # server unreachable — a harness/infra failure, not the user's
        return [_case_error(first_id, "could not connect to database server: %s" % (exc,))]

    results = []
    try:
        for tc in test_cases:
            try:
                result = _run_case(seed_conn, query_conn, tc, user_code, comparison, time_limit_ms)
            except BaseException as exc:  # noqa: BLE001 - a bug in our own normalization/
                # compare code (e.g. json.dumps choking on a column type
                # _normalize_cell doesn't convert) must fail only this case,
                # not crash the process and turn the whole submission into
                # judge_error — matching harness.py's own per-case
                # BaseException guard around user-facing execution.
                result = _case_error(tc.get("id", 0), exc)
            results.append(result)
            if result["status"] != "passed" and stop_on_first_failure:
                break
    finally:
        seed_conn.close()
        query_conn.close()

    return results


def main():
    # Payload channel: stdin by default (the `docker run -i` path). Under
    # Kubernetes there's no stdin pipe, so the runner mounts the payload as a
    # file and points JUDGE_PAYLOAD_FILE at it — same duality as harness.py.
    import os
    payload_file = os.environ.get("JUDGE_PAYLOAD_FILE")
    raw = open(payload_file, encoding="utf-8").read() if payload_file else sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write("harness: invalid payload JSON: %s\n" % exc)
        sys.exit(2)
    results = run(payload)
    sys.stdout.write(json.dumps({"results": results}))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
