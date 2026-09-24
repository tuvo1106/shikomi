"""Turn a sandbox run into a submission verdict (DESIGN.md §5.2 step 6, §5.3).

Pure function, no I/O — unit-tested directly and reused by the judge job.
"""
import dataclasses
import json
from typing import List, Optional

# Per-case harness status → submission status.
_CASE_STATUS = {
    "wrong_answer": "wrong_answer",
    "runtime_error": "runtime_error",
    "time_limit_exceeded": "time_limit_exceeded",
}


@dataclasses.dataclass
class Verdict:
    """The judged outcome: overall `status`, summed `runtime_ms`, and `passed/total`.

    `status` is the final submission status (accepted or a specific failure);
    `results` is the per-case list; `passed`/`total` drive the "X/N passed" display.
    """

    status: str
    runtime_ms: Optional[float]
    results: List[dict]
    passed: int = 0
    total: int = 0


def parse_harness_output(stdout: str):
    """Return the results list, or None if the harness output is unusable."""
    try:
        doc = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    results = doc.get("results") if isinstance(doc, dict) else None
    return results if isinstance(results, list) else None


def aggregate(container_result, results: Optional[List[dict]],
              total_cases: Optional[int] = None) -> Verdict:
    """Combine container-level signals and per-case results into one verdict.

    `total_cases` is the number of test cases the submission was judged against,
    used as the `total` denominator. When the harness short-circuits (a compile
    error yields one result, fail-fast stops early), the count still reads `X/N`
    against the real case count rather than the number of rows produced.

    Precedence: infra-level failures (wall-clock kill, OOM, output cap,
    unparseable output) are decided from container signals before per-case
    results are consulted — and among those, `timed_out` is checked **first**,
    before `oom_killed`. `run_in_container` kills a hung container with
    `docker kill` (SIGKILL), and SIGKILL's exit code (137) is *identical* to
    Docker's own OOM-kill exit code — so once `timed_out` is true, `exit_code`
    no longer tells us why the process died, only that we killed it. Checking
    `oom_killed` first would misreport every wall-clock kill (a genuine hang,
    or — before the `unordered`-mode fast path existed — merely a slow
    same-process comparison) as `memory_limit_exceeded` instead of the correct
    `time_limit_exceeded`.
    """
    result_list = results or []
    passed = sum(1 for r in result_list if r.get("status") == "passed")
    total = total_cases if total_cases is not None else len(result_list)

    if container_result.timed_out:
        # See the precedence note above: once we've killed the container
        # ourselves, its exit code can't be trusted to distinguish OOM from
        # any other reason a killed process exits 137.
        return Verdict("time_limit_exceeded", None, [], passed=0, total=total)
    if container_result.oom_killed:
        return Verdict("memory_limit_exceeded", None, [], passed=0, total=total)
    if container_result.stdout_truncated:
        return Verdict("output_limit_exceeded", None, result_list, passed=passed, total=total)
    if results is None:
        # No usable output, and (per the timed_out check above) not a
        # wall-clock kill either — a genuine judge-side fault.
        return Verdict("judge_error", None, [], passed=0, total=total)

    # Total execution time across all cases (the sum, so large cases dominate), rounded to shed
    # float-summation noise. `or 0` (not just `.get(..., 0)`) so a result that
    # carries an explicit `"runtime_ms": null` doesn't crash the sum with a
    # TypeError — the harness never emits that today, but this is the boundary
    # between our code and its output, so it shouldn't trust the shape blindly.
    runtime_ms = round(sum(r.get("runtime_ms") or 0 for r in result_list), 3) if result_list else None
    # All cases run (unless the problem opted into fail-fast); the overall status
    # is the first non-passing case's status, or accepted if every case passed.
    status = "accepted"
    for r in result_list:
        if r.get("status") != "passed":
            status = _CASE_STATUS.get(r.get("status"), "judge_error")
            break
    return Verdict(status, runtime_ms, result_list, passed=passed, total=total)
