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
    "class LookaheadIterator:\n"
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
    ops = ["LookaheadIterator", "next", "peek", "next", "next", "hasNext"]
    args = [[[4, 5, 6]], [], [], [], [], []]
    expected = [None, 4, 5, 5, 6, False]
    res = results(payload(PEEKING_ITERATOR, [case(0, ops, args, expected)],
                          class_name="LookaheadIterator",
                          params=[{"name": "iterator", "type": "Iterator"}]))
    assert res[0]["status"] == "passed", res[0]["error"]


# --- custom_validator mode ---------------------------------------------------
# An operations validator judges the per-op result list in the parent. Further
# calls on the instance (a round trip, a distribution) are the case's probes,
# below; the older form that made them itself through the live `instance` is
# refused.

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

def test_an_older_form_operations_validator_is_refused():
    """The older `validate(..., instance=None)` form made its own calls into the
    live object, so it had to run in the child, next to the submission, where
    `expected` was readable and its verdict forgeable. Probes replaced it
    (ADR-0007), and the harness now refuses it before any child starts."""
    older = ("def validate(actual, expected, args, instance=None):\n"
             "    return instance.decode(actual[1]) == args[1][1][0]\n")
    res = results(payload(CODEC, [case(0, ["Codec", "encode"], [[], ["https://a.b/c"]], None)],
                          class_name="Codec", comparison={"mode": "custom_validator", "validator_code": older}))
    assert [r["status"] for r in res] == ["judge_error"]


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
        "def validate(actual, expected, args, probe_results):\n"
        "    ops, arg_lists = args\n"
        "    return actual[1] == len(arg_lists[0][0])\n"
    )
    res = results(payload(keeps_and_clears, [case(0, ["C", "size"], [[[1, 2, 3]], []], None)],
                          comparison={"mode": "custom_validator", "validator_code": size_matches_input}))
    assert res[0]["status"] == "wrong_answer"


# --- probes: judge-added calls, checked in the parent (ADR-0007) --------------
# A validator (`validate(actual, expected, args, probe_results)`) gets the results
# of the case's probes instead of the live instance, so it runs in the trusted
# parent. Probes run in the child on the same instance, after the replay.

PROBE_ROUND_TRIP = (
    "def validate(actual, expected, args, probe_results):\n"
    "    return probe_results == [args[1][1][0]]\n"
)


def probe_case(i, ops, args, probes, expected=None):
    return {**case(i, ops, args, expected), "probes": probes}


def _validator(code):
    return {"mode": "custom_validator", "validator_code": code}


DECODE_OP_1 = [{"op": "decode", "args": [None], "refs": {"0": 1}}]


def test_a_probe_ref_feeds_an_earlier_result_back_in():
    """`decode(encode(x)) == x`: the probe's argument is op 1's result."""
    res = results(payload(CODEC, [probe_case(0, ["Codec", "encode"], [[], ["https://a.b/c"]], DECODE_OP_1)],
                          class_name="Codec", comparison=_validator(PROBE_ROUND_TRIP)))
    assert res[0]["status"] == "passed", res[0]["error"]
    assert res[0]["output"] == '[null,"http://tiny/0"]'  # `actual` excludes the probes


def test_a_broken_round_trip_is_a_wrong_answer():
    wrong = CODEC.replace("return self.store[short_url.rsplit('/', 1)[-1]]", "return 'nope'")
    res = results(payload(wrong, [probe_case(0, ["Codec", "encode"], [[], ["https://a.b/c"]], DECODE_OP_1)],
                          class_name="Codec", comparison=_validator(PROBE_ROUND_TRIP)))
    assert res[0]["status"] == "wrong_answer"


def test_a_probe_that_raises_is_the_submissions_runtime_error_labelled_as_judge_added():
    """The harness made this call on the submission's object, so a raise is the
    submission's bug, not the validator's (judge_error)."""
    never_stores = CODEC.replace("self.store[key] = long_url", "pass")
    res = results(payload(never_stores, [probe_case(0, ["Codec", "encode"], [[], ["https://a.b/c"]],
                                                    DECODE_OP_1)],
                          class_name="Codec", comparison=_validator(PROBE_ROUND_TRIP)))
    assert res[0]["status"] == "runtime_error"
    assert "call the judge added" in res[0]["error"]
    assert "decode(), call 1 of 1" in res[0]["error"]
    assert "KeyError" in res[0]["error"]


def test_repeat_makes_the_same_call_n_times():
    counter = (
        "class C:\n"
        "    def __init__(self):\n"
        "        self.n = 0\n"
        "    def tick(self):\n"
        "        self.n += 1\n"
        "        return self.n\n"
    )
    counts = (
        "def validate(actual, expected, args, probe_results):\n"
        "    return probe_results == list(range(2, 1002))\n"
    )
    res = results(payload(counter, [probe_case(0, ["C", "tick"], [[], []], [{"op": "tick", "repeat": 1000}])],
                          comparison=_validator(counts)))
    assert res[0]["status"] == "passed", res[0]["error"]


def test_probes_run_inside_the_case_time_limit():
    slow = (
        "import time\n"
        "class C:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "    def tick(self):\n"
        "        time.sleep(0.01)\n"
    )
    accept = "def validate(actual, expected, args, probe_results):\n    return True\n"
    res = results(payload(slow, [probe_case(0, ["C"], [[]], [{"op": "tick", "repeat": 100}])],
                          comparison=_validator(accept), time_limit_ms=300))
    assert res[0]["status"] == "time_limit_exceeded"


def test_a_probe_form_validator_cannot_be_forged_or_read_expected():
    """The operations counterpart of test_protocol.py's forge/peek tests: a
    probe-form validator runs in the parent, so the child gets no `expected` and
    its verdict frame isn't trusted."""
    forge = (
        "import json, os, sys\n"
        "class C:\n"
        "    def __init__(self):\n"
        "        frame = sys._getframe()\n"
        "        while frame:\n"
        "            m = frame.f_locals.get('message')\n"
        "            if isinstance(m, dict) and 'input' in m:\n"
        "                break\n"
        "            frame = frame.f_back\n"
        "        leaked = 'expected' in m\n"
        "        reply = {'status': 'ok', 'actual': [leaked], 'passed': True,\n"
        "                 'probe_results': [], 'runtime_ms': 0, 'stdout': ''}\n"
        "        os.write(int(sys.argv[2]), (json.dumps(reply) + '\\n').encode())\n"
        "        os._exit(0)\n"
    )
    wants_secret = (
        "def validate(actual, expected, args, probe_results):\n"
        "    return actual == ['the secret']\n"
    )
    res = results(payload(forge, [case(0, ["C"], [[]], "the secret")],
                          comparison=_validator(wants_secret)))
    assert res[0]["status"] == "wrong_answer"
    assert res[0]["output"] == "[false]"  # `expected` never reached the child


def test_a_reply_missing_probe_results_is_a_runtime_error_not_a_judge_error():
    """A forged reply with the wrong number of probe results mustn't reach the
    validator (whose resulting crash would blame the author)."""
    forge = (
        "import json, os, sys\n"
        "class C:\n"
        "    def __init__(self):\n"
        "        reply = {'status': 'ok', 'actual': [None], 'probe_results': [1],\n"
        "                 'runtime_ms': 0, 'stdout': ''}\n"
        "        os.write(int(sys.argv[2]), (json.dumps(reply) + '\\n').encode())\n"
        "        os._exit(0)\n"
        "    def tick(self):\n"
        "        return 1\n"
    )
    indexes = "def validate(actual, expected, args, probe_results):\n    return probe_results[2] == 1\n"
    res = results(payload(forge, [probe_case(0, ["C"], [[]], [{"op": "tick", "repeat": 3}])],
                          comparison=_validator(indexes)))
    assert res[0]["status"] == "runtime_error"
    assert "didn't account for every check" in res[0]["error"]


def test_a_probe_form_validator_works_without_probes():
    """Group B problems: a validator that replays the ops against a model needs
    no probes, only the new signature, to move to the parent."""
    res = results(payload(LRU_CACHE, [case(0, ["LRUCache", "put", "get"], [[1], [1, 1], [1]], None)],
                          class_name="LRUCache",
                          comparison=_validator("def validate(actual, expected, args, probe_results):\n"
                                                "    return probe_results == [] and actual[2] == 1\n")))
    assert res[0]["status"] == "passed", res[0]["error"]


def test_a_malformed_probe_is_a_judge_error():
    """A ref past the case's ops is an authoring bug (seed validation refuses it)."""
    res = results(payload(CODEC, [probe_case(0, ["Codec", "encode"], [[], ["u"]],
                                             [{"op": "decode", "args": [None], "refs": {"0": 9}}])],
                          class_name="Codec", comparison=_validator(PROBE_ROUND_TRIP)))
    assert res[0]["status"] == "judge_error"
    # A fixed line: the probe is a hidden case's data. The detail is in stderr.
    assert res[0]["error"].startswith("a check the judge adds to this test case is malformed")


def test_a_forged_probe_error_frame_cant_turn_a_case_without_probes_into_a_judge_error():
    """The child's frame is untrusted. A `probe_error` status only means
    something when the parent sent probes; otherwise the frame is judged like any
    other, on its (missing) answer."""
    forge = (
        "import json, os, sys\n"
        "class Codec:\n"
        "    def __init__(self):\n"
        "        frame = {'status': 'probe_error', 'error': 5}\n"
        "        os.write(int(sys.argv[2]), (json.dumps(frame) + '\\n').encode())\n"
        "        os._exit(0)\n"
    )
    res = results(payload(forge, [case(0, ["Codec", "encode"], [[], ["u"]], None)],
                          class_name="Codec", comparison=_validator(PROBE_ROUND_TRIP)))
    assert res[0]["status"] == "wrong_answer"


def test_a_probe_cannot_change_actual_through_returned_internal_state():
    """`getAll` returns the instance's own list; the probes then append to it.
    `actual` is what `getAll` returned at the time, not the list afterwards."""
    leaky = (
        "class C:\n"
        "    def __init__(self):\n"
        "        self.items = []\n"
        "    def getAll(self):\n"
        "        return self.items\n"
        "    def add(self):\n"
        "        self.items.append(1)\n"
    )
    empty_then_three = (
        "def validate(actual, expected, args, probe_results):\n"
        "    return actual == [None, []]\n"
    )
    res = results(payload(leaky, [probe_case(0, ["C", "getAll"], [[], []], [{"op": "add", "repeat": 3}])],
                          comparison=_validator(empty_then_three)))
    assert res[0]["status"] == "passed", res[0]
    assert res[0]["output"] == "[null,[]]"


def test_an_unencodable_result_is_the_submissions_runtime_error_not_a_judge_error():
    """`refs` read the JSON snapshot, so the harness never copies an object the
    submission returned; one it can't encode fails as the submission's own error."""
    returns_a_generator = (
        "class Codec:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "    def encode(self, s):\n"
        "        return (c for c in s)\n"
        "    def decode(self, s):\n"
        "        return s\n"
    )
    res = results(payload(returns_a_generator,
                          [probe_case(0, ["Codec", "encode"], [[], ["abc"]], DECODE_OP_1)],
                          class_name="Codec", comparison=_validator(PROBE_ROUND_TRIP)))
    assert res[0]["status"] == "runtime_error"
    assert "not JSON serializable" in res[0]["error"]
