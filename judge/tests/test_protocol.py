"""Harness protocol tests (DESIGN.md §10.1) — run harness.py as a subprocess and
assert its result JSON. No Docker required; this is the executable spec of §5.3.
"""
import json
import pathlib
import subprocess
import sys

HARNESS = pathlib.Path(__file__).resolve().parents[1] / "harness.py"
TRUNC_MAX = 4096 + len("…(truncated)")


def payload(user_code, test_cases, function_name="f", comparison=None, time_limit_ms=2000,
            params=None, return_type=""):
    return {
        "function_name": function_name,
        "user_code": user_code,
        "test_cases": test_cases,
        "comparison": comparison or {"mode": "exact"},
        "time_limit_ms": time_limit_ms,
        "params": params or [],
        "return_type": return_type,
    }


def case(i, inp, expected):
    return {"id": i, "input": inp, "expected": expected}


def run_harness(pl, raw=None, timeout=15):
    stdin = raw if raw is not None else json.dumps(pl)
    return subprocess.run(
        [sys.executable, str(HARNESS)],
        input=stdin, capture_output=True, text=True, timeout=timeout,
    )


def results(pl, **kw):
    proc = run_harness(pl, **kw)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["results"]


PAIR_SUM = (
    "def f(nums, target):\n"
    "    seen = {}\n"
    "    for i, n in enumerate(nums):\n"
    "        if target - n in seen:\n"
    "            return [seen[target - n], i]\n"
    "        seen[n] = i\n"
)


def test_correct_solution_all_pass():
    res = results(payload(PAIR_SUM, [case(0, [[4, 9, 1, 6], 7], [2, 3]),
                                     case(1, [[5, 5], 10], [0, 1])]))
    assert [r["status"] for r in res] == ["passed", "passed"]
    assert all(r["error"] is None for r in res)


def test_wrong_answer_reports_output():
    res = results(payload("def f(x):\n    return x + 1", [case(0, [1], 999)]))
    assert res[0]["status"] == "wrong_answer"
    assert res[0]["output"] == "2"


def test_runtime_error_user_frames_only():
    res = results(payload("def f(x):\n    return x[10]", [case(0, [[1, 2]], 0)]))
    assert res[0]["status"] == "runtime_error"
    assert "IndexError" in res[0]["error"]
    assert "<user_code>" in res[0]["error"]      # user frame is shown
    assert "harness.py" not in res[0]["error"]   # harness frames are stripped


def test_tle_busy_loop():
    res = results(payload("def f(x):\n    while True:\n        pass",
                          [case(0, [1], 1)], time_limit_ms=200))
    assert res[0]["status"] == "time_limit_exceeded"


def test_tle_sleep():
    res = results(payload("import time\ndef f(x):\n    time.sleep(10)",
                          [case(0, [1], 1)], time_limit_ms=200))
    assert res[0]["status"] == "time_limit_exceeded"


def test_stdout_captured_and_truncated():
    res = results(payload("def f(x):\n    print('X' * 10000)\n    return x",
                          [case(0, [5], 5)]))
    assert res[0]["status"] == "passed"          # protocol not corrupted by prints
    assert res[0]["stdout"].startswith("X")
    assert len(res[0]["stdout"]) <= TRUNC_MAX


def test_missing_function():
    res = results(payload("def g(x):\n    return x", [case(0, [1], 1)], function_name="f"))
    assert res[0]["status"] == "runtime_error"
    assert "Function 'f' not found" in res[0]["error"]


def test_syntax_error():
    res = results(payload("def f(x)\n    return x", [case(0, [1], 1)]))
    assert res[0]["status"] == "runtime_error"
    assert "SyntaxError" in res[0]["error"]


def test_top_level_import_error():
    res = results(payload("import definitely_not_real_xyz\ndef f(x):\n    return x",
                          [case(0, [1], 1)]))
    assert res[0]["status"] == "runtime_error"
    assert "ModuleNotFoundError" in res[0]["error"] or "ImportError" in res[0]["error"]


def test_input_mutation_isolated_between_cases():
    code = "def f(nums):\n    nums.append(99)\n    return len(nums)"
    res = results(payload(code, [case(0, [[1, 2]], 3), case(1, [[1, 2, 3]], 4)]))
    assert [r["status"] for r in res] == ["passed", "passed"]


def test_runs_all_cases_by_default():
    res = results(payload("def f(x):\n    return x",
                          [case(0, [1], 1), case(1, [2], 999), case(2, [3], 3)]))
    assert [r["status"] for r in res] == ["passed", "wrong_answer", "passed"]


def test_runs_remaining_cases_after_runtime_error():
    code = "def f(x):\n    if x == 2:\n        raise ValueError('boom')\n    return x"
    res = results(payload(code, [case(0, [1], 1), case(1, [2], 2), case(2, [3], 3)]))
    assert [r["status"] for r in res] == ["passed", "runtime_error", "passed"]


def test_stop_on_first_failure_flag_short_circuits():
    pl = payload("def f(x):\n    return x",
                 [case(0, [1], 1), case(1, [2], 999), case(2, [3], 3)])
    pl["stop_on_first_failure"] = True
    res = results(pl)
    assert [r["status"] for r in res] == ["passed", "wrong_answer"]
    assert len(res) == 2  # the third case never ran


def test_output_repr_truncated():
    big = "y" * 10000
    res = results(payload("def f(x):\n    return 'y' * 10000", [case(0, [0], big)]))
    assert res[0]["status"] == "passed"
    assert len(res[0]["output"]) <= TRUNC_MAX


def test_malformed_payload_nonzero_exit():
    proc = run_harness(None, raw="this is not json")
    assert proc.returncode != 0


# --- comparison modes (DESIGN.md §5.4) -------------------------------------

def test_unordered_mode():
    code = "def f(x):\n    return [3, 2, 1]"
    assert results(payload(code, [case(0, [0], [1, 2, 3])],
                           comparison={"mode": "unordered"}))[0]["status"] == "passed"
    assert results(payload(code, [case(0, [0], [1, 2, 3])],
                           comparison={"mode": "exact"}))[0]["status"] == "wrong_answer"


def test_unordered_mode_nested_lists():
    # A "list of groups" answer: each element is itself a list, so it's
    # unhashable at the top level until `_hashable_key` converts it to a tuple
    # for the Counter fast path. Groups may appear in any order; the elements
    # *within* a group must still match in the same order (same result as the
    # O(n^2) `==`-based fallback — the fast path only speeds it up).
    code = "def f(x):\n    return [['b', 'a'], ['c']]"
    assert results(payload(code, [case(0, [0], [['c'], ['b', 'a']])],
                           comparison={"mode": "unordered"}))[0]["status"] == "passed"
    assert results(payload(code, [case(0, [0], [['c'], ['a', 'b']])],
                           comparison={"mode": "unordered"}))[0]["status"] == "wrong_answer"


def test_unordered_mode_large_list_is_fast():
    # An O(n^2) `_multiset_equal` (remove-by-value from a shrinking list) takes
    # tens of seconds on a reversed list this size, which
    # `compare()`'s untimed-but-wall-clock-bounded budget (worker/judging.py)
    # can't absorb. The Counter-based fast path makes this O(n); if the
    # regression comes back this test hits `run_harness`'s subprocess timeout
    # instead of merely running slow.
    n = 50_000
    code = "def f(x):\n    return list(reversed(x))"
    values = list(range(n))
    proc = run_harness(payload(code, [case(0, [values], values)],
                               comparison={"mode": "unordered"}), timeout=10)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["results"][0]["status"] == "passed"


def test_float_tolerance_nested():
    code = "def f(x):\n    return [[1.0000001, 2.0], [3.0]]"
    expected = [[1.0, 2.0], [3.0]]
    assert results(payload(code, [case(0, [0], expected)],
                           comparison={"mode": "float_tolerance", "epsilon": 1e-4}))[0]["status"] == "passed"
    assert results(payload(code, [case(0, [0], expected)],
                           comparison={"mode": "float_tolerance", "epsilon": 1e-9}))[0]["status"] == "wrong_answer"


def test_any_of_mode():
    code = "def f(x):\n    return 7"
    assert results(payload(code, [case(0, [0], [5, 7, 9])],
                           comparison={"mode": "any_of"}))[0]["status"] == "passed"
    assert results(payload(code, [case(0, [0], [5, 9])],
                           comparison={"mode": "any_of"}))[0]["status"] == "wrong_answer"


# --- custom_validator mode ---------------------------------------------------
# `expected` is unused by these particular checks (the property is checked
# against `args` — the original, pre-call input — not a fixed answer) — left `None` in each case, same as any
# other comparison mode would leave it unused if the check doesn't need it.

SAME_MULTISET = (
    "def validate(actual, expected, args, instance=None):\n"
    "    return sorted(actual) == sorted(args[0])\n"
)


def test_custom_validator_correct():
    code = "def f(s):\n    return sorted(s)"
    pl = payload(code, [case(0, ["cab"], None)],
                 comparison={"mode": "custom_validator", "validator_code": SAME_MULTISET})
    res = results(pl)
    assert res[0]["status"] == "passed"


def test_custom_validator_wrong():
    code = "def f(s):\n    return ['z']"
    pl = payload(code, [case(0, ["cab"], None)],
                 comparison={"mode": "custom_validator", "validator_code": SAME_MULTISET})
    res = results(pl)
    assert res[0]["status"] == "wrong_answer"


def test_custom_validator_sees_the_input_before_the_submission_mutated_it():
    """`args` is the test case's input as it was *before* the call. A
    submission receives its own copy, so overwriting its input can't also
    rewrite what the validator compares against — otherwise this "return
    whatever I just wrote into the input" cheat satisfies any multiset check
    against the input, so any validator comparing output to input would accept
    a fabricated answer."""
    code = "def f(nums):\n    nums[:] = [0] * len(nums)\n    return nums"
    pl = payload(code, [case(0, [[3, 1, 2]], None)],
                 comparison={"mode": "custom_validator", "validator_code": SAME_MULTISET})
    res = results(pl)
    assert res[0]["status"] == "wrong_answer"


def test_custom_validator_that_raises_is_judge_error_not_runtime_error():
    """A validator bug is a problem-authoring fault, never the submitter's —
    distinct status from a submission's own runtime_error (AGENTS.md)."""
    code = "def f(s):\n    return s"
    broken_validator = (
        "def validate(actual, expected, args, instance=None):\n"
        "    raise ValueError('validator bug')\n"
    )
    pl = payload(code, [case(0, ["x"], None)],
                 comparison={"mode": "custom_validator", "validator_code": broken_validator})
    res = results(pl)
    assert res[0]["status"] == "judge_error"
    assert "validator bug" in res[0]["error"]


def test_custom_validator_missing_validate_function_fails_fast():
    """A validator script that never defines `validate` fails once, for the
    whole payload — mirrors the top-level user-code-compile-failure path."""
    code = "def f(s):\n    return s"
    pl = payload(code, [case(0, ["x"], None), case(1, ["y"], None)],
                 comparison={"mode": "custom_validator", "validator_code": "x = 1\n"})
    res = results(pl)
    assert len(res) == 1
    assert res[0]["status"] == "judge_error"
    assert "validate" in res[0]["error"]


def test_custom_validator_syntax_error_fails_fast():
    code = "def f(s):\n    return s"
    pl = payload(code, [case(0, ["x"], None)],
                 comparison={"mode": "custom_validator", "validator_code": "def validate(:\n"})
    res = results(pl)
    assert res[0]["status"] == "judge_error"


def test_custom_validator_hang_is_time_limit_exceeded():
    """A validator's own compute (e.g. a many-call statistical check) runs
    under the same per-case SIGALRM as the submission's call, so a runaway
    validator reports time_limit_exceeded rather than hanging the container —
    unlike `compare()` for the other four modes, which stays untimed."""
    code = "def f(s):\n    return s"
    hanging_validator = (
        "def validate(actual, expected, args, instance=None):\n"
        "    while True:\n"
        "        pass\n"
    )
    pl = payload(code, [case(0, ["x"], None)],
                 comparison={"mode": "custom_validator", "validator_code": hanging_validator},
                 time_limit_ms=200)
    res = results(pl)
    assert res[0]["status"] == "time_limit_exceeded"


# --- ListNode/TreeNode codec -----------------------------------------------

REVERSE_LIST = (
    "class ListNode:\n"
    "    def __init__(self, val=0, next=None):\n"
    "        self.val = val\n"
    "        self.next = next\n"
    "\n"
    "def f(head):\n"
    "    prev = None\n"
    "    while head:\n"
    "        head.next, prev, head = prev, head, head.next\n"
    "    return prev\n"
)


def test_listnode_param_and_return_round_trip():
    pl = payload(REVERSE_LIST, [case(0, [[1, 2, 3]], [3, 2, 1]), case(1, [[]], [])],
                 params=[{"name": "head", "type": "ListNode"}], return_type="ListNode")
    res = results(pl)
    assert [r["status"] for r in res] == ["passed", "passed"]


def test_listnode_return_value_shown_as_flat_array():
    pl = payload(REVERSE_LIST, [case(0, [[1, 2]], [2, 1])],
                 params=[{"name": "head", "type": "ListNode"}], return_type="ListNode")
    assert results(pl)[0]["output"] == "[2,1]"


def test_listnode_class_available_without_user_defining_it():
    # By convention starter_code/solutions show ListNode only as a comment. The harness must pre-bind the real name so code that never
    # defines its own class (here, one that *constructs* a new node) still works.
    code = "def f(n):\n    return ListNode(n)\n"
    pl = payload(code, [case(0, [5], [5])], return_type="ListNode")
    res = results(pl)
    assert res[0]["status"] == "passed", res[0]["error"]


def test_treenode_class_available_without_user_defining_it():
    code = "def f(n):\n    return TreeNode(n)\n"
    pl = payload(code, [case(0, [5], [5])], return_type="TreeNode")
    res = results(pl)
    assert res[0]["status"] == "passed", res[0]["error"]


def test_iterator_class_available_without_user_defining_it():
    # Unlike ListNode/TreeNode, no submission ever constructs an Iterator
    # itself (it only receives a pre-built one via the "Iterator" param
    # codec) — but it's still pre-bound in the general namespace for the same
    # consistency reason as the others (judge/harness.py).
    code = "def f(n):\n    it = Iterator([n])\n    return it.next()\n"
    pl = payload(code, [case(0, [5], 5)])
    res = results(pl)
    assert res[0]["status"] == "passed", res[0]["error"]


def test_randomlistnode_class_available_without_user_defining_it():
    code = "def f(n):\n    return RandomListNode(n)\n"
    pl = payload(code, [case(0, [5], [[5, None]])], return_type="RandomListNode")
    res = results(pl)
    assert res[0]["status"] == "passed", res[0]["error"]


def test_graphnode_class_available_without_user_defining_it():
    code = "def f(n):\n    return GraphNode(n)\n"
    pl = payload(code, [case(0, [1], [[]])], return_type="GraphNode")
    res = results(pl)
    assert res[0]["status"] == "passed", res[0]["error"]


NODE_VALUE = (
    "def f(node):\n"
    "    return node.val\n"
)


def test_listnode_param_without_return_codec():
    # Only the input side needs conversion — return_type left untouched (plain int).
    pl = payload(NODE_VALUE, [case(0, [[42, 1, 2]], 42)],
                 params=[{"name": "node", "type": "ListNode"}])
    assert results(pl)[0]["status"] == "passed"


INVERT_TREE = (
    "class TreeNode:\n"
    "    def __init__(self, val=0, left=None, right=None):\n"
    "        self.val = val\n"
    "        self.left = left\n"
    "        self.right = right\n"
    "\n"
    "def f(root):\n"
    "    if root is None:\n"
    "        return None\n"
    "    root.left, root.right = f(root.right), f(root.left)\n"
    "    return root\n"
)


CYCLIC_TREE = (
    "class TreeNode:\n"
    "    def __init__(self, val=0, left=None, right=None):\n"
    "        self.val = val\n"
    "        self.left = left\n"
    "        self.right = right\n"
    "\n"
    "def f(root):\n"
    "    root.left = root\n"  # buggy solution wires a child back to an ancestor
    "    return root\n"
)


def test_treenode_cyclic_return_does_not_hang():
    pl = payload(CYCLIC_TREE, [case(0, [[1, 2]], [1])], time_limit_ms=300,
                 params=[{"name": "root", "type": "TreeNode"}], return_type="TreeNode")
    res = results(pl, timeout=5)
    assert res[0]["status"] != "time_limit_exceeded"


def test_treenode_param_and_return_round_trip():
    pl = payload(INVERT_TREE, [
        case(0, [[4, 2, 7, 1, 3, 6, 9]], [4, 7, 2, 9, 6, 3, 1]),
        case(1, [[1, None, 2]], [1, 2]),
        case(2, [[]], []),
    ], params=[{"name": "root", "type": "TreeNode"}], return_type="TreeNode")
    res = results(pl)
    assert [r["status"] for r in res] == ["passed", "passed", "passed"]


# --- List[ListNode]/List[TreeNode] codec ------------------------------------

LISTNODE_CLASS = (
    "class ListNode:\n"
    "    def __init__(self, val=0, next=None):\n"
    "        self.val = val\n"
    "        self.next = next\n"
    "\n"
)

MERGE_LISTS = LISTNODE_CLASS + (
    "def f(lists):\n"
    "    vals = []\n"
    "    for node in lists:\n"
    "        while node:\n"
    "            vals.append(node.val)\n"
    "            node = node.next\n"
    "    vals.sort()\n"
    "    head = tail = None\n"
    "    for v in vals:\n"
    "        node = ListNode(v)\n"
    "        if head is None:\n"
    "            head = tail = node\n"
    "        else:\n"
    "            tail.next = node\n"
    "            tail = node\n"
    "    return head\n"
)


def test_list_of_listnode_param_round_trip():
    # "Merge several sorted lists": List[ListNode] in, a single ListNode out.
    pl = payload(MERGE_LISTS, [
        case(0, [[[2, 8], [1, 5, 9], [3]]], [1, 2, 3, 5, 8, 9]),
        case(1, [[]], []),  # no lists at all
        case(2, [[[]]], []),  # one empty list within the list of lists
    ], params=[{"name": "lists", "type": "List[ListNode]"}], return_type="ListNode")
    res = results(pl)
    assert [r["status"] for r in res] == ["passed", "passed", "passed"]


REVERSE_EACH_LIST = LISTNODE_CLASS + (
    "def f(lists):\n"
    "    out = []\n"
    "    for node in lists:\n"
    "        prev = None\n"
    "        while node:\n"
    "            node.next, prev, node = prev, node, node.next\n"
    "        out.append(prev)\n"
    "    return out\n"
)


def test_list_of_listnode_return_round_trip():
    pl = payload(REVERSE_EACH_LIST, [
        case(0, [[[1, 2, 3], [4, 5]]], [[3, 2, 1], [5, 4]]),
        case(1, [[]], []),
    ], params=[{"name": "lists", "type": "List[ListNode]"}], return_type="List[ListNode]")
    res = results(pl)
    assert [r["status"] for r in res] == ["passed", "passed"]


TREENODE_CLASS = (
    "class TreeNode:\n"
    "    def __init__(self, val=0, left=None, right=None):\n"
    "        self.val = val\n"
    "        self.left = left\n"
    "        self.right = right\n"
    "\n"
)

INVERT_EACH_TREE = TREENODE_CLASS + (
    "def invert(root):\n"
    "    if root is None:\n"
    "        return None\n"
    "    root.left, root.right = invert(root.right), invert(root.left)\n"
    "    return root\n"
    "\n"
    "def f(trees):\n"
    "    return [invert(t) for t in trees]\n"
)


def test_list_of_treenode_param_and_return_round_trip():
    pl = payload(INVERT_EACH_TREE, [
        case(0, [[[4, 2, 7, 1, 3, 6, 9], [1, None, 2]]], [[4, 7, 2, 9, 6, 3, 1], [1, 2]]),
        case(1, [[]], []),
    ], params=[{"name": "trees", "type": "List[TreeNode]"}], return_type="List[TreeNode]")
    res = results(pl)
    assert [r["status"] for r in res] == ["passed", "passed"]


# --- CyclicListNode codec ----------------------------------------------------
# `[values, pos]` (the cycle-detection wire encoding) decodes to
# a genuinely cyclic list, unlike plain ListNode which can only build a
# straight line. A CyclicListNode *return value* encodes back to the returned
# node's original index in `values` (identity-based — see harness.py's
# `_encode_cyclic_node`), not its `.val`, so duplicate values in `values`
# don't make the expected answer ambiguous.

HAS_CYCLE = (
    "def f(head):\n"
    "    slow = fast = head\n"
    "    while fast and fast.next:\n"
    "        slow = slow.next\n"
    "        fast = fast.next.next\n"
    "        if slow is fast:\n"
    "            return True\n"
    "    return False\n"
)


def test_cyclic_listnode_param_decodes_a_real_cycle():
    pl = payload(HAS_CYCLE, [
        case(0, [[[6, 8, 4, 9], 2]], True),
        case(1, [[[1, 2], 0]], True),
        case(2, [[[1], -1]], False),
        case(3, [[[], -1]], False),
        case(4, [[[1], 0]], True),  # single-node self-cycle
    ], params=[{"name": "head", "type": "CyclicListNode"}])
    res = results(pl)
    assert [r["status"] for r in res] == ["passed"] * 5


DETECT_CYCLE = (
    "def f(head):\n"
    "    slow = fast = head\n"
    "    while fast and fast.next:\n"
    "        slow = slow.next\n"
    "        fast = fast.next.next\n"
    "        if slow is fast:\n"
    "            ptr = head\n"
    "            while ptr is not slow:\n"
    "                ptr = ptr.next\n"
    "                slow = slow.next\n"
    "            return ptr\n"
    "    return None\n"
)


def test_cyclic_listnode_return_round_trip_by_identity():
    pl = payload(DETECT_CYCLE, [
        case(0, [[[6, 8, 4, 9], 2]], 2),
        case(1, [[[1], -1]], None),
        case(2, [[[1], 0]], 0),
        # duplicate values: only identity (not .val) can tell node 2 from node 4
        case(3, [[[1, 2, 1, 2, 1], 2]], 2),
    ], params=[{"name": "head", "type": "CyclicListNode"}], return_type="CyclicListNode")
    res = results(pl)
    assert [r["status"] for r in res] == ["passed"] * 4


FABRICATE_CYCLE_NODE = (
    "class ListNode:\n"
    "    def __init__(self, val=0, next=None):\n"
    "        self.val = val\n"
    "        self.next = next\n"
    "\n"
    "def f(head):\n"
    "    slow = fast = head\n"
    "    while fast and fast.next:\n"
    "        slow = slow.next\n"
    "        fast = fast.next.next\n"
    "        if slow is fast:\n"
    "            return ListNode(slow.val)\n"  # fabricated node, not the real one
    "    return None\n"
)


def test_cyclic_listnode_return_rejects_a_fabricated_node():
    # A solution that builds a fresh node with the right .val instead of
    # returning the real one from the input graph must not be able to pass by
    # accident — the harness can only recognize nodes it built itself.
    pl = payload(FABRICATE_CYCLE_NODE, [case(0, [[[6, 8, 4, 9], 2]], 2)],
                 params=[{"name": "head", "type": "CyclicListNode"}], return_type="CyclicListNode")
    res = results(pl)
    assert res[0]["status"] == "wrong_answer"


# --- RandomListNode codec -----------------------------------------------------
# `[[val, random_index], ...]` (the `"RandomListNode"` wire encoding) decodes to a linked list whose nodes carry a second
# `.random` pointer to an arbitrary node (or None). Unlike CyclicListNode, the
# *return* direction can't rely on an `_idx` stamp from build time — the
# submission is supposed to return a freshly built deep copy, not a node from
# the input graph — so the return encoder re-derives indices purely from the
# structure of whatever graph comes back (see harness.py's
# `_encode_random_list`).

READ_RANDOM_VALUES = (
    "def f(head):\n"
    "    out = []\n"
    "    node = head\n"
    "    while node:\n"
    "        out.append(node.random.val if node.random else None)\n"
    "        node = node.next\n"
    "    return out\n"
)


def test_randomlistnode_param_decodes_random_pointers():
    pl = payload(READ_RANDOM_VALUES, [
        # Random pointers land on arbitrary indices, including ones later in
        # the array.
        case(0, [[[4, None], [9, 0], [2, 4], [6, 2], [3, 0]]], [None, 4, 3, 2, 4]),
        case(1, [[[5, 0]]], [5]),                 # a node whose random is itself
        case(2, [[[1, None], [2, None]]], [None, None]),  # random_index null throughout
        case(3, [[]], []),
    ], params=[{"name": "head", "type": "RandomListNode"}])
    res = results(pl)
    assert [r["status"] for r in res] == ["passed"] * 4


IDENTITY_RANDOM_LIST = "def f(head):\n    return head\n"


def test_randomlistnode_return_round_trip():
    wire = [[4, None], [9, 0], [2, 4], [6, 2], [3, 0]]
    pl = payload(IDENTITY_RANDOM_LIST, [case(0, [wire], wire), case(1, [[]], [])],
                 params=[{"name": "head", "type": "RandomListNode"}],
                 return_type="RandomListNode")
    res = results(pl)
    assert [r["status"] for r in res] == ["passed", "passed"]


CYCLIC_RANDOM_RETURN = (
    "class RandomListNode:\n"
    "    def __init__(self, val=0, next=None, random=None):\n"
    "        self.val = val\n"
    "        self.next = next\n"
    "        self.random = random\n"
    "\n"
    "def f(head):\n"
    "    a = RandomListNode(1)\n"
    "    b = RandomListNode(2)\n"
    "    a.next = b\n"
    "    b.next = a\n"  # a buggy submission's .next cycle
    "    return a\n"
)


def test_randomlistnode_return_encoder_is_cycle_safe():
    # A submission returning a .next-cyclic structure must not hang the
    # encoder (run_harness's own subprocess timeout would fail this test if
    # it did) — it should terminate and simply score as wrong_answer.
    pl = payload(CYCLIC_RANDOM_RETURN, [case(0, [[]], [[9, None]])],
                 params=[{"name": "head", "type": "RandomListNode"}],
                 return_type="RandomListNode")
    res = results(pl)
    assert res[0]["status"] == "wrong_answer"


# --- GraphNode codec ------------------------------------------------------
# The adjacency-list wire encoding: `adj_list[i]` is the list of neighbor
# *values* for the node valued `i + 1` (1-indexed by value, not array
# position). Like RandomListNode, the *return* direction can't rely on an
# `_idx` stamp from build time — a graph deep-copy's whole point is that the
# submission returns a fresh copy, not a node from the input graph.
# Unlike RandomListNode, it also can't re-derive positions from traversal
# order — a graph has no single unambiguous walk, so the wire row for a node
# is keyed by that node's own `.val` (see harness.py's `_encode_graph`).

READ_GRAPH_SHAPE = (
    "def f(node):\n"
    "    if node is None:\n"
    "        return None\n"
    "    return [\n"
    "        node.val,\n"
    "        [nb.val for nb in node.neighbors],\n"
    "        (node.neighbors[0].neighbors[0] is node) if node.neighbors else None,\n"
    "    ]\n"
)


def test_graphnode_param_decodes_neighbors():
    pl = payload(READ_GRAPH_SHAPE, [
        # A 2-node cycle: neighbors resolve to real node objects, not just
        # matching values — walking back through node 2's neighbor lands on
        # the exact same node 1 object the harness built.
        case(0, [[[2], [1]]], [1, [2], True]),
        case(1, [[[]]], [1, [], None]),   # a single isolated node, not an empty graph
        case(2, [[]], None),              # a genuinely empty graph -> None
    ], params=[{"name": "node", "type": "GraphNode"}])
    res = results(pl)
    assert [r["status"] for r in res] == ["passed"] * 3


IDENTITY_GRAPH = "def f(head):\n    return head\n"


def test_graphnode_return_round_trip():
    wire = [[2, 3], [1, 3], [1, 2, 4], [3]]  # a triangle with a pendant node
    pl = payload(IDENTITY_GRAPH, [
        case(0, [wire], wire),
        case(1, [[[]]], [[]]),  # single isolated node
        case(2, [[]], []),      # empty graph
    ], params=[{"name": "head", "type": "GraphNode"}], return_type="GraphNode")
    res = results(pl)
    assert [r["status"] for r in res] == ["passed"] * 3


CYCLIC_GRAPH_RETURN = (
    "class GraphNode:\n"
    "    def __init__(self, val=0, neighbors=None):\n"
    "        self.val = val\n"
    "        self.neighbors = neighbors or []\n"
    "\n"
    "def f(head):\n"
    "    a = GraphNode(1)\n"
    "    b = GraphNode(2)\n"
    "    a.neighbors = [b]\n"
    "    b.neighbors = [a]\n"  # mutual reference — a correct undirected graph, not a bug
    "    return a\n"
)


def test_graphnode_return_encoder_is_cycle_safe():
    # An undirected graph is inherently cyclic (that's why copying one needs a
    # visited map) — the encoder must not hang on this,
    # unlike the other node-graph codecs where a cycle only shows up in buggy
    # output. Mismatched `expected` keeps this from passing by coincidence.
    pl = payload(CYCLIC_GRAPH_RETURN, [case(0, [[]], [[99]])],
                 params=[{"name": "head", "type": "GraphNode"}],
                 return_type="GraphNode")
    res = results(pl)
    assert res[0]["status"] == "wrong_answer"
