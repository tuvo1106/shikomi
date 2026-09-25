"""Harness protocol tests for the JS harness (DESIGN.md §5.3, §13) — run
harness.js as a Node subprocess and assert its result JSON. No Docker
required; mirrors test_protocol.py's structure so the two harnesses are held
to the same behavioral contract.
"""
import json
import pathlib
import subprocess

import pytest

HARNESS = pathlib.Path(__file__).resolve().parents[1] / "harness.js"
TRUNC_MAX = 4096 + len("…(truncated)")


def payload(user_code, test_cases, function_name="f", comparison=None, time_limit_ms=2000,
            stop_on_first_failure=False):
    return {
        "function_name": function_name,
        "user_code": user_code,
        "test_cases": test_cases,
        "comparison": comparison or {"mode": "exact"},
        "time_limit_ms": time_limit_ms,
        "stop_on_first_failure": stop_on_first_failure,
    }


def case(i, inp, expected):
    return {"id": i, "input": inp, "expected": expected}


def run_harness(pl, raw=None, timeout=15):
    stdin = raw if raw is not None else json.dumps(pl)
    return subprocess.run(
        ["node", str(HARNESS)],
        input=stdin, capture_output=True, text=True, timeout=timeout,
    )


def results(pl, **kw):
    proc = run_harness(pl, **kw)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["results"]


PAIR_SUM = (
    "var pairSum = function(nums, target) {\n"
    "  var seen = {};\n"
    "  for (var i = 0; i < nums.length; i++) {\n"
    "    var need = target - nums[i];\n"
    "    if (need in seen) return [seen[need], i];\n"
    "    seen[nums[i]] = i;\n"
    "  }\n"
    "};"
)


def test_correct_solution_all_pass():
    res = results(payload(PAIR_SUM, [case(0, [[4, 9, 1, 6], 7], [2, 3]),
                                     case(1, [[5, 5], 10], [0, 1])],
                          function_name="pairSum"))
    assert [r["status"] for r in res] == ["passed", "passed"]
    assert all(r["error"] is None for r in res)


def test_const_and_arrow_function_declarations_are_found():
    # `sandbox[functionName]` (a plain object-property lookup) misses these:
    # only top-level `var`/`function` become properties of the contextified
    # global object. A `const`/arrow-function declaration — legal, idiomatic
    # modern JS, and exactly the style these problems are meant to allow — is
    # a lexical binding invisible to that lookup, even though it resolves and
    # calls fine. The existence check must resolve the same way the call does
    # (via `typeof` through `vm.runInContext`), not via property access.
    code = "const f = (x) => x + 1;"
    res = results(payload(code, [case(0, [1], 2)]))
    assert res[0]["status"] == "passed"

    code_let = "let f = function(x) { return x + 1; };"
    res = results(payload(code_let, [case(0, [1], 2)]))
    assert res[0]["status"] == "passed"


def test_wrong_answer():
    res = results(payload("function f(x) { return x + 1; }",
                          [case(0, [1], 3)]))
    assert res[0]["status"] == "wrong_answer"
    assert res[0]["output"] == "2"


def test_runtime_error_reports_thrown_message_not_harness_internals():
    res = results(payload("function f(x) { throw new Error('boom'); }", [case(0, [1], None)]))
    assert res[0]["status"] == "runtime_error"
    assert "boom" in res[0]["error"]
    assert "harness.js" not in res[0]["error"]
    assert "internal/" not in res[0]["error"]


def test_syntax_error_is_single_runtime_error():
    res = results(payload("function f(x) { return x ++ + ; }", [case(0, [1], None)]))
    assert len(res) == 1
    assert res[0]["status"] == "runtime_error"


def test_function_not_found():
    res = results(payload("function f(x) { return x; }", [case(0, [1], 1)], function_name="g"))
    assert res[0]["status"] == "runtime_error"
    assert "not found" in res[0]["error"]


def test_time_limit_exceeded_on_infinite_loop():
    res = results(payload("function f(x) { while (true) {} }", [case(0, [1], None)],
                          time_limit_ms=300))
    assert res[0]["status"] == "time_limit_exceeded"
    assert res[0]["runtime_ms"] == 300


def test_stdout_is_captured_and_separate_from_protocol():
    res = results(payload("function f(x) { console.log('hi', x); return x; }",
                          [case(0, [5], 5)]))
    assert res[0]["stdout"] == "hi 5"
    assert res[0]["status"] == "passed"


def test_stdout_is_truncated():
    res = results(payload("function f(x) { console.log('a'.repeat(10000)); return x; }",
                          [case(0, [0], 0)]))
    assert len(res[0]["stdout"]) == TRUNC_MAX
    assert res[0]["stdout"].endswith("…(truncated)")


def test_one_case_mutation_does_not_leak_into_next():
    res = results(payload(
        "function f(arr) { arr.push(999); return arr.length; }",
        [case(0, [[1, 2, 3]], 4), case(1, [[1, 2, 3]], 4)]))
    assert [r["status"] for r in res] == ["passed", "passed"]


def test_stop_on_first_failure_skips_remaining_cases():
    res = results(payload("function f(x) { return x; }",
                          [case(0, [1], 2), case(1, [1], 1)],
                          stop_on_first_failure=True))
    assert len(res) == 1
    assert res[0]["status"] == "wrong_answer"


@pytest.mark.parametrize("mode,comparison,actual_code,expected", [
    ("unordered", {"mode": "unordered"}, "function f(x) { return [2, 1]; }", [1, 2]),
    ("float_tolerance", {"mode": "float_tolerance", "epsilon": 1e-6},
     "function f(x) { return 0.1 + 0.2; }", 0.3),
    ("any_of", {"mode": "any_of"}, "function f(x) { return 'b'; }", ["a", "b", "c"]),
])
def test_comparison_modes(mode, comparison, actual_code, expected):
    res = results(payload(actual_code, [case(0, [0], expected)], comparison=comparison))
    assert res[0]["status"] == "passed", res[0]


def test_unsupported_kind_is_reported_not_crashed():
    pl = payload("class C {}", [case(0, [[], []], None)])
    pl["kind"] = "operations"
    proc = run_harness(pl)
    assert proc.returncode == 0, proc.stderr
    res = json.loads(proc.stdout)["results"]
    assert res[0]["status"] == "runtime_error"
    assert "operations" in res[0]["error"]


def test_invalid_payload_json_exits_nonzero():
    proc = run_harness(None, raw="{not json")
    assert proc.returncode == 2
    assert "invalid payload JSON" in proc.stderr


# --- async support (DESIGN.md §13) ------------------------------------------
# A returned Promise is awaited and raced against the case's remaining time
# budget on a real timer — `setTimeout`/`clearTimeout` are bound into the
# sandbox for exactly this.

def test_resolved_promise_is_awaited_and_compared():
    code = "function f(n) { return new Promise((resolve) => setTimeout(() => resolve(n * 2), 30)); }"
    res = results(payload(code, [case(0, [21], 42)]))
    assert res[0]["status"] == "passed"
    assert res[0]["output"] == "42"


def test_promise_that_settles_too_late_times_out():
    code = "function f(n) { return new Promise((resolve) => setTimeout(() => resolve(n), 5000)); }"
    res = results(payload(code, [case(0, [1], 1)], time_limit_ms=150))
    assert res[0]["status"] == "time_limit_exceeded"
    assert res[0]["runtime_ms"] == 150


def test_rejected_promise_reports_thrown_message_not_harness_internals():
    code = "function f(n) { return new Promise((_, reject) => setTimeout(() => reject(new Error('boom')), 10)); }"
    res = results(payload(code, [case(0, [1], None)]))
    assert res[0]["status"] == "runtime_error"
    assert "boom" in res[0]["error"]
    assert "harness.js" not in res[0]["error"]
    assert "internal/" not in res[0]["error"]


def test_console_log_inside_async_callback_is_captured():
    code = ("function f(n) { return new Promise((resolve) => setTimeout(() => "
            "{ console.log('resolving', n); resolve(n); }, 10)); }")
    res = results(payload(code, [case(0, [7], 7)]))
    assert res[0]["status"] == "passed"
    assert res[0]["stdout"] == "resolving 7"


def test_leaked_interval_does_not_hang_the_process():
    # A submission that never resolves and leaves a repeating timer running
    # must not keep the harness process alive past its own time_limit_ms —
    # `main()`'s explicit `process.exit()` is what guarantees this.
    code = "function f(n) { setInterval(() => {}, 10); return new Promise(() => {}); }"
    proc = run_harness(payload(code, [case(0, [1], 1)], time_limit_ms=150), timeout=5)
    assert proc.returncode == 0, proc.stderr
    res = json.loads(proc.stdout)["results"]
    assert res[0]["status"] == "time_limit_exceeded"


def test_sync_call_that_exhausts_the_budget_times_out_without_awaiting():
    # If the synchronous portion of the call already used up the whole budget,
    # a returned-but-unsettled promise must be reported as timed out rather
    # than waited on further.
    code = ("function f(n) { const start = Date.now(); while (Date.now() - start < 150) {} "
            "return new Promise((resolve) => setTimeout(() => resolve(n), 5000)); }")
    res = results(payload(code, [case(0, [1], 1)], time_limit_ms=150))
    assert res[0]["status"] == "time_limit_exceeded"


def test_multiple_async_cases_do_not_leak_stdout_between_cases():
    code = ("function f(n) { return new Promise((resolve) => setTimeout(() => "
            "{ console.log('case', n); resolve(n + 1); }, 10)); }")
    res = results(payload(code, [case(0, [1], 2), case(1, [10], 11)]))
    assert [r["status"] for r in res] == ["passed", "passed"]
    assert res[0]["stdout"] == "case 1"
    assert res[1]["stdout"] == "case 10"


# --- process-level containment (a real host timer's callback runs outside of
# any try/catch this file controls, so these are the ways it can otherwise
# take the whole harness process down with it, losing every case's results
# rather than just the one that misbehaved) --------------------------------

def test_throw_inside_a_timer_callback_fails_only_that_case():
    code = ("function f(n) { setTimeout(() => { throw new Error('boom'); }, 10); "
            "return new Promise((resolve) => setTimeout(() => resolve(n), 50)); }")
    res = results(payload(code, [case(0, [1], 1)]))
    assert res[0]["status"] == "runtime_error"
    assert "boom" in res[0]["error"]


def test_promise_that_rejects_after_budget_exhausted_does_not_crash():
    # awaitIfThenable must attach a rejection handler to the returned promise
    # even when the synchronous portion already used up the whole budget (an
    # immediate timeout) — otherwise that later rejection is a completely
    # unobserved 'unhandledRejection', which crashes the process and loses
    # every other case's already-computed results, not just this one.
    code = ("function f(n) { const start = Date.now(); while (Date.now() - start < 100) {} "
            "return new Promise((_, reject) => setTimeout(() => reject(new Error('late')), 5)); }")
    res = results(payload(code, [case(0, [1], 1), case(1, [2], 2)], time_limit_ms=100))
    assert [r["status"] for r in res] == ["time_limit_exceeded", "time_limit_exceeded"]


def test_large_result_set_is_not_truncated_by_exit_racing_the_write():
    # main() must wait for process.stdout.write's own callback before calling
    # process.exit() — exiting right after issuing the write can tear the
    # process down before a large payload actually reaches the pipe.
    code = "function f(n) { for (let i = 0; i < 50; i++) console.log('x'.repeat(80)); return n; }"
    cases = [case(i, [i], i) for i in range(200)]
    res = results(payload(code, cases))
    assert len(res) == 200
    assert all(r["status"] == "passed" for r in res)


def test_leaked_interval_does_not_contaminate_a_later_cases_stdout():
    code = ("function f(n) {\n"
            "  if (n === 1) { setInterval(() => console.log('leaked'), 5); return new Promise(() => {}); }\n"
            "  return new Promise((resolve) => setTimeout(() => { console.log('own', n); resolve(n); }, 60));\n"
            "}")
    res = results(payload(code, [case(0, [1], 1), case(1, [2], 2)], time_limit_ms=300))
    assert res[0]["status"] == "time_limit_exceeded"
    assert res[1]["status"] == "passed"
    assert res[1]["stdout"] == "own 2"
    assert "leaked" not in res[1]["stdout"]
