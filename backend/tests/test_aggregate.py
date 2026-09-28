"""Verdict aggregation unit tests (DESIGN.md §10.2 — worker tests)."""
import json

import pytest

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


def test_per_case_mle_mapping():
    # Only the Rust harness reports this per case (RLIMIT_AS per case process);
    # it must not fall through to judge_error as an unknown status.
    v = aggregate(cr(), [{"status": "passed", "runtime_ms": 1},
                         {"status": "memory_limit_exceeded", "runtime_ms": 30}])
    assert v.status == "memory_limit_exceeded"
    assert (v.passed, v.total) == (1, 2)


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


# --- the report must account for the cases sent (defence in depth) ---------------

def _doc(*rows):
    return json.dumps({"results": [{"test_case_id": i, "status": st} for i, st in rows]})


def test_a_complete_report_is_accepted():
    assert parse_harness_output(_doc((0, "passed"), (1, "wrong_answer")), [0, 1]) is not None


def test_an_early_stop_on_a_failure_is_accepted():
    """A compile error is one row; stop_on_first_failure ends at the first failure."""
    assert parse_harness_output(_doc((0, "runtime_error")), [0, 1, 2]) is not None
    assert parse_harness_output(_doc((0, "passed"), (1, "wrong_answer")), [0, 1, 2]) is not None


@pytest.mark.parametrize("rows", [
    (),                                   # nothing for a non-empty run
    ((0, "passed"),),                     # stops early without a failure
    ((0, "passed"), (2, "passed")),       # skips a case
    ((1, "passed"), (0, "passed")),       # out of order
    ((0, "passed"), (1, "passed"), (1, "passed")),  # more rows than cases
    ((7, "passed"), (8, "passed")),       # unknown ids
])
def test_a_report_that_doesnt_account_for_every_case_is_unusable(rows):
    assert parse_harness_output(_doc(*rows), [0, 1]) is None


def test_an_unusable_report_is_a_judge_error_not_a_pass():
    verdict = aggregate(cr(), parse_harness_output(_doc(), [0, 1]), total_cases=2)
    assert verdict.status == "judge_error"
