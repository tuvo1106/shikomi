"""Harness protocol tests for the Rust harness (DESIGN.md §5.3, §13; ADR-0004).

The same behavioral contract as test_protocol.py and test_js_protocol.py,
plus the failure modes only a compiled language has: compile errors, stack
overflow, allocation failure, `process::exit`, integer overflow. The last few
tests pin the per-case process isolation that contains them: one case's crash
must never cost another case its result.

Docker-marked: the harness needs rustc and the prebuilt prelude, which only
the image has (rust_runner.py). Build it first:

    docker build -f judge/Dockerfile.rust -t shikomi-judge-rust:latest judge/
"""
import json
import pathlib
import time

import pytest

from rust_runner import run_rust_container, rust_results

pytestmark = pytest.mark.docker

TRUNC_MAX = 4096 + len("…(truncated)")


def payload(user_code, test_cases, function_name="f", comparison=None, time_limit_ms=2000,
            stop_on_first_failure=False, params=None, memory_limit_mb=256, kind=None):
    pl = {
        "function_name": function_name,
        "user_code": user_code,
        "test_cases": test_cases,
        "comparison": comparison or {"mode": "exact"},
        "time_limit_ms": time_limit_ms,
        "memory_limit_mb": memory_limit_mb,
        "stop_on_first_failure": stop_on_first_failure,
        "params": params if params is not None else [],
    }
    if kind:
        pl["kind"] = kind
    return pl


def case(i, inp, expected):
    return {"id": i, "input": inp, "expected": expected}


PAIR_SUM = """use std::collections::HashMap;

fn pair_sum(nums: Vec<i32>, target: i32) -> Vec<i32> {
    let mut seen: HashMap<i32, i32> = HashMap::new();
    for (i, &n) in nums.iter().enumerate() {
        if let Some(&j) = seen.get(&(target - n)) {
            return vec![j, i as i32];
        }
        seen.insert(n, i as i32);
    }
    vec![]
}
"""
PAIR_SUM_PARAMS = [{"name": "nums", "type": "Vec<i32>"}, {"name": "target", "type": "i32"}]


def test_correct_solution_all_pass():
    res = rust_results(payload(PAIR_SUM, [
        case(0, [[4, 9, 1, 6], 7], [2, 3]),
        case(1, [[3, 3], 6], [0, 1]),
    ], function_name="pair_sum", params=PAIR_SUM_PARAMS))
    assert [r["status"] for r in res] == ["passed", "passed"]
    assert res[0]["output"] == "[2,3]"  # compact JSON, like the other harnesses
    assert res[0]["test_case_id"] == 0 and res[0]["error"] is None
    assert isinstance(res[0]["runtime_ms"], float)


def test_wrong_answer():
    res = rust_results(payload("fn f(x: i32) -> i32 { x + 1 }", [case(0, [1], 3)]))
    assert res[0]["status"] == "wrong_answer"
    assert res[0]["output"] == "2"


def test_compile_error_is_single_runtime_error_with_diagnostics():
    # Like harness.py's SyntaxError: one row, no case runs, rustc's message names
    # the user's line (the glue sits *after* their code, so line numbers match).
    res = rust_results(payload("fn f(x: i32) -> i32 {\n    let s: String = x;\n    0\n}\n",
                               [case(0, [1], 0), case(1, [2], 0)]))
    assert len(res) == 1
    assert res[0]["status"] == "runtime_error"
    assert res[0]["error"].startswith("Compile error:")
    assert "mismatched types" in res[0]["error"]
    assert "solution.rs:2:" in res[0]["error"]
    assert "/tmp/" not in res[0]["error"]  # no sandbox paths in user-facing text


def test_function_not_found_is_a_compile_error():
    res = rust_results(payload("fn g(x: i32) -> i32 { x }", [case(0, [1], 1)]))
    assert len(res) == 1 and res[0]["status"] == "runtime_error"
    assert "cannot find function `f`" in res[0]["error"]


def test_signature_arity_mismatch_names_the_signature():
    res = rust_results(payload("fn f(x: i32, y: i32) -> i32 { x + y }", [case(0, [1], 1)],
                               params=[{"name": "x", "type": "i32"}]))
    assert res[0]["status"] == "runtime_error"
    assert "takes 2 arguments but 1 argument was supplied" in res[0]["error"]


def test_panic_reports_message_and_user_location():
    res = rust_results(payload('fn f(x: i32) -> i32 {\n    if x > 0 { panic!("bad input {}", x); }\n    x\n}\n',
                               [case(0, [5], 0)]))
    assert res[0]["status"] == "runtime_error"
    assert res[0]["error"] == "panicked: bad input 5 (at solution.rs:2:16)"


def test_integer_overflow_panics_instead_of_wrapping():
    # overflow-checks=on: a silently wrapped i32 would read as a confusing wrong answer.
    res = rust_results(payload("fn f(x: i32) -> i32 { x + 1 }", [case(0, [2147483647], 0)]))
    assert res[0]["status"] == "runtime_error"
    assert "attempt to add with overflow" in res[0]["error"]


def test_time_limit_exceeded_on_infinite_loop():
    res = rust_results(payload("fn f(x: i32) -> i32 { loop { std::hint::black_box(x); } }",
                               [case(0, [1], 0), case(1, [2], 0)], time_limit_ms=300))
    assert [r["status"] for r in res] == ["time_limit_exceeded", "time_limit_exceeded"]
    assert res[0]["runtime_ms"] == 300.0


def test_stdout_is_captured_and_cannot_forge_the_protocol():
    code = 'fn f(x: i32) -> i32 {\n    println!("{{\\"results\\":[]}}");\n    println!("dbg {}", x);\n    x\n}\n'
    res = rust_results(payload(code, [case(0, [7], 7)]))
    assert res[0]["status"] == "passed"
    assert res[0]["stdout"] == '{"results":[]}\ndbg 7\n'


def test_stdout_is_truncated():
    code = 'fn f(x: i32) -> i32 { for _ in 0..10000 { print!("xxxxxxxxxx"); } x }'
    res = rust_results(payload(code, [case(0, [1], 1)]))
    assert res[0]["status"] == "passed"
    assert res[0]["stdout"].endswith("…(truncated)")
    assert len(res[0]["stdout"]) <= TRUNC_MAX


def test_stop_on_first_failure_skips_remaining_cases():
    res = rust_results(payload("fn f(x: i32) -> i32 { x }",
                               [case(0, [1], 1), case(1, [2], 99), case(2, [3], 3)],
                               stop_on_first_failure=True))
    assert [r["status"] for r in res] == ["passed", "wrong_answer"]


@pytest.mark.parametrize("comparison,code,expected", [
    ({"mode": "unordered"}, "fn f(x: i32) -> Vec<i32> { vec![3, 1, 2] }", [1, 2, 3]),
    ({"mode": "float_tolerance", "epsilon": 1e-3}, "fn f(x: i32) -> f64 { 1.0 / 3.0 }", 0.3333),
    ({"mode": "any_of"}, "fn f(x: i32) -> Vec<i32> { vec![1, 0] }", [[0, 1], [1, 0]]),
    ({"mode": "exact"}, "fn f(x: i32) -> f64 { 2.0 }", 2),  # JSON 2 == 2.0, as in harness.py
])
def test_comparison_modes(comparison, code, expected):
    res = rust_results(payload(code, [case(0, [0], expected)], comparison=comparison))
    assert res[0]["status"] == "passed", res[0]


def test_unordered_is_a_multiset_not_a_set():
    res = rust_results(payload("fn f(x: i32) -> Vec<i32> { vec![1, 1, 2] }",
                               [case(0, [0], [1, 2, 2])], comparison={"mode": "unordered"}))
    assert res[0]["status"] == "wrong_answer"


def test_argument_types_are_inferred_from_the_signature():
    # No type table: rustc infers each decoded argument from the user's own
    # parameter types (prelude.rs), so all of these just work.
    code = """
fn f(grid: Vec<Vec<char>>, words: Vec<String>, pair: (i64, bool), maybe: Option<u8>, ratio: f64)
    -> (usize, String, Vec<Option<i64>>, f64) {
    let joined = words.join("-");
    let _ = (grid[0][1], maybe);
    (grid.len(), joined, vec![Some(pair.0), None], ratio * 2.0)
}
"""
    res = rust_results(payload(code, [case(
        0,
        [[["a", "b"], ["c", "d"]], ["héllo", "wörld 🚀"], [9007199254740993, True], None, 1.25],
        [2, "héllo-wörld 🚀", [9007199254740993, None], 2.5],
    )], params=[{"name": n, "type": "?"} for n in "abcde"]))
    assert res[0]["status"] == "passed", res[0]
    # i64 beyond 2^53 survives exactly (Int, not f64), and whole floats print as floats
    assert res[0]["output"] == '[2,"héllo-wörld 🚀",[9007199254740993,null],2.5]'


def test_type_the_prelude_cannot_decode_fails_at_compile_time():
    # The flip side of inference: an unsupported parameter type is a compile
    # error naming the missing FromJson impl, caught when the problem's own
    # reference solution is judged (test_seed_solutions.py), not at submit time.
    code = "use std::collections::HashSet;\nfn f(s: HashSet<i32>) -> usize { s.len() }"
    res = rust_results(payload(code, [case(0, [[1, 2]], 2)]))
    assert res[0]["status"] == "runtime_error"
    assert "FromJson` is not implemented for `HashSet<i32>`" in res[0]["error"]


def test_unit_return_encodes_as_null():
    res = rust_results(payload("fn f(x: i32) { let _ = x; }", [case(0, [1], None)]))
    assert res[0]["status"] == "passed"
    assert res[0]["output"] == "null"


def test_argument_that_does_not_fit_the_type_is_a_runtime_error():
    res = rust_results(payload("fn f(x: u8) -> u8 { x }", [case(0, [300], 0), case(1, ["s"], 0)]))
    assert res[0]["status"] == "runtime_error"
    assert "argument 1: 300 is out of range for u8" in res[0]["error"]
    assert "argument 1: expected u8, got \"s\"" in res[1]["error"]


def test_unsupported_kind_is_reported_not_crashed():
    res = rust_results(payload("-- SELECT 1", [case(3, [[], []], [])], kind="sql"))
    assert res == [{
        "test_case_id": 3, "status": "runtime_error", "runtime_ms": 0.0, "output": None,
        "stdout": "", "error": "kind 'sql' is not supported by the Rust harness",
    }]


def test_function_name_that_is_not_an_identifier_is_refused():
    # function_name is pasted into generated source, so it must be a plain
    # identifier. A bad one is a problem-authoring fault, not the submitter's, so
    # the harness exits without results (the worker reports judge_error).
    proc = run_rust_container(json.dumps(payload("fn f() {}", [case(0, [], None)],
                                                 function_name="f(); std::process::exit(0); g")))
    assert proc.returncode == 3 and proc.stdout == ""
    assert "invalid function_name" in proc.stderr


def test_invalid_payload_json_exits_nonzero():
    proc = run_rust_container("{not json")
    assert proc.returncode == 2
    assert "invalid payload JSON" in proc.stderr


# --- per-case process isolation (ADR-0004) -----------------------------------
# Each of these would, in a single shared process, lose *every* case's result.

HOSTILE = """
fn deep(n: u64) -> u64 {
    let pad = [n; 64];
    std::hint::black_box(&pad);
    if n == 0 { 0 } else { deep(n - 1).wrapping_add(pad[3]) }
}

fn f(mode: i32) -> i32 {
    match mode {
        1 => { let mut v: Vec<Vec<u8>> = Vec::new(); loop { v.push(vec![1u8; 1 << 20]); } }
        2 => deep(1 << 40) as i32,
        3 => std::process::exit(0),
        4 => std::process::abort(),
        _ => mode,
    }
}
"""


def test_each_failure_is_contained_to_its_own_case():
    res = rust_results(payload(HOSTILE, [
        case(0, [0], 0),
        case(1, [1], 0),
        case(2, [2], 0),
        case(3, [3], 0),
        case(4, [4], 0),
        case(5, [5], 5),
    ], memory_limit_mb=256))
    by_id = {r["test_case_id"]: r for r in res}
    assert by_id[0]["status"] == "passed"
    assert by_id[1]["status"] == "memory_limit_exceeded"  # RLIMIT_AS, not the container OOM
    assert by_id[2]["status"] == "runtime_error"
    assert "stack overflow" in by_id[2]["error"]
    assert by_id[3]["status"] == "runtime_error"
    assert "exited (code 0) before returning" in by_id[3]["error"]
    assert by_id[4]["status"] == "runtime_error"
    assert by_id[4]["error"].startswith("aborted")
    assert by_id[5]["status"] == "passed"  # the run survived everything above


def test_recursion_depth_typical_of_dfs_fits_the_stack():
    # 10^5-deep recursion is routine in tree/graph solutions; the harness raises the
    # case's stack limit to 64MB so it doesn't need rewriting as an explicit stack.
    code = "fn depth(n: u64) -> u64 { if n == 0 { 0 } else { 1 + depth(n - 1) } }\nfn f(n: u64) -> u64 { depth(n) }"
    res = rust_results(payload(code, [case(0, [100000], 100000)]))
    assert res[0]["status"] == "passed", res[0]


def test_one_case_mutating_state_does_not_leak_into_the_next():
    # A static is per-process, so a fresh process per case resets it.
    code = """
use std::sync::atomic::{AtomicI32, Ordering};
static CALLS: AtomicI32 = AtomicI32::new(0);
fn f(x: i32) -> i32 { CALLS.fetch_add(1, Ordering::SeqCst) + x }
"""
    res = rust_results(payload(code, [case(0, [10], 10), case(1, [20], 20)]))
    assert [r["status"] for r in res] == ["passed", "passed"]


def test_submission_cannot_see_expected():
    # The child only ever receives the input; reading its own stdin again or its
    # environment turns up no `expected` to copy.
    code = """
fn f(x: i32) -> String {
    let env: Vec<String> = std::env::vars().map(|(k, v)| format!("{}={}", k, v)).collect();
    env.join(";")
}
"""
    res = rust_results(payload(code, [case(0, [1], "SECRET-EXPECTED-VALUE")]))
    assert res[0]["status"] == "wrong_answer"
    assert "SECRET-EXPECTED-VALUE" not in json.dumps(res[0]["output"])


def test_processes_a_case_leaves_behind_do_not_starve_later_cases():
    # Case 0 fills most of --pids-limit (64) with background processes and
    # returns. The harness is PID 1, so those are reparented to it. Unless it
    # kills each case's whole process group and reaps the orphans, their pids
    # stay taken and later cases can't spawn (or can't even be started).
    code = """
fn f(n: i32) -> i32 {
    let mut started = 0;
    for _ in 0..n {
        if std::process::Command::new("sleep").arg("60").spawn().is_ok() { started += 1; }
    }
    started
}
"""
    res = rust_results(payload(code, [case(0, [55], 55), case(1, [20], 20), case(2, [20], 20)]))
    assert [r["status"] for r in res] == ["passed", "passed", "passed"], res


def test_a_case_cannot_read_the_payload_out_of_the_harness():
    # The case's process runs as the same uid as the harness (PID 1), which holds
    # the whole payload, every `expected` included. The harness marks itself
    # non-dumpable, so same-uid /proc access to its memory and environment is
    # refused, and the case gets a scrubbed environment of its own.
    code = """
fn f(_x: i32) -> String {
    let mem = std::fs::File::open("/proc/1/mem").is_ok();
    let environ = std::fs::read("/proc/1/environ").is_ok();
    let mut keys: Vec<String> = std::env::vars().map(|(k, _)| k).collect();
    keys.sort();
    format!("mem={} environ={} env={}", mem, environ, keys.join(","))
}
"""
    res = rust_results(payload(code, [case(0, [1], "SECRET-EXPECTED-VALUE")]))
    assert res[0]["output"] == '"mem=false environ=false env=HOME,PATH,SHIKOMI_RESULT,TMPDIR"'


def test_large_input_to_a_program_that_never_reads_it_still_times_out_per_case():
    # A static constructor runs before `main`, so the program never reads stdin.
    # With a >64KB input the pipe fills and the write blocks; the harness has to
    # be enforcing the deadline *during* that write, or it hangs until the
    # container's outer wall-clock kill and every per-case result is lost.
    code = """
extern "C" fn stall() { loop { std::hint::black_box(0); } }
#[used]
#[unsafe(link_section = ".init_array")]
static STALL: extern "C" fn() = stall;

fn f(v: Vec<i64>) -> usize { v.len() }
"""
    big = list(range(40_000))  # ~230KB of JSON
    res = rust_results(payload(code, [case(0, [big], 0), case(1, [big], 0)], time_limit_ms=300),
                       timeout=30)
    assert [r["status"] for r in res] == ["time_limit_exceeded", "time_limit_exceeded"]


def test_integers_past_2_53_are_not_rounded_into_equality():
    # 9007199254740993 isn't representable as f64 and rounds to ...992. Comparing
    # through `as f64` would call this wrong answer correct.
    res = rust_results(payload("fn f(_x: i32) -> i64 { 9007199254740993 }",
                               [case(0, [0], 9007199254740992.0), case(1, [0], 9007199254740993)]))
    assert [r["status"] for r in res] == ["wrong_answer", "passed"]


def test_compile_timeout_comes_from_the_payload():
    # The worker sends compile_timeout_s from app/judge_budget.py, the same number
    # the wall budget reserves. Allowing `long_running_const_eval` stops rustc from
    # giving up on this 2^40-step const loop by itself, so only the harness's 1s
    # deadline can end the compile, and the harness must still report cleanly
    # (rustc and any linker it started are killed as a group).
    code = """#![allow(long_running_const_eval)]
const fn spin(n: u64) -> u64 { let mut i = 0; let mut s = 0; while i < n { s += i; i += 1; } s }
const X: u64 = spin(1u64 << 40);
fn f(_x: i32) -> u64 { X }
"""
    pl = payload(code, [case(0, [0], 0)])
    pl["compile_timeout_s"] = 1
    res = rust_results(pl)
    assert res[0]["status"] == "runtime_error"
    assert res[0]["error"] == "Compilation timed out after 1s"


# --- review round 2 ------------------------------------------------------------


def test_unordered_compare_of_a_large_answer_stays_fast():
    # The comparison runs untimed, bounded only by the worker's outer wall clock.
    # An O(n^2) multiset match took ~6s at 100k elements and could turn a correct
    # answer into a whole-run TLE (harness.py hit and fixed the same bug).
    n = 100_000
    code = f"fn f(_x: i32) -> Vec<i64> {{ (0..{n}).rev().collect() }}"
    started = time.monotonic()
    res = rust_results(payload(code, [case(0, [0], list(range(n)))],
                               comparison={"mode": "unordered"}))
    assert res[0]["status"] == "passed"
    assert time.monotonic() - started < 4


def test_unordered_still_treats_1_and_1_0_as_equal():
    res = rust_results(payload("fn f(_x: i32) -> Vec<f64> { vec![2.0, 1.0] }",
                               [case(0, [0], [1, 2])], comparison={"mode": "unordered"}))
    assert res[0]["status"] == "passed"


def test_output_larger_than_tmp_does_not_fail_a_correct_solution():
    # 40MB of prints against a 32MB /tmp. Output is captured through pipes and
    # truncated, so nothing lands in the tmpfs and the answer still counts.
    code = """
fn f(x: i32) -> i32 {
    let chunk = "x".repeat(1 << 20);
    for _ in 0..40 { print!("{}", chunk); }
    x
}
"""
    res = rust_results(payload(code, [case(0, [7], 7)], time_limit_ms=10000))
    assert res[0]["status"] == "passed", res[0]
    assert res[0]["stdout"].endswith("…(truncated)")


def test_nan_and_infinity_are_values_not_crashes():
    code = "fn f(x: i32) -> f64 { if x == 0 { f64::INFINITY } else { f64::NAN } }"
    res = rust_results(payload(code, [case(0, [0], float("inf")), case(1, [1], 1.0)],
                               comparison={"mode": "float_tolerance", "epsilon": 1e-9}))
    assert [r["status"] for r in res] == ["passed", "wrong_answer"]
    assert res[1]["output"] == "NaN"


def test_user_code_may_define_main_and_shadow_ok():
    # Keeping a `main` for local testing is common, and so is a `use MyEnum::*`
    # whose variants include `Ok`. Neither may collide with the judge's glue.
    code = """
#[derive(Debug)]
enum Verdict { Ok, Bad }
use Verdict::*;

fn f(x: i32) -> String {
    let v = if x > 0 { Ok } else { Bad };
    format!("{:?}", v)
}

fn main() {
    println!("{}", f(1));
}
"""
    res = rust_results(payload(code, [case(0, [1], "Ok"), case(1, [0], "Bad")]))
    assert [r["status"] for r in res] == ["passed", "passed"], res


# --- ListNode / TreeNode codecs (prelude.rs `nodes`, harness.rs `node_structs`) ----
# The wire format must match harness.py's, since a problem's cases are shared
# by every language it's offered in. The structs are generated into the
# submission's crate for the node types a problem declares, so these payloads
# declare them the way a problem file would.

LIST = [{"name": "head", "type": "ListNode"}]
TREE = [{"name": "root", "type": "TreeNode"}]


def nodes_payload(code, cases, function_name, params, return_type="", **kw):
    pl = payload(code, cases, function_name=function_name, params=params, **kw)
    pl["return_type"] = return_type
    return pl


REVERSE_LIST = """fn reverse(head: Option<Box<ListNode>>) -> Option<Box<ListNode>> {
    let (mut prev, mut cur) = (None, head);
    while let Some(mut node) = cur {
        cur = node.next.take();
        node.next = prev;
        prev = Some(node);
    }
    prev
}
"""

MIRROR_TREE = """use std::cell::RefCell;
use std::rc::Rc;

fn mirror(root: Option<Rc<RefCell<TreeNode>>>) -> Option<Rc<RefCell<TreeNode>>> {
    if let Some(node) = &root {
        let mut n = node.borrow_mut();
        let (l, r) = (n.left.take(), n.right.take());
        n.left = mirror(r);
        n.right = mirror(l);
    }
    root
}
"""

RC = "use std::cell::RefCell;\nuse std::rc::Rc;\n"


def test_list_round_trips_and_empty_is_an_empty_array():
    res = rust_results(nodes_payload(REVERSE_LIST, [
        case(0, [[1, 2, 3]], [3, 2, 1]),
        case(1, [[]], []),        # None encodes as [], as in harness.py
        case(2, [None], []),      # null also reads as an empty list
        case(3, [[7]], [7]),
    ], "reverse", LIST, "ListNode"))
    assert [r["status"] for r in res] == ["passed"] * 4
    assert res[1]["output"] == "[]"


def test_tree_round_trips_in_null_padded_level_order():
    res = rust_results(nodes_payload(MIRROR_TREE, [
        case(0, [[4, 2, 7, 1, 3, 6, 9]], [4, 7, 2, 9, 6, 3, 1]),
        # A null keeps no slots for its absent children, and trailing nulls are trimmed.
        case(1, [[1, None, 2, 3]], [1, 2, None, None, 3]),
        case(2, [[]], []),
        case(3, [[5, 4]], [5, None, 4]),
    ], "mirror", TREE, "TreeNode"))
    assert [r["status"] for r in res] == ["passed"] * 4


def test_node_values_are_encoded_not_the_struct():
    code = "fn sum(head: Option<Box<ListNode>>) -> i64 {\n" \
           "    let mut t = 0; let mut c = head.as_deref();\n" \
           "    while let Some(n) = c { t += n.val as i64; c = n.next.as_deref(); }\n    t\n}\n"
    res = rust_results(nodes_payload(code, [case(0, [[1, 2, 3]], 6)], "sum", LIST))
    assert res[0]["status"] == "passed"


def test_list_of_lists_uses_the_same_codec_per_element():
    code = "fn lens(lists: Vec<Option<Box<ListNode>>>) -> Vec<usize> {\n" \
           "    lists.iter().map(|l| { let mut n = 0; let mut c = l.as_deref();\n" \
           "        while let Some(x) = c { n += 1; c = x.next.as_deref(); } n }).collect()\n}\n"
    res = rust_results(nodes_payload(code, [case(0, [[[1, 2], [], [3]]], [2, 0, 1])], "lens",
                                     [{"name": "lists", "type": "List[ListNode]"}]))
    assert res[0]["status"] == "passed", res[0]


def test_vec_of_trees_returns_each_encoded():
    code = RC + "fn two(a: i32) -> Vec<Option<Rc<RefCell<TreeNode>>>> {\n" \
                "    vec![Some(Rc::new(RefCell::new(TreeNode::new(a)))), None]\n}\n"
    res = rust_results(nodes_payload(code, [case(0, [8], [[8], []])], "two",
                                     [{"name": "a", "type": "i32"}], "List[TreeNode]"))
    assert res[0]["status"] == "passed", res[0]
    assert res[0]["output"] == "[[8],[]]"


def test_deep_list_and_degenerate_tree_do_not_overflow_the_codec():
    n = 100_000
    chain_tree = [0]
    for i in range(1, n):
        chain_tree += [None, i]   # every node a right child: depth n
    res = rust_results(nodes_payload(
        REVERSE_LIST, [case(0, [list(range(n))], list(range(n - 1, -1, -1)))], "reverse", LIST, "ListNode"))
    assert res[0]["status"] == "passed", res[0]["error"]
    code = RC + "fn same(root: Option<Rc<RefCell<TreeNode>>>) -> Option<Rc<RefCell<TreeNode>>> { root }\n"
    res = rust_results(nodes_payload(code, [case(0, [chain_tree], chain_tree)], "same", TREE, "TreeNode"))
    assert res[0]["status"] == "passed", res[0]["error"]


def test_a_cyclic_tree_answer_is_refused_not_looped_on():
    # Rc makes a cycle expressible in safe code. The encoder must neither hang nor
    # judge a truncation (here [1], which would match an expected [1]).
    code = RC + "fn loopy(root: Option<Rc<RefCell<TreeNode>>>) -> Option<Rc<RefCell<TreeNode>>> {\n" \
                "    let r = root.clone().unwrap(); r.borrow_mut().left = root.clone(); root\n}\n"
    res = rust_results(nodes_payload(code, [case(0, [[1]], [1])], "loopy", TREE, "TreeNode"))
    assert res[0]["status"] == "runtime_error"
    assert res[0]["error"].startswith("the returned tree has a cycle")


def test_a_tree_sharing_a_subtree_between_parents_is_a_valid_answer():
    # Memoized solutions ("all full binary trees") reuse equal subtrees on purpose;
    # that's a finite tree with a clean encoding, not a cycle.
    code = RC + "fn share(v: i32) -> Option<Rc<RefCell<TreeNode>>> {\n" \
                "    let kid = Rc::new(RefCell::new(TreeNode::new(v)));\n" \
                "    let root = Rc::new(RefCell::new(TreeNode::new(0)));\n" \
                "    root.borrow_mut().left = Some(Rc::clone(&kid)); root.borrow_mut().right = Some(kid);\n" \
                "    Some(root)\n}\n"
    res = rust_results(nodes_payload(code, [case(0, [5], [0, 5, 5])], "share",
                                     [{"name": "v", "type": "i32"}], "TreeNode"))
    assert res[0]["status"] == "passed", res[0]


def test_an_exponentially_shared_tree_is_refused_as_too_large():
    # 30 levels, each node's two children the same node: 30 nodes, 2^30 entries.
    code = RC + "fn big(n: i32) -> Option<Rc<RefCell<TreeNode>>> {\n" \
                "    let mut t = Rc::new(RefCell::new(TreeNode::new(0)));\n" \
                "    for _ in 0..n { let p = Rc::new(RefCell::new(TreeNode::new(0)));\n" \
                "        p.borrow_mut().left = Some(Rc::clone(&t)); p.borrow_mut().right = Some(t); t = p; }\n" \
                "    Some(t)\n}\n"
    res = rust_results(nodes_payload(code, [case(0, [30], [0])], "big",
                                     [{"name": "n", "type": "i32"}], "TreeNode"))
    assert res[0]["status"] == "runtime_error"
    assert "too large to encode" in res[0]["error"]


def test_null_inside_a_list_is_a_decode_error():
    res = rust_results(nodes_payload(REVERSE_LIST, [case(0, [[1, None]], [])], "reverse", LIST, "ListNode"))
    assert res[0]["status"] == "runtime_error"
    assert "can't be null" in res[0]["error"]


def test_the_node_structs_are_the_submissions_own_types():
    # The orphan rule would forbid both impls for a struct from another crate.
    code = """use std::cmp::Ordering;
use std::collections::BinaryHeap;

impl Ord for ListNode {
    fn cmp(&self, other: &Self) -> Ordering { other.val.cmp(&self.val) }
}
impl PartialOrd for ListNode {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> { Some(self.cmp(other)) }
}
impl ListNode {
    fn detach(mut self: Box<Self>) -> (Box<Self>, Option<Box<Self>>) { let n = self.next.take(); (self, n) }
}

fn merge(lists: Vec<Option<Box<ListNode>>>) -> Option<Box<ListNode>> {
    let mut heap: BinaryHeap<Box<ListNode>> = lists.into_iter().flatten().collect();
    let mut out = None;
    let mut tail = &mut out;
    while let Some(node) = heap.pop() {
        let (node, rest) = node.detach();
        if let Some(r) = rest { heap.push(r); }
        tail = &mut tail.insert(node).next;
    }
    out
}
"""
    res = rust_results(nodes_payload(code, [case(0, [[[1, 4], [2, 3], []]], [1, 2, 3, 4])], "merge",
                                     [{"name": "lists", "type": "List[ListNode]"}], "ListNode"))
    assert res[0]["status"] == "passed", res[0]


def test_redefining_listnode_gets_a_hint():
    code = "#[derive(Debug)]\npub struct ListNode { pub val: i32, pub next: Option<Box<ListNode>> }\n" + REVERSE_LIST
    res = rust_results(nodes_payload(code, [case(0, [[1]], [1])], "reverse", LIST, "ListNode"))
    assert res[0]["status"] == "runtime_error"
    assert "Compile error" in res[0]["error"]
    assert "judge already defines the node struct" in res[0]["error"]


def test_a_node_name_in_a_comment_does_not_trigger_the_hint():
    # The starter's shape comment names the struct; a wrong signature must get
    # rustc's own error, not advice to remove a struct the user never wrote.
    code = "// pub struct ListNode {\n//     pub val: i32,\n// }\n" \
           "fn reverse(head: &ListNode) -> i32 { head.val }\n"
    res = rust_results(nodes_payload(code, [case(0, [[1]], 1)], "reverse", LIST))
    assert res[0]["status"] == "runtime_error"
    assert "Compile error" in res[0]["error"]
    assert "Hint" not in res[0]["error"]


def test_a_problem_without_node_types_leaves_the_names_free():
    # A trie problem declares no node codec, so its own TreeNode can't collide.
    code = "use std::collections::HashMap;\n#[derive(Default)]\nstruct TreeNode { kids: HashMap<char, TreeNode>, end: bool }\n" \
           "fn count(words: Vec<String>) -> usize {\n    let mut root = TreeNode::default();\n" \
           "    for w in &words { let mut n = &mut root; for c in w.chars() { n = n.kids.entry(c).or_default(); } n.end = true; }\n" \
           "    fn walk(n: &TreeNode) -> usize { n.end as usize + n.kids.values().map(walk).sum::<usize>() }\n    walk(&root)\n}\n"
    res = rust_results(payload(code, [case(0, [["ab", "abc", "ab"]], 2)], function_name="count",
                               params=[{"name": "words", "type": "Vec<String>"}]))
    assert res[0]["status"] == "passed", res[0]


# --- the shared-node codecs: CyclicListNode, RandomListNode, GraphNode --------

CYC = [{"name": "head", "type": "CyclicListNode"}]
RND = [{"name": "head", "type": "RandomListNode"}]
GRAPH = [{"name": "node", "type": "GraphNode"}]

CYCLE_START = RC + """fn start(head: Option<Rc<RefCell<CyclicListNode>>>) -> Option<Rc<RefCell<CyclicListNode>>> {
    let mut seen = std::collections::HashSet::new();
    let mut cur = head;
    while let Some(n) = cur {
        if !seen.insert(Rc::as_ptr(&n)) {
            return Some(n);
        }
        cur = n.borrow().next.clone();
    }
    None
}
"""


def test_cyclic_list_decodes_a_real_cycle_and_answers_by_identity():
    res = rust_results(nodes_payload(CYCLE_START, [
        case(0, [[[3, 2, 0, -4], 1]], 1),
        case(1, [[[1, 2], 0]], 0),
        case(2, [[[1], -1]], None),       # no cycle
        case(3, [[]], None),              # empty list
        case(4, [[[7], 0]], 0),           # a node pointing at itself
        case(5, [[[1, 1, 1, 1], 2]], 2),  # repeated values: only identity can tell
    ], "start", CYC, "CyclicListNode"))
    assert [r["status"] for r in res] == ["passed"] * 6, res


def test_a_fabricated_cyclic_node_is_not_an_input_node():
    code = RC + ("fn start(head: Option<Rc<RefCell<CyclicListNode>>>) -> Option<Rc<RefCell<CyclicListNode>>> {\n"
                 "    let v = head.unwrap().borrow().val;\n"
                 "    Some(Rc::new(RefCell::new(CyclicListNode::new(v))))\n}\n")
    res = rust_results(nodes_payload(code, [case(0, [[[5, 6], 0]], 0)], "start", CYC, "CyclicListNode"))
    assert res[0]["status"] == "wrong_answer" and res[0]["output"] == "null"


COPY_RANDOM = RC + """use std::collections::HashMap;

fn copy(head: Option<Rc<RefCell<RandomListNode>>>) -> Option<Rc<RefCell<RandomListNode>>> {
    let mut old = Vec::new();
    let mut cur = head;
    while let Some(n) = cur {
        cur = n.borrow().next.clone();
        old.push(n);
    }
    let at: HashMap<_, _> = old.iter().enumerate().map(|(i, n)| (Rc::as_ptr(n), i)).collect();
    let new: Vec<_> = old.iter().map(|n| Rc::new(RefCell::new(RandomListNode::new(n.borrow().val)))).collect();
    for (i, n) in old.iter().enumerate() {
        let mut c = new[i].borrow_mut();
        c.next = new.get(i + 1).cloned();
        c.random = n.borrow().random.as_ref().map(|r| Rc::clone(&new[at[&Rc::as_ptr(r)]]));
    }
    new.into_iter().next()
}
"""


def test_random_list_round_trips_through_a_deep_copy():
    wire = [[7, None], [13, 0], [11, 4], [10, 2], [1, 0]]
    res = rust_results(nodes_payload(COPY_RANDOM, [
        case(0, [wire], wire),
        case(1, [[]], []),
        case(2, [[[1, 0]]], [[1, 0]]),  # random pointing at itself
    ], "copy", RND, "RandomListNode"))
    assert [r["status"] for r in res] == ["passed"] * 3, res


def test_a_copy_whose_random_points_into_the_input_encodes_null():
    # A shallow copy: fresh `next` chain, but `random` still aims at input nodes.
    code = RC + ("fn copy(head: Option<Rc<RefCell<RandomListNode>>>) -> Option<Rc<RefCell<RandomListNode>>> {\n"
                 "    let h = head?; let n = h.borrow();\n"
                 "    let c = RandomListNode { val: n.val, next: None, random: n.random.clone() };\n"
                 "    Some(Rc::new(RefCell::new(c)))\n}\n")
    res = rust_results(nodes_payload(code, [case(0, [[[4, 0]]], [[4, 0]])], "copy", RND, "RandomListNode"))
    assert res[0]["status"] == "wrong_answer" and res[0]["output"] == "[[4,null]]"


def test_a_random_list_whose_next_loops_is_refused():
    code = RC + ("fn copy(head: Option<Rc<RefCell<RandomListNode>>>) -> Option<Rc<RefCell<RandomListNode>>> {\n"
                 "    let h = head?; h.borrow_mut().next = Some(Rc::clone(&h)); Some(h)\n}\n")
    res = rust_results(nodes_payload(code, [case(0, [[[4, None]]], [[4, None]])], "copy", RND, "RandomListNode"))
    assert res[0]["status"] == "runtime_error"
    assert "the returned list has a cycle" in res[0]["error"]


CLONE_GRAPH = RC + """use std::collections::HashMap;

fn clone(node: Option<Rc<RefCell<GraphNode>>>) -> Option<Rc<RefCell<GraphNode>>> {
    let start = node?;
    let mut copies: HashMap<*const RefCell<GraphNode>, Rc<RefCell<GraphNode>>> = HashMap::new();
    let mut stack = vec![Rc::clone(&start)];
    copies.insert(Rc::as_ptr(&start), Rc::new(RefCell::new(GraphNode::new(start.borrow().val))));
    while let Some(n) = stack.pop() {
        let copy = Rc::clone(&copies[&Rc::as_ptr(&n)]);
        for nb in &n.borrow().neighbors {
            let c = copies.entry(Rc::as_ptr(nb)).or_insert_with(|| {
                stack.push(Rc::clone(nb));
                Rc::new(RefCell::new(GraphNode::new(nb.borrow().val)))
            });
            copy.borrow_mut().neighbors.push(Rc::clone(c));
        }
    }
    Some(Rc::clone(&copies[&Rc::as_ptr(&start)]))
}
"""


def test_graph_round_trips_through_a_clone():
    square = [[2, 4], [1, 3], [2, 4], [1, 3]]
    res = rust_results(nodes_payload(CLONE_GRAPH, [
        case(0, [square], square),
        case(1, [[[]]], [[]]),        # one isolated node
        case(2, [[]], []),            # empty graph
        case(3, [[[2], [1]]], [[2], [1]]),
    ], "clone", GRAPH, "GraphNode"))
    assert [r["status"] for r in res] == ["passed"] * 4, res


def test_a_large_cyclic_graph_encodes_without_hanging():
    n = 20_000
    ring = [[(i - 1) % n + 1, (i + 1) % n + 1] for i in range(n)]  # node i+1 <-> its neighbours
    res = rust_results(nodes_payload(CLONE_GRAPH, [case(0, [ring], ring)], "clone", GRAPH, "GraphNode"))
    assert res[0]["status"] == "passed", res[0]["error"]


def test_a_bad_cycle_position_is_a_decode_error():
    res = rust_results(nodes_payload(CYCLE_START, [case(0, [[[1, 2], 5]], None)], "start", CYC, "CyclicListNode"))
    assert res[0]["status"] == "runtime_error"
    assert "outside the list" in res[0]["error"]


def test_debug_printing_a_cyclic_graph_does_not_recurse():
    # A correct graph is cyclic; a derived Debug would follow the links forever.
    code = RC + ("fn clone(node: Option<Rc<RefCell<GraphNode>>>) -> Option<Rc<RefCell<GraphNode>>> {\n"
                 "    let n = node.clone().unwrap(); println!(\"{:?}\", n.borrow()); dbg!(&n); node\n}\n")
    res = rust_results(nodes_payload(code, [case(0, [[[2], [1]]], [[2], [1]])], "clone", GRAPH, "GraphNode"))
    assert res[0]["status"] == "passed", res[0]
    assert "GraphNode { val: 1, neighbors: [2] }" in res[0]["stdout"]


def test_debug_shows_list_links_as_their_targets_values():
    code = RC + ("fn start(head: Option<Rc<RefCell<CyclicListNode>>>) -> Option<Rc<RefCell<CyclicListNode>>> {\n"
                 "    println!(\"{:?}\", head.as_ref().unwrap().borrow()); None\n}\n")
    res = rust_results(nodes_payload(code, [case(0, [[[7], 0]], None)], "start", CYC, "CyclicListNode"))
    assert res[0]["status"] == "passed" and "CyclicListNode { val: 7, next: Some(7) }" in res[0]["stdout"]


def test_two_cyclic_lists_are_indexed_per_list_like_python():
    code = RC + ("fn second(a: Option<Rc<RefCell<CyclicListNode>>>, b: Option<Rc<RefCell<CyclicListNode>>>)\n"
                 "    -> Option<Rc<RefCell<CyclicListNode>>> { let _ = a; b }\n")
    params = [{"name": "a", "type": "CyclicListNode"}, {"name": "b", "type": "CyclicListNode"}]
    res = rust_results(nodes_payload(code, [case(0, [[[1, 2, 3], -1], [[4, 5], -1]], 0)], "second",
                                     params, "CyclicListNode"))
    assert res[0]["status"] == "passed", res[0]  # b's head is index 0 of b, not 3


def test_a_graph_with_a_huge_value_is_refused_not_allocated():
    code = RC + ("fn clone(node: Option<Rc<RefCell<GraphNode>>>) -> Option<Rc<RefCell<GraphNode>>> {\n"
                 "    node.as_ref().unwrap().borrow_mut().val = i32::MAX; node\n}\n")
    res = rust_results(nodes_payload(code, [case(0, [[[]]], [[]])], "clone", GRAPH, "GraphNode"))
    assert res[0]["status"] == "runtime_error"
    assert "too large to encode" in res[0]["error"]


def test_decoded_rc_nodes_are_kept_alive_so_no_drop_cascades():
    # Freeing a long Rc chain recurses once per node. Each decoded node holds one
    # extra reference (prelude.rs `nodes::KEEP`), so dropping the user's handle only
    # decrements counts and never frees a chain. (A chain long enough to overflow
    # the 64MB stack can't be decoded within the sandbox's memory, so this checks
    # the mechanism rather than the overflow.)
    code = RC + ("fn count(head: Option<Rc<RefCell<RandomListNode>>>) -> Vec<usize> {\n"
                 "    let h = head.unwrap(); let second = h.borrow().next.clone().unwrap();\n"
                 "    vec![Rc::strong_count(&h), Rc::strong_count(&second)]\n}\n")
    res = rust_results(nodes_payload(code, [case(0, [[[1, None], [2, None]]], [2, 3])], "count", RND))
    assert res[0]["status"] == "passed", res[0]  # h: user + registry; second: prev.next + clone + registry


def test_the_return_value_is_never_dropped():
    # `ret` leaks the value (mem::forget): its drop could recurse per node too.
    code = ("struct Loud;\n"
            "impl Drop for Loud { fn drop(&mut self) { println!(\"dropped\"); } }\n"
            "impl shikomi_prelude::ToJson for Loud {\n"
            "    fn to_json(&self) -> Result<shikomi_prelude::Json, String> { Ok(shikomi_prelude::Json::Int(1)) }\n"
            "}\n"
            "fn f(_x: i32) -> Loud { Loud }\n")
    res = rust_results(payload(code, [case(0, [0], 1)], params=[{"name": "x", "type": "i32"}]))
    assert res[0]["status"] == "passed", res[0]
    assert "dropped" not in res[0]["stdout"]


# --- operations mode (prelude.rs `ops`, harness.rs `operations_glue`) -------------------------

def ops_payload(code, cases, class_name="Editor", params=None, **kw):
    pl = payload(code, cases, params=params, kind="operations", **kw)
    pl["class_name"] = class_name
    del pl["function_name"]
    return pl


def ops_case(i, calls, expected, class_name="Editor"):
    """`calls` is [(op, args), ...] after the constructor's args; the wire shape
    is harness.py's `[ops, args]` with `ops[0]` the class name."""
    ctor_args, *rest = calls
    return case(i, [[class_name] + [op for op, _ in rest], [ctor_args] + [a for _, a in rest]],
                [None] + expected)


EDITOR = """struct Editor {
    text: String,
    done: Vec<String>,
    undone: Vec<String>,
}

impl Editor {
    fn new() -> Self {
        Editor { text: String::new(), done: Vec::new(), undone: Vec::new() }
    }

    fn append(&mut self, s: String) -> String {
        self.done.push(self.text.clone());
        self.undone.clear();
        self.text.push_str(&s);
        self.text.clone()
    }

    fn undo(&mut self) -> String {
        if let Some(t) = self.done.pop() {
            self.undone.push(std::mem::replace(&mut self.text, t));
        }
        self.text.clone()
    }

    fn get_state(&self) -> (usize, usize) {
        (self.done.len(), self.undone.len())
    }

    fn clear(&mut self) {
        self.text.clear();
    }
}
"""


def test_operations_replay_passes_and_maps_op_names_to_snake_case():
    """`getState` (the shared cases' spelling) calls `get_state`; `&self` and
    `&mut self` methods both dispatch, and `()` encodes as null like Python's None."""
    res = rust_results(ops_payload(EDITOR, [
        ops_case(0, [[], ("append", ["ab"]), ("append", ["cd"]), ("undo", []), ("getState", []),
                     ("clear", []), ("getState", [])],
                 ["ab", "abcd", "ab", [1, 1], None, [1, 1]]),
        ops_case(1, [[], ("undo", [])], [""]),
    ]))
    assert [r["status"] for r in res] == ["passed", "passed"]
    assert res[0]["output"] == '[null,"ab","abcd","ab",[1,1],null,[1,1]]'


def test_operations_wrong_answer_shows_every_call_result():
    res = rust_results(ops_payload(EDITOR, [ops_case(0, [[], ("append", ["x"])], ["y"])]))
    assert res[0]["status"] == "wrong_answer"
    assert res[0]["output"] == '[null,"x"]'


PEEKER = """struct Peeker { vals: Vec<i32>, i: usize }

impl Peeker {
    fn new(nums: IntIter) -> Self {
        Peeker { vals: nums.collect(), i: 0 }
    }
    fn next(&mut self) -> i32 {
        let v = self.vals[self.i];
        self.i += 1;
        v
    }
    fn has_next(&self) -> bool {
        self.i < self.vals.len()
    }
}
"""


def test_operations_iterator_constructor_arg_is_an_int_iter():
    """A declared `"Iterator"` param is Rust's `IntIter` (prelude.rs): a flat
    `[i32]` on the wire, a real `Iterator<Item = i32>` in `new`."""
    params = [{"name": "nums", "type": "Iterator"}]
    res = rust_results(ops_payload(PEEKER, [
        ops_case(0, [[[1, 2, 3]], ("next", []), ("hasNext", []), ("next", []), ("next", []),
                     ("hasNext", [])],
                 [1, True, 2, 3, False], class_name="Peeker"),
        ops_case(1, [[[]], ("hasNext", [])], [False], class_name="Peeker"),
    ], class_name="Peeker", params=params))
    assert [r["status"] for r in res] == ["passed", "passed"], res
    assert res[0]["output"] == "[null,1,true,2,3,false]"


def test_iterator_param_works_in_function_mode_too():
    """`IntIter` is just a `FromJson` type, so any declared "Iterator" param gets it."""
    res = rust_results(payload("fn f(nums: IntIter) -> i32 { nums.sum() }",
                               [case(0, [[1, 2, 3]], 6)], params=[{"name": "nums", "type": "Iterator"}]))
    assert res[0]["status"] == "passed", res[0]


def test_int_iter_cannot_be_asked_its_length():
    """Like the Python `Iterator`: a one-pass stream with no length."""
    res = rust_results(payload("fn f(nums: IntIter) -> usize { nums.len() }",
                               [case(0, [[1, 2, 3]], 3)], params=[{"name": "nums", "type": "Iterator"}]))
    assert res[0]["status"] == "runtime_error" and "len" in res[0]["error"], res[0]


def test_impl_iterator_constructor_gets_an_int_iter_hint():
    code = "struct P;\nimpl P { fn new(nums: impl Iterator<Item = i32>) -> Self { P } fn n(&self) -> i32 { 0 } }\n"
    res = rust_results(ops_payload(code, [ops_case(0, [[[1]], ("n", [])], [0], class_name="P")],
                                   class_name="P", params=[{"name": "nums", "type": "Iterator"}]))
    assert res[0]["status"] == "runtime_error", res[0]
    assert "IntIter" in res[0]["error"], res[0]["error"]


def test_operations_iterator_constructor_arg_rejects_non_integers():
    res = rust_results(ops_payload(PEEKER, [
        ops_case(0, [[["a"]], ("hasNext", [])], [False], class_name="Peeker"),
    ], class_name="Peeker", params=[{"name": "nums", "type": "Iterator"}]))
    assert res[0]["status"] == "runtime_error", res


def test_operations_constructor_args_and_node_structs():
    """`params` describe the constructor, as in Python: its arguments are the first
    list, typed by `new`'s signature, and a declared node type gets its struct."""
    code = """struct Walker { cur: Option<Box<ListNode>>, step: i64 }

impl Walker {
    fn new(head: Option<Box<ListNode>>, step: i64) -> Self { Walker { cur: head, step } }

    fn next(&mut self) -> Option<i64> {
        let node = self.cur.take()?;
        self.cur = node.next;
        Some(node.val as i64 * self.step)
    }
}
"""
    res = rust_results(ops_payload(code, [
        ops_case(0, [[[1, 2], 10], ("next", []), ("next", []), ("next", [])], [10, 20, None],
                 class_name="Walker"),
    ], class_name="Walker", params=[{"name": "head", "type": "ListNode"}, {"name": "step", "type": "i64"}]))
    assert res[0]["status"] == "passed", res[0]


def test_operations_method_arguments_are_decoded_by_signature():
    code = """use std::collections::HashMap;

struct Book { prices: HashMap<String, f64> }

impl Book {
    fn new() -> Self { Book { prices: HashMap::new() } }
    fn set(&mut self, items: Vec<(String, f64)>, scale: Option<f64>) -> usize {
        for (k, v) in items { self.prices.insert(k, v * scale.unwrap_or(1.0)); }
        self.prices.len()
    }
    fn price(&self, k: String) -> Option<f64> { self.prices.get(&k).copied() }
}
"""
    res = rust_results(ops_payload(code, [ops_case(0, [
        [], ("set", [[["a", 1.5], ["b", 2]], None]), ("set", [[["a", 1]], 3]),
        ("price", ["a"]), ("price", ["z"]),
    ], [2, 2, 3.0, None], class_name="Book")], class_name="Book"))
    assert res[0]["status"] == "passed", res[0]


def test_operations_argument_count_mismatch_is_a_runtime_error():
    res = rust_results(ops_payload(EDITOR, [ops_case(0, [[], ("append", ["a", "b"])], ["a"])]))
    assert res[0]["status"] == "runtime_error"
    assert res[0]["error"] == ("could not decode the test case: `append`: takes 1 argument "
                               "but the test case passes 2")


def test_operations_panic_fails_only_its_case():
    code = EDITOR.replace("fn undo(&mut self) -> String {",
                          'fn undo(&mut self) -> String {\n        assert!(!self.done.is_empty(), "nothing to undo");')
    res = rust_results(ops_payload(code, [
        ops_case(0, [[], ("undo", [])], [""]),
        ops_case(1, [[], ("append", ["a"])], ["a"]),
    ]))
    assert [r["status"] for r in res] == ["runtime_error", "passed"]
    assert "nothing to undo" in res[0]["error"]


def test_operations_method_named_like_the_case_gets_a_snake_case_hint():
    res = rust_results(ops_payload(EDITOR.replace("fn get_state", "fn getState"),
                                   [ops_case(0, [[], ("getState", [])], [[0, 0]])]))
    assert res[0]["status"] == "runtime_error"
    assert "Hint: the test cases call `getState`, which the judge calls as the Rust method " \
           "`get_state`" in res[0]["error"]


def test_operations_missing_constructor_gets_a_hint():
    res = rust_results(ops_payload(EDITOR.replace("fn new()", "fn create()"),
                                   [ops_case(0, [[], ("undo", [])], [""])]))
    assert "Hint: the judge builds the object with `Editor::new(...)`" in res[0]["error"]


def test_operations_method_returning_a_reference_gets_a_signature_hint():
    code = EDITOR.replace("fn undo(&mut self) -> String {", "fn peek(&self) -> &str { &self.text }\n\n    fn undo(&mut self) -> String {")
    res = rust_results(ops_payload(code, [ops_case(0, [[], ("peek", [])], [""])]))
    assert res[0]["status"] == "runtime_error"
    assert "Hint: the judge calls each method with arguments decoded" in res[0]["error"]


def test_operations_only_dispatch_ops_the_cases_use():
    """A Run judges only the samples, so a method no case calls needn't exist yet."""
    res = rust_results(ops_payload(EDITOR, [ops_case(0, [[], ("append", ["a"])], ["a"])]))
    assert res[0]["status"] == "passed"


METHOD_NAMES = json.loads((pathlib.Path(__file__).parent / "rust_method_names.json").read_text())["names"]


def test_operations_method_names_follow_the_shared_table():
    """harness.rs `method_name` against judge/tests/rust_method_names.json, the table
    backend tests hold `app/sandbox.py`'s `rust_method_name` to as well. Each method
    returns its own name, so the replay shows which one each op reached."""
    mapped = {op: m for op, m in METHOD_NAMES.items() if m is not None}
    body = "\n".join(f'    fn {m}(&self) -> String {{ "{m}".to_string() }}' for m in mapped.values())
    code = f"struct Names;\n\nimpl Names {{\n    fn new() -> Self {{ Names }}\n{body}\n}}\n"
    res = rust_results(ops_payload(code, [ops_case(0, [[]] + [(op, []) for op in mapped], list(mapped.values()),
                                                   class_name="Names")], class_name="Names"))
    assert res[0]["status"] == "passed", res[0]


@pytest.mark.parametrize("op", sorted(op for op, m in METHOD_NAMES.items() if m is None))
def test_operations_refused_op_names_are_a_judge_fault(op):
    """An op the table refuses never reaches generated source. Seed validation
    refuses it first; the harness's backstop is a judge fault (exit 3, no results)."""
    pl = ops_payload(EDITOR, [ops_case(0, [[], (op, [])], [None])])
    proc = run_rust_container(json.dumps(pl))
    assert proc.returncode == 3 and proc.stdout == "", proc.stderr


def test_operations_duplicate_new_gets_no_node_struct_hint():
    """With no node struct declared, a duplicate `new` is the user's own, and the
    node-struct hint would send them looking for a struct that doesn't exist."""
    code = EDITOR + "\nimpl Editor {\n    fn new() -> Self { todo!() }\n}\n"
    res = rust_results(ops_payload(code, [ops_case(0, [[], ("undo", [])], [""])]))
    assert "duplicate definitions with name `new`" in res[0]["error"]
    assert "node struct" not in res[0]["error"]


def test_operations_hint_covers_enum_classes_and_keyword_methods():
    code = """enum Machine { On }

impl Machine {
    fn new() -> Self { Machine::On }
    fn Type(&self) -> i32 { 1 }
}
"""
    res = rust_results(ops_payload(code, [ops_case(0, [[], ("Type", [])], [1], class_name="Machine")],
                                   class_name="Machine"))
    assert "the test cases call `Type`, which the judge calls as the Rust method `type`" in res[0]["error"]


def test_operations_object_is_never_dropped_on_an_error():
    """`ops::replay` never drops the object, error paths included (the same reason
    `ret` leaks return values). A `Drop` that panics makes a drop visible: it would
    replace the argument-count error with a panic."""
    code = """struct Loud;

impl Drop for Loud {
    fn drop(&mut self) { panic!("dropped"); }
}

impl Loud {
    fn new() -> Self { Loud }
    fn size(&self, _x: i32) -> i32 { 0 }
}
"""
    res = rust_results(ops_payload(code, [
        ops_case(0, [[], ("size", [])], [0], class_name="Loud"),
        ops_case(1, [[], ("size", [1])], [0], class_name="Loud"),
    ], class_name="Loud"))
    assert res[0]["status"] == "runtime_error"
    assert res[0]["error"] == ("could not decode the test case: `size`: takes 1 argument "
                               "but the test case passes 0")
    assert res[1]["status"] == "passed"  # the success path doesn't drop it either


def test_operations_case_shape_errors_are_decode_errors():
    res = rust_results(ops_payload(EDITOR, [case(0, [["Editor", "undo"], [[]]], [None, ""])]))
    assert res[0]["status"] == "runtime_error"
    assert "one argument list per op" in res[0]["error"]


# --- custom validators (DESIGN.md §5.4; docs/adr/0007) -----------------------
# A problem's Rust validator is compiled into its own program, sealed in memory,
# and run by the harness once per case after the case's process has ended.

SAME_MULTISET = """use shikomi_prelude::Json;

fn validate(actual: &Json, _expected: &Json, args: &Json, _probe_results: &[Json]) -> bool {
    let mut a: Vec<i64> = match actual.decode() {
        Ok(v) => v,
        Err(_) => return false,
    };
    let mut b: Vec<i64> = args[0].decode().unwrap();
    a.sort();
    b.sort();
    a == b
}
"""
SORTS = "fn f(mut v: Vec<i64>) -> Vec<i64> { v.sort(); v }"


def validator_payload(code, validator=SAME_MULTISET, cases=None, **kw):
    return payload(code, cases or [case(0, [[3, 1, 2]], None), case(1, [[5, 5, 4]], None)],
                   comparison={"mode": "custom_validator", "validator_code": validator}, **kw)


def test_custom_validator_passes_and_fails():
    assert [r["status"] for r in rust_results(validator_payload(SORTS))] == ["passed", "passed"]
    wrong = "fn f(v: Vec<i64>) -> Vec<i64> { vec![0; v.len()] }"
    res = rust_results(validator_payload(wrong))
    assert [r["status"] for r in res] == ["wrong_answer", "wrong_answer"]
    assert res[0]["output"] == "[0,0,0]"


def test_a_validator_that_panics_is_judge_error_without_its_message():
    """A panic's message can quote `expected` (`assert_eq!(actual, expected)`), so
    the user gets a fixed line and the message goes only to the harness's stderr."""
    boom = SAME_MULTISET.replace("a == b", 'panic!("secret {:?}", _expected)')
    proc = run_rust_container(json.dumps(validator_payload(SORTS, boom)))
    res = json.loads(proc.stdout)["results"]
    assert {r["status"] for r in res} == {"judge_error"}
    assert "custom validator failed" in res[0]["error"] and "secret" not in res[0]["error"]
    assert "panicked" in proc.stderr and "secret" in proc.stderr


def test_a_validator_that_doesnt_compile_is_one_judge_error_without_its_diagnostics():
    """The author's code: the user is told it's a problem bug, and rustc's output
    (which would show the validator's source) goes only to the judge's log."""
    proc = run_rust_container(json.dumps(validator_payload(SORTS, "fn validate() -> bool { nope }")))
    res = json.loads(proc.stdout)["results"]
    assert len(res) == 1 and res[0]["status"] == "judge_error"
    assert "failed to compile" in res[0]["error"] and "nope" not in res[0]["error"]
    assert "nope" in proc.stderr


def test_a_submission_that_doesnt_compile_is_reported_before_the_validator():
    res = rust_results(validator_payload("fn f(v: Vec<i64>) -> Vec<i64> { oops }"))
    assert len(res) == 1 and res[0]["status"] == "runtime_error"
    assert "Compile error" in res[0]["error"]


def test_a_validator_that_hangs_uses_up_the_case_time_limit():
    hangs = SAME_MULTISET.replace("a == b", "loop {}")
    res = rust_results(validator_payload(SORTS, hangs, time_limit_ms=300))
    assert {r["status"] for r in res} == {"time_limit_exceeded"}


def test_a_validator_can_print_and_still_give_its_verdict():
    """The verdict is stdout's last line, so a debugging println! doesn't break it."""
    chatty = SAME_MULTISET.replace("    a == b", '    println!("true");\n    println!("{:?}", a);\n    a == b')
    wrong = "fn f(v: Vec<i64>) -> Vec<i64> { vec![0; v.len()] }"
    assert rust_results(validator_payload(wrong, chatty))[0]["status"] == "wrong_answer"
    # Far more than the harness keeps: the tail is kept, so the verdict survives.
    flood = SAME_MULTISET.replace("    a == b", '    for _ in 0..20000 { println!("debug line"); }\n    a == b')
    assert [r["status"] for r in rust_results(validator_payload(SORTS, flood))] == ["passed", "passed"]


def test_a_submission_cannot_read_the_validators_source():
    """validator.rs (and rustc's output, which quotes it) is deleted before any case
    runs, so a submission can't print the author's code back out of /tmp."""
    peek = r'''
fn f(v: Vec<i64>) -> Vec<i64> {
    let mut seen = String::new();
    for p in ["/tmp/judge/validator.rs", "/tmp/judge/validator.rustc.stderr", "/tmp/judge/validator"] {
        if std::fs::metadata(p).is_ok() { seen.push_str(p); }
    }
    print!("{}", seen);
    v
}
'''
    res = rust_results(validator_payload(peek))
    assert [r["stdout"] for r in res] == ["", ""]


def test_the_validator_runs_from_an_execute_only_file():
    """The memfd is mode 0111, which makes the kernel mark the validator
    non-dumpable from exec, before its own prctl: it can't read its own program."""
    self_read = SAME_MULTISET.replace("    a == b", '    std::fs::read("/proc/self/exe").is_err() && a == b')
    assert [r["status"] for r in rust_results(validator_payload(SORTS, self_read))] == ["passed", "passed"]


def test_a_validator_gets_time_to_start_even_when_the_submission_used_the_limit():
    """A submission that finishes just inside its limit isn't timed out by the
    validator's own startup: the validator is topped up to a minimum budget."""
    slow = """fn f(mut v: Vec<i64>) -> Vec<i64> {
    let t = std::time::Instant::now();
    while t.elapsed() < std::time::Duration::from_millis(289) {}
    v.sort();
    v
}"""
    # 5ms of validator work: more than the ~1ms the submission leaves, less than the top-up.
    working = SAME_MULTISET.replace("    a == b", "    std::thread::sleep(std::time::Duration::from_millis(5));\n    a == b")
    res = rust_results(validator_payload(slow, working, time_limit_ms=290))
    assert [r["status"] for r in res] == ["passed", "passed"]


def test_a_submission_cannot_replace_the_validator():
    """The validator binary lives in a sealed memfd, not in the writable /tmp. A
    submission that plants an always-`true` program at /tmp/judge/validator, and
    tries to write through the harness's fds, still gets its wrong answer judged."""
    attack = r'''
fn f(v: Vec<i64>) -> Vec<i64> {
    use std::os::unix::fs::PermissionsExt;
    let fake = "/tmp/judge/validator";
    let _ = std::fs::write(fake, "#!/bin/sh\necho true\n");
    let _ = std::fs::set_permissions(fake, std::fs::Permissions::from_mode(0o755));
    for fd in 0..64 {
        let _ = std::fs::write(format!("/proc/1/fd/{}", fd), "#!/bin/sh\necho true\n");
    }
    vec![0; v.len()]
}
'''
    res = rust_results(validator_payload(attack))
    assert [r["status"] for r in res] == ["wrong_answer", "wrong_answer"]


def test_an_operations_validator_checks_the_replay():
    """The group-B shape: replay the ops against a model. Here, every `get` must
    return the value last `put` for its key."""
    store = """use std::collections::HashMap;
struct Store { m: HashMap<i64, i64> }
impl Store {
    fn new() -> Self { Store { m: HashMap::new() } }
    fn put(&mut self, k: i64, v: i64) { self.m.insert(k, v); }
    fn get(&self, k: i64) -> i64 { *self.m.get(&k).unwrap_or(&-1) }
}
"""
    model = """use shikomi_prelude::Json;
use std::collections::HashMap;

fn validate(actual: &Json, _expected: &Json, args: &Json, probe_results: &[Json]) -> bool {
    if !probe_results.is_empty() { return false; }
    let mut m = HashMap::new();
    for (i, op) in args[0].as_arr().iter().enumerate().skip(1) {
        let a = &args[1][i];
        match op.as_str() {
            Some("put") => { m.insert(a[0].as_i64(), a[1].as_i64()); }
            Some("get") => {
                let want = m.get(&a[0].as_i64()).copied().flatten().unwrap_or(-1);
                if actual[i].as_i64() != Some(want) { return false; }
            }
            _ => return false,
        }
    }
    true
}
"""
    cases = [ops_case(0, [[], ("put", [1, 7]), ("get", [1]), ("get", [2])], [None, None, None], class_name="Store")]
    pl = ops_payload(store, cases, class_name="Store",
                     comparison={"mode": "custom_validator", "validator_code": model})
    assert rust_results(pl)[0]["status"] == "passed"
    broken = store.replace("*self.m.get(&k).unwrap_or(&-1)", "0")
    pl["user_code"] = broken
    assert rust_results(pl)[0]["status"] == "wrong_answer"


# --- probes (docs/adr/0007; prelude.rs `ops::replay`) -------------------------------
# Extra calls the judge makes on the instance after the replay, whose results reach
# the validator as `probe_results`.

CODEC = """struct Codec;
impl Codec {
    fn new() -> Self { Codec }
    fn encode(&self, words: Vec<String>) -> String {
        words.iter().map(|w| format!("{}#{}", w.len(), w)).collect()
    }
    fn decode(&self, s: String) -> Vec<String> {
        let (mut out, mut i, b) = (Vec::new(), 0, s.as_bytes());
        while i < b.len() {
            let j = i + b[i..].iter().position(|&c| c == b'#').unwrap();
            let n: usize = s[i..j].parse().unwrap();
            out.push(s[j + 1..j + 1 + n].to_string());
            i = j + 1 + n;
        }
        out
    }
}
"""
# decode(encode(words)) == words, whatever `encode` produced.
ROUND_TRIP = """use shikomi_prelude::Json;

fn validate(_actual: &Json, _expected: &Json, args: &Json, probe_results: &[Json]) -> bool {
    probe_results.len() == 1 && probe_results[0] == args[1][1][0]
}
"""


def codec_payload(code, probes=None, validator=ROUND_TRIP):
    probes = probes if probes is not None else [{"op": "decode", "args": [None], "refs": {"0": 1}}]
    cases = [{**ops_case(0, [[], ("encode", [["a#b", "", "long word"]])], [None], class_name="Codec"),
              "probes": probes}]
    return ops_payload(code, cases, class_name="Codec",
                       comparison={"mode": "custom_validator", "validator_code": validator})


def test_a_probe_round_trips_an_ops_result_into_a_method_no_case_calls():
    """`decode` appears only in a probe, so it needs its own dispatch arm, and its
    argument is filled from op 1's result (`refs`)."""
    res = rust_results(codec_payload(CODEC))
    assert res[0]["status"] == "passed", res[0]
    # The output is the case's own ops only; the probe's result isn't shown.
    assert res[0]["output"] == '[null,"3#a#b0#9#long word"]'
    broken = CODEC.replace("out.push(s[j + 1..j + 1 + n].to_string());",
                           "out.push(s[j + 1..j + 1 + n].to_uppercase());")
    assert rust_results(codec_payload(broken))[0]["status"] == "wrong_answer"


def test_a_repeated_probe_gives_one_result_per_call():
    counter = """struct Counter { n: i64 }
impl Counter {
    fn new() -> Self { Counter { n: 0 } }
    fn bump(&mut self, by: i64) -> i64 { self.n += by; self.n }
}
"""
    counts_up = """use shikomi_prelude::Json;

fn validate(_actual: &Json, _expected: &Json, _args: &Json, probe_results: &[Json]) -> bool {
    let got: Vec<i64> = probe_results.iter().filter_map(Json::as_i64).collect();
    got == (1..=500).map(|i| 5 + 2 * i).collect::<Vec<_>>()
}
"""
    cases = [{**ops_case(0, [[], ("bump", [5])], [None], class_name="Counter"),
              "probes": [{"op": "bump", "args": [2], "repeat": 500}]}]
    pl = ops_payload(counter, cases, class_name="Counter",
                     comparison={"mode": "custom_validator", "validator_code": counts_up})
    assert rust_results(pl)[0]["status"] == "passed"


def test_a_panic_in_a_probe_says_the_judge_made_that_call():
    res = rust_results(codec_payload(CODEC.replace("let (mut out", 'panic!("no"); let (mut out')))
    assert res[0]["status"] == "runtime_error"
    assert res[0]["error"].startswith(
        "In a call the judge added after your operations to check the result (decode(), call 1 of 1):\n"
        "panicked: no"), res[0]["error"]


def test_a_probe_argument_that_does_not_fit_names_the_probe():
    res = rust_results(codec_payload(CODEC, probes=[{"op": "decode", "args": [7]}]))
    assert res[0]["status"] == "runtime_error"
    assert "decode(), call 1 of 1" in res[0]["error"] and "argument 1" in res[0]["error"]


def test_forged_probe_results_of_the_wrong_length_are_the_submissions_fault():
    """The result file is the submission's to write. One that skips the probes
    and claims none were made is refused against the harness's own copy of them,
    not handed to the validator (whose failure would read as the author's bug)."""
    forger = CODEC.replace("fn new() -> Self { Codec }", """fn new() -> Self {
        let path = std::env::var("SHIKOMI_RESULT").unwrap();
        std::fs::write(path, r#"{"ok":[null,"x"],"probe_results":[]}"#).unwrap();
        std::process::exit(0);
    }""")
    res = rust_results(codec_payload(forger))
    assert res[0]["status"] == "runtime_error"
    assert "didn't account for every check" in res[0]["error"]


def test_a_malformed_probe_is_one_judge_error_before_any_case_runs():
    proc = run_rust_container(json.dumps(codec_payload(CODEC, probes=[{"op": "decode", "args": [None],
                                                                       "refs": {"0": 9}}])))
    res = json.loads(proc.stdout)["results"]
    assert len(res) == 1 and res[0]["status"] == "judge_error"
    assert "probes are malformed" in res[0]["error"] and "refs" not in res[0]["error"]
    assert "a ref names a missing argument or op" in proc.stderr


def test_probes_outside_operations_mode_are_refused():
    cases = [{**case(0, [[3, 1, 2]], None), "probes": [{"op": "x"}]}]
    res = rust_results(validator_payload(SORTS, cases=cases))
    assert len(res) == 1 and res[0]["status"] == "judge_error"
    assert "kind 'operations'" in res[0]["error"]


def test_probe_calls_count_against_the_case_time_limit():
    slow = CODEC.replace("fn decode(&self, s: String) -> Vec<String> {",
                         "fn decode(&self, s: String) -> Vec<String> { loop {}")
    res = rust_results({**codec_payload(slow), "time_limit_ms": 300})
    assert res[0]["status"] == "time_limit_exceeded"


# --- the prelude's Rng -------------------------------------------------------------

def rng_results(body, ret="Vec<i64>"):
    code = f"use shikomi_prelude::Rng;\nfn f(_x: i32) -> {ret} {{\n{body}\n}}\n"
    res = rust_results(payload(code, [case(0, [0], None)], params=[{"name": "x", "type": "i32"}]))
    assert res[0]["output"] is not None, res[0]
    return json.loads(res[0]["output"])


def test_a_seeded_rng_repeats_its_sequence_and_new_ones_differ():
    out = rng_results("""
        let (mut a, mut b) = (Rng::seeded(42), Rng::seeded(42));
        let same = (0..100).all(|_| a.next_u64() == b.next_u64());
        let (mut c, mut d) = (Rng::new(), Rng::new());
        let differ = (0..4).any(|_| c.next_u64() != d.next_u64());
        vec![same as i64, differ as i64, (Rng::seeded(0).next_u64() != 0) as i64]""")
    assert out == [1, 1, 1]


def test_gen_range_stays_in_bounds_including_the_extremes():
    out = rng_results("""
        let mut r = Rng::seeded(7);
        let mut ok = true;
        for _ in 0..10000 {
            let x = r.gen_range(-3..4); ok &= (-3..4).contains(&x);
            let y = r.gen_range(10u8..=255); ok &= y >= 10;
            let z: f64 = r.gen_range(-1.0..1.0); ok &= (-1.0..1.0).contains(&z);
            let u = r.gen_f64(); ok &= (0.0..1.0).contains(&u);
        }
        // Whole-type ranges, where the span doesn't fit the type itself.
        let _ = r.gen_range(i64::MIN..=i64::MAX);
        let _ = r.gen_range(u64::MIN..=u64::MAX);
        let _ = r.gen_range(i64::MIN..i64::MAX);
        vec![ok as i64, r.gen_range(5..=5), r.gen_range(9..10)]""")
    assert out == [1, 5, 9]


def test_gen_range_is_uniform():
    """60000 draws over 3 buckets: each within ~5 standard deviations of 20000.
    (Plain `% 3` of a u64 would pass this too; the bias it carries is ~1e-19. The
    point is that the rejection loop doesn't skew or stall anything.)"""
    counts = rng_results("""
        let mut r = Rng::new();
        let mut c = vec![0i64; 3];
        for _ in 0..60000 { c[r.gen_range(0..3usize)] += 1; }
        c""")
    assert all(abs(n - 20000) < 600 for n in counts), counts


def test_shuffle_permutes_and_choose_picks_an_element():
    out = rng_results("""
        let mut r = Rng::new();
        let mut v: Vec<i64> = (0..50).collect();
        r.shuffle(&mut v);
        let moved = v.iter().enumerate().any(|(i, &x)| i as i64 != x);
        let mut sorted = v.clone(); sorted.sort();
        let empty: [i64; 0] = [];
        vec![(sorted == (0..50).collect::<Vec<_>>()) as i64, moved as i64,
             *r.choose(&[4, 4, 4]).unwrap(), r.choose(&empty).is_none() as i64]""")
    assert out == [1, 1, 4, 1]


def test_an_empty_range_panics():
    code = "use shikomi_prelude::Rng;\nfn f(_x: i32) -> i32 { Rng::new().gen_range(3..3) }\n"
    res = rust_results(payload(code, [case(0, [0], None)], params=[{"name": "x", "type": "i32"}]))
    assert res[0]["status"] == "runtime_error" and "empty range 3..3" in res[0]["error"]
