"""Harness protocol tests for `kind: "operations"` (design/class-replay
problems — a cache, an iterator, a state machine; DESIGN.md §12) — same subprocess/
result-JSON approach as `test_protocol.py`, kept standalone rather than
importing from it (matching `test_sandbox.py`'s own-helpers convention).
"""
import json
import pathlib
import subprocess
import sys

HARNESS = pathlib.Path(__file__).resolve().parents[1] / "harness.py"


def payload(user_code, test_cases, class_name="C", comparison=None, time_limit_ms=2000,
            stop_on_first_failure=False, params=None):
    return {
        "kind": "operations",
        "class_name": class_name,
        "user_code": user_code,
        "test_cases": test_cases,
        "comparison": comparison or {"mode": "exact"},
        "time_limit_ms": time_limit_ms,
        "stop_on_first_failure": stop_on_first_failure,
        "params": params or [],
    }


def case(i, ops, args, expected):
    return {"id": i, "input": [ops, args], "expected": expected}


def run_harness(pl, timeout=15):
    return subprocess.run(
        [sys.executable, str(HARNESS)],
        input=json.dumps(pl), capture_output=True, text=True, timeout=timeout,
    )


def results(pl, **kw):
    proc = run_harness(pl, **kw)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["results"]


LRU_CACHE = (
    "class LRUCache:\n"
    "    def __init__(self, capacity):\n"
    "        self.capacity = capacity\n"
    "        self.cache = {}\n"
    "        self.order = []\n"
    "\n"
    "    def get(self, key):\n"
    "        if key not in self.cache:\n"
    "            return -1\n"
    "        self.order.remove(key)\n"
    "        self.order.append(key)\n"
    "        return self.cache[key]\n"
    "\n"
    "    def put(self, key, value):\n"
    "        if key in self.cache:\n"
    "            self.order.remove(key)\n"
    "        elif len(self.cache) >= self.capacity:\n"
    "            oldest = self.order.pop(0)\n"
    "            del self.cache[oldest]\n"
    "        self.cache[key] = value\n"
    "        self.order.append(key)\n"
)


def test_lru_cache_full_trace_passes():
    # Capacity 2: reading 5 makes 6 the least recent, so 7 evicts 6; then 8 evicts 5.
    ops = ["LRUCache", "put", "put", "get", "put", "get", "put", "get", "get", "get"]
    args = [[2], [5, 50], [6, 60], [5], [7, 70], [6], [8, 80], [5], [7], [8]]
    expected = [None, None, None, 50, None, -1, None, -1, 70, 80]
    res = results(payload(LRU_CACHE, [case(0, ops, args, expected)], class_name="LRUCache"))
    assert res[0]["status"] == "passed", res[0]["error"]
    assert res[0]["output"] == json.dumps(expected, separators=(",", ":"))


def test_wrong_answer_on_one_op():
    ops = ["LRUCache", "put", "get"]
    args = [[2], [1, 1], [1]]
    res = results(payload(LRU_CACHE, [case(0, ops, args, [None, None, 999])],
                          class_name="LRUCache"))
    assert res[0]["status"] == "wrong_answer"


def test_class_not_found():
    res = results(payload(LRU_CACHE, [case(0, ["Wrong"], [[2]], [None])],
                          class_name="Wrong"))
    assert res[0]["status"] == "runtime_error"
    assert "Class 'Wrong' not found" in res[0]["error"]


def test_misspelled_method_is_runtime_error_with_user_frames_only():
    ops = ["LRUCache", "gett"]  # typo'd method name
    args = [[2], [1]]
    res = results(payload(LRU_CACHE, [case(0, ops, args, [None, -1])], class_name="LRUCache"))
    assert res[0]["status"] == "runtime_error"
    assert "AttributeError" in res[0]["error"]
    assert "harness.py" not in res[0]["error"]  # harness frames are stripped


def test_tle_inside_one_op():
    code = (
        "class C:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "    def spin(self):\n"
        "        while True:\n"
        "            pass\n"
    )
    res = results(payload(code, [case(0, ["C", "spin"], [[], []], [None, None])],
                          time_limit_ms=200))
    assert res[0]["status"] == "time_limit_exceeded"


def test_multi_case_mixed_results_without_stop_on_first_failure():
    passing = case(0, ["LRUCache", "put", "get"], [[2], [1, 1], [1]], [None, None, 1])
    failing = case(1, ["LRUCache", "put", "get"], [[2], [1, 1], [1]], [None, None, 999])
    res = results(payload(LRU_CACHE, [passing, failing], class_name="LRUCache"))
    assert [r["status"] for r in res] == ["passed", "wrong_answer"]


# --- constructor-arg codec (ListNode/TreeNode into `kind: "operations"`) ----
# Only the constructor's args (`arg_lists[0]`, decoded via `params`) go through
# the codec; every method call after that is untouched — see the codec-scope
# comment in judge/harness.py.

BST_ITERATOR = (
    "class TreeNode:\n"
    "    def __init__(self, val=0, left=None, right=None):\n"
    "        self.val = val\n"
    "        self.left = left\n"
    "        self.right = right\n"
    "\n"
    "class BSTIterator:\n"
    "    def __init__(self, root):\n"
    "        self.stack = []\n"
    "        self._leftmost_inorder(root)\n"
    "\n"
    "    def next(self):\n"
    "        node = self.stack.pop()\n"
    "        if node.right:\n"
    "            self._leftmost_inorder(node.right)\n"
    "        return node.val\n"
    "\n"
    "    def hasNext(self):\n"
    "        return len(self.stack) > 0\n"
    "\n"
    "    def _leftmost_inorder(self, root):\n"
    "        while root:\n"
    "            self.stack.append(root)\n"
    "            root = root.left\n"
)


def test_bst_iterator_constructor_decodes_treenode():
    # In-order over a 5-node BST: 2, 4, 6, 8, 12, then exhausted.
    ops = ["BSTIterator", "next", "next", "hasNext", "next", "hasNext",
           "next", "hasNext", "next", "hasNext"]
    args = [[[8, 4, 12, 2, 6]], [], [], [], [], [], [], [], [], []]
    expected = [None, 2, 4, True, 6, True, 8, True, 12, False]
    res = results(payload(BST_ITERATOR, [case(0, ops, args, expected)],
                          class_name="BSTIterator",
                          params=[{"name": "root", "type": "TreeNode"}]))
    assert res[0]["status"] == "passed", res[0]["error"]


LIST_SUMMER = (
    "class ListNode:\n"
    "    def __init__(self, val=0, next=None):\n"
    "        self.val = val\n"
    "        self.next = next\n"
    "\n"
    "class ListSummer:\n"
    "    def __init__(self, head):\n"
    "        total = 0\n"
    "        while head:\n"
    "            total += head.val\n"
    "            head = head.next\n"
    "        self._total = total\n"
    "\n"
    "    def total(self):\n"
    "        return self._total\n"
)


def test_listnode_constructor_decodes_arg():
    ops = ["ListSummer", "total"]
    args = [[[1, 2, 3]], []]
    res = results(payload(LIST_SUMMER, [case(0, ops, args, [None, 6])],
                          class_name="ListSummer",
                          params=[{"name": "head", "type": "ListNode"}]))
    assert res[0]["status"] == "passed", res[0]["error"]


# --- Iterator codec (a pre-built Iterator into `kind: "operations"`) --------
# "Iterator" is decode-only (judge/harness.py's `_build_iterator`): a flat
# `List[int]` constructor arg becomes a pre-bound `Iterator` positioned at the
# start, for a class that wraps an iterator to add `peek()`.

PEEKING_ITERATOR = (
    "class PeekingIterator:\n"
    "    def __init__(self, iterator):\n"
    "        self.iterator = iterator\n"
    "        self._has_peeked = False\n"
    "        self._peeked = None\n"
    "\n"
    "    def peek(self):\n"
    "        if not self._has_peeked:\n"
    "            self._peeked = self.iterator.next()\n"
    "            self._has_peeked = True\n"
    "        return self._peeked\n"
    "\n"
    "    def next(self):\n"
    "        if self._has_peeked:\n"
    "            value = self._peeked\n"
    "            self._has_peeked = False\n"
    "            self._peeked = None\n"
    "            return value\n"
    "        return self.iterator.next()\n"
    "\n"
    "    def hasNext(self):\n"
    "        return self._has_peeked or self.iterator.hasNext()\n"
)


def test_peeking_iterator_constructor_decodes_iterator():
    # peek() must not advance: the value it shows is the one next() returns.
    ops = ["PeekingIterator", "next", "peek", "next", "next", "hasNext"]
    args = [[[4, 5, 6]], [], [], [], [], []]
    expected = [None, 4, 5, 5, 6, False]
    res = results(payload(PEEKING_ITERATOR, [case(0, ops, args, expected)],
                          class_name="PeekingIterator",
                          params=[{"name": "iterator", "type": "Iterator"}]))
    assert res[0]["status"] == "passed", res[0]["error"]


# --- custom_validator mode: the live `instance` -----------------------------
# `custom_validator` gets the live operations-mode `instance` (not just the
# per-op result list), so it can make *further* calls beyond the harness's
# own replay — e.g. a round-trip check where the second call's argument is
# the first call's result, which the fixed `ops`/`args` wire shape can't
# express on its own (DESIGN.md §5.4).

CODEC = (
    "class Codec:\n"
    "    def __init__(self):\n"
    "        self.store = {}\n"
    "    def encode(self, long_url):\n"
    "        key = str(len(self.store))\n"
    "        self.store[key] = long_url\n"
    "        return 'http://tiny/' + key\n"
    "    def decode(self, short_url):\n"
    "        return self.store[short_url.rsplit('/', 1)[-1]]\n"
)

ROUND_TRIP_VALIDATOR = (
    "def validate(actual, expected, args, instance=None):\n"
    "    original_url = args[1][1][0]\n"
    "    encoded = actual[1]\n"
    "    return instance.decode(encoded) == original_url\n"
)


def test_custom_validator_receives_live_instance_for_round_trip():
    ops = ["Codec", "encode"]
    args = [[], ["https://example.com/some/path"]]
    res = results(payload(CODEC, [case(0, ops, args, None)], class_name="Codec",
                          comparison={"mode": "custom_validator", "validator_code": ROUND_TRIP_VALIDATOR}))
    assert res[0]["status"] == "passed", res[0]["error"]


def test_custom_validator_catches_broken_round_trip():
    # A broken submission (encode() never stores the url) makes the
    # validator's own extra call — `instance.decode(...)` — raise KeyError.
    # Any exception raised while `validate()` runs is reported the same way
    # (judge_error), whether it's the validator's own bug or a submission bug
    # surfacing through a call the validator chose to make — the harness has
    # no way to tell those apart, and doesn't try to.
    broken_codec = CODEC.replace("self.store[key] = long_url", "pass")  # never stores it
    ops = ["Codec", "encode"]
    args = [[], ["https://example.com/some/path"]]
    res = results(payload(broken_codec, [case(0, ops, args, None)], class_name="Codec",
                          comparison={"mode": "custom_validator", "validator_code": ROUND_TRIP_VALIDATOR}))
    assert res[0]["status"] == "judge_error"
    assert "custom validator" in res[0]["error"]


def test_custom_validator_sees_constructor_args_before_the_submission_mutated_them():
    """Same guarantee as the function-mode test in test_protocol.py, for
    operations mode: a constructor that keeps a reference to its argument
    list and edits it in place (a shuffle, a sort) must not change the
    `args` the validator reads as the original input."""
    keeps_and_clears = (
        "class C:\n"
        "    def __init__(self, nums):\n"
        "        self.nums = nums\n"
        "        nums.clear()\n"
        "    def size(self):\n"
        "        return len(self.nums)\n"
    )
    size_matches_input = (
        "def validate(actual, expected, args, instance=None):\n"
        "    ops, arg_lists = args\n"
        "    return actual[1] == len(arg_lists[0][0])\n"
    )
    res = results(payload(keeps_and_clears, [case(0, ["C", "size"], [[[1, 2, 3]], []], None)],
                          comparison={"mode": "custom_validator", "validator_code": size_matches_input}))
    assert res[0]["status"] == "wrong_answer"
