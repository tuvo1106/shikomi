"""Verdict aggregation unit tests (DESIGN.md §10.2 — worker tests)."""
from worker.aggregate import aggregate, parse_harness_output
from worker.docker_runner import ContainerResult


def cr(**overrides):
    defaults = dict(stdout="", stderr="", exit_code=0, timed_out=False, stdout_truncated=False)
    defaults.update(overrides)
    return ContainerResult(**defaults)


def test_all_passed_is_accepted():
    results = [{"status": "passed", "runtime_ms": 5}, {"status": "passed", "runtime_ms": 8}]
    v = aggregate(cr(), results)
    assert v.status == "accepted"
    assert v.runtime_ms == 13  # sum across cases (5 + 8)
    assert (v.passed, v.total) == (2, 2)


def test_null_runtime_ms_does_not_crash_the_sum():
    """A result carrying `"runtime_ms": null` must not blow up the sum with a
    TypeError — not live today (the harness always emits a number), but aggregate
    shouldn't trust the shape of its input blindly."""
    results = [{"status": "passed", "runtime_ms": 5}, {"status": "passed", "runtime_ms": None}]
    v = aggregate(cr(), results)
    assert v.status == "accepted"
    assert v.runtime_ms == 5


def test_first_failure_determines_verdict():
    results = [{"status": "passed", "runtime_ms": 5}, {"status": "wrong_answer", "runtime_ms": 3}]
    assert aggregate(cr(), results).status == "wrong_answer"


def test_counts_reflect_all_cases_when_a_pass_follows_a_failure():
    # Run-all means a later pass is still counted even though the verdict is a failure.
    results = [{"status": "passed", "runtime_ms": 1},
               {"status": "wrong_answer", "runtime_ms": 2},
               {"status": "passed", "runtime_ms": 3}]
    v = aggregate(cr(), results)
    assert v.status == "wrong_answer"  # first failure decides the verdict
    assert (v.passed, v.total) == (2, 3)


def test_total_uses_case_count_when_harness_short_circuits():
    # A compile error yields one result row, but the denominator is the real case count.
    results = [{"status": "runtime_error", "runtime_ms": 0, "error": "SyntaxError"}]
    v = aggregate(cr(), results, total_cases=10)
    assert v.status == "runtime_error"
    assert (v.passed, v.total) == (0, 10)  # 0/10, not 0/1


def test_infra_failure_reports_full_denominator():
    assert aggregate(cr(exit_code=137), None, total_cases=10).total == 10


def test_runtime_error_mapping():
    assert aggregate(cr(), [{"status": "runtime_error", "runtime_ms": 0}]).status == "runtime_error"


def test_per_case_tle_mapping():
    assert aggregate(cr(), [{"status": "time_limit_exceeded", "runtime_ms": 2000}]).status == "time_limit_exceeded"


def test_oom_takes_precedence_over_results():
    v = aggregate(cr(exit_code=137), [{"status": "passed", "runtime_ms": 1}])
    assert v.status == "memory_limit_exceeded"


def test_timeout_takes_precedence_over_oom_exit_code():
    # A wall-clock kill (docker kill == SIGKILL) exits 137, identical to Docker's
    # own OOM-kill exit code — so once we've killed the container ourselves,
    # exit_code can no longer distinguish OOM from "we killed it for hanging".
    # Checking oom_killed before timed_out would misreport every wall-clock
    # kill as memory_limit_exceeded instead of time_limit_exceeded.
    v = aggregate(cr(exit_code=137, timed_out=True), [{"status": "passed", "runtime_ms": 1}])
    assert v.status == "time_limit_exceeded"


def test_output_truncation_is_output_limit_exceeded():
    assert aggregate(cr(stdout_truncated=True), []).status == "output_limit_exceeded"


def test_unparseable_output_is_judge_error():
    assert aggregate(cr(), None).status == "judge_error"


def test_unparseable_output_with_timeout_is_tle():
    assert aggregate(cr(timed_out=True), None).status == "time_limit_exceeded"


def test_unknown_case_status_is_judge_error():
    assert aggregate(cr(), [{"status": "bogus", "runtime_ms": 0}]).status == "judge_error"


def test_parse_harness_output_valid():
    assert parse_harness_output('{"results": [{"status": "passed"}]}') == [{"status": "passed"}]


def test_parse_harness_output_invalid():
    assert parse_harness_output("garbage") is None
    assert parse_harness_output('{"no_results": 1}') is None
    assert parse_harness_output('{"results": "not a list"}') is None
