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
    res = rust_results(payload("struct C;", [case(3, [[], []], [])], kind="operations"))
    assert res == [{
        "test_case_id": 3, "status": "runtime_error", "runtime_ms": 0.0, "output": None,
        "stdout": "", "error": "kind 'operations' is not supported by the Rust harness",
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
