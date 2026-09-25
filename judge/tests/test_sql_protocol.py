"""SQL judge harness protocol tests (judge/harness_sql.py, DESIGN.md §5.3,
docs/adr/0002-sql-judge-engine-mysql-vs-mariadb.md).

Unlike test_protocol.py/test_js_protocol.py, this harness can't be exercised
as a bare `python3 harness_sql.py` subprocess — it needs a live mariadbd
already listening on a Unix socket, which only judge/sql_entrypoint.sh
provides (it boots the pre-baked template, then execs the harness). So these
tests run the real built image end to end, same as test_sandbox.py, and are
gated the same way:

    docker build -f judge/Dockerfile.sql-mysql -t shikomi-judge-sql:latest judge/
    pytest -m docker judge/tests/test_sql_protocol.py

tmpfs_size_mb=32 and memory_mb=192 match ADR-0002's Phase 0 spike results
(fits comfortably; 16MB fails cleanly, MariaDB needs ~22MB for its tuned
template) — not the python/js images' defaults.
"""
import json

import pytest

from sql_runner import run_sql_container

pytestmark = pytest.mark.docker

SEED = """
CREATE TABLE Deliveries (courier_id INT, parcel_id INT, event ENUM('pickup','dropoff'), minute FLOAT);
INSERT INTO Deliveries VALUES
(0,0,'pickup',2.0),(0,0,'dropoff',10.5),(0,1,'pickup',12.0),(0,1,'dropoff',15.0),
(1,0,'pickup',1.0),(1,0,'dropoff',4.25),(1,1,'pickup',5.0),(1,1,'dropoff',6.0);
"""
# Average pickup-to-dropoff time per courier. Every value is a dyadic fraction, so
# FLOAT stores it exactly and the expected averages carry no rounding noise.
CORRECT_QUERY = (
    "SELECT p.courier_id, ROUND(AVG(d.minute - p.minute), 3) "
    "FROM Deliveries p JOIN Deliveries d "
    "ON d.courier_id = p.courier_id AND d.parcel_id = p.parcel_id "
    "WHERE p.event='pickup' AND d.event='dropoff' GROUP BY p.courier_id"
)
EXPECTED = [[0, 5.75], [1, 2.125]]


def _payload(user_code, test_cases=None, time_limit_ms=2000, comparison=None):
    return json.dumps({
        "language": "mysql",
        "kind": "sql",
        "user_code": user_code,
        "test_cases": test_cases or [{"id": 0, "input": [SEED], "expected": EXPECTED}],
        "comparison": comparison or {"mode": "unordered"},
        "time_limit_ms": time_limit_ms,
    })


def _results(payload_json, **kw):
    proc = run_sql_container(payload_json, **kw)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["results"]


def test_correct_query_passes():
    results = _results(_payload(CORRECT_QUERY))
    assert results[0]["status"] == "passed"
    assert json.loads(results[0]["output"]) == EXPECTED


def test_wrong_answer():
    results = _results(_payload("SELECT courier_id, 0 FROM Deliveries GROUP BY courier_id"))
    assert results[0]["status"] == "wrong_answer"


def test_syntax_error_is_runtime_error():
    results = _results(_payload("SELCT * FROM Deliveries"))
    assert results[0]["status"] == "runtime_error"
    assert "syntax" in results[0]["error"].lower()


def test_partial_seed_failure_does_not_corrupt_later_cases():
    # A seed script whose CREATE DATABASE (issued by the harness itself,
    # before this runs) succeeds but whose own later statement fails must
    # still drop case_<id> (harness_sql.py's _run_case cleans up on the
    # seed-error path). This doesn't directly observe the leak (the container is --rm'd
    # before anything outside could inspect it), but it does prove the
    # harness keeps functioning correctly for later cases in the same
    # submission after a mid-seed failure, rather than getting stuck in a
    # corrupted state.
    bad_seed = "CREATE TABLE Deliveries (courier_id INT); INSERT INTO Deliveries VALUES (this is not valid sql);"
    cases = [
        {"id": 0, "input": [bad_seed], "expected": None},
        {"id": 1, "input": [SEED], "expected": EXPECTED},
    ]
    results = _results(_payload(CORRECT_QUERY, test_cases=cases))
    assert results[0]["status"] == "runtime_error"
    assert results[1]["status"] == "passed"


def test_stacked_query_is_rejected_not_executed():
    # A second statement after `;` must never run — the query connection has
    # no CLIENT_MULTI_STATEMENTS capability (harness_sql.py), so the server
    # itself rejects this as a syntax error near the second statement rather
    # than silently executing the DROP.
    results = _results(_payload("SELECT 1; DROP TABLE Deliveries"))
    assert results[0]["status"] == "runtime_error"


def test_time_limit_exceeded():
    results = _results(_payload("SELECT SLEEP(5)", time_limit_ms=500))
    assert results[0]["status"] == "time_limit_exceeded"
    assert results[0]["runtime_ms"] == 500


def test_load_file_is_blocked():
    # The `judge` DB user's grant (sql_provision_template.sh) is scoped to
    # `case\_%.*`, which structurally excludes the global-only FILE
    # privilege — LOAD_FILE returns NULL rather than file contents.
    results = _results(_payload("SELECT LOAD_FILE('/etc/passwd')",
                                 test_cases=[{"id": 0, "input": ["CREATE TABLE t (x INT)"],
                                              "expected": [[None]]}]))
    assert results[0]["status"] == "passed"
    assert json.loads(results[0]["output"]) == [[None]]


def test_multiple_cases_share_one_server_instance():
    # Proves the "boot once, loop per case" design (harness_sql.py's run()):
    # 3 independent case_<id> databases, one mariadbd instance, each case
    # judged in isolation.
    cases = [{"id": i, "input": [SEED], "expected": EXPECTED} for i in range(3)]
    results = _results(_payload(CORRECT_QUERY, test_cases=cases))
    assert len(results) == 3
    assert all(r["status"] == "passed" for r in results)
    assert [r["test_case_id"] for r in results] == [0, 1, 2]


def test_enum_column_round_trips():
    # The fixture schema uses ENUM — confirms the codec handles it, not just
    # plain INT/VARCHAR columns.
    results = _results(_payload("SELECT event FROM Deliveries WHERE courier_id=0 ORDER BY minute LIMIT 1",
                                 test_cases=[{"id": 0, "input": [SEED], "expected": [["pickup"]]}]))
    assert results[0]["status"] == "passed"


def test_unsupported_kind_is_single_runtime_error():
    payload = json.dumps({
        "language": "mysql", "kind": "function", "user_code": "SELECT 1",
        "test_cases": [{"id": 0, "input": [SEED], "expected": None}],
        "comparison": {"mode": "exact"}, "time_limit_ms": 2000,
    })
    results = _results(payload)
    assert len(results) == 1
    assert results[0]["status"] == "runtime_error"
