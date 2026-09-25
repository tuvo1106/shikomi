#!/usr/bin/env python3
"""Judge harness — runs untrusted user code against test cases inside the sandbox.

Protocol (DESIGN.md §5.3):
  stdin:  payload JSON  {function_name, user_code, test_cases[], comparison,
                         time_limit_ms, params[], return_type, kind, class_name}
  stdout: exactly one result JSON document  {"results": [...]}

`params[].type`/`return_type` are optional; when a param or the return value is
declared `"ListNode"`/`"TreeNode"`, test-case JSON arrays are converted to/from
that object graph around the call — every other type string is a
no-op passthrough. `ListNode`/`TreeNode`/
`RandomListNode`/`GraphNode` are all also pre-bound in the user code's execution
namespace regardless of whether this particular problem uses them (by
convention `starter_code`/solutions show the class only as a comment, never
live code, since the runtime already provides it).
`"CyclicListNode"` is a separate declared type (not a variant of `"ListNode"`)
for problems that need a genuinely cyclic input/answer — see the codec section
below for why `"ListNode"` itself can't represent one; it has no pre-bound
class of its own since it decodes to plain `ListNode` instances. `"RandomListNode"`
is another separate type, for a list whose nodes carry a second `.random`
pointer to an arbitrary node (or none) — see `_build_random_list`/
`_encode_random_list` below. `"GraphNode"` is a third, for a graph represented
as node objects (a `.val` plus a `.neighbors` list of other nodes) rather than
a flat list — see `_build_graph`/`_encode_graph` below. `"Iterator"` is a
fourth, decode-only type for `kind: "operations"` constructor args that take a
pre-built iterator object rather than a plain value (e.g. a class that wraps
an iterator to add a `peek()`) — see `_build_iterator` below.

`kind` selects the invocation mode: `"function"` (default) calls `function_name`
once per test case, as above. `"operations"` is the design/class-replay mode
(a cache, a state machine, a tree built from scratch): `class_name` names a class the harness
instantiates once per test case and replays a sequence of method calls
against, comparing the list of per-call results (see the "operations mode"
section below) — everything else (SIGALRM timeout, stdout capture, truncation,
comparison, `stop_on_first_failure`) is shared with function mode unchanged.

`comparison.mode == "custom_validator"` replaces the fixed-answer `compare()`
dispatch (DESIGN.md §5.4) with problem-authored Python for "any output
satisfying property P" problems (a round trip, a structural check, a
statistical property) that no fixed `expected` value can express — see the
"custom validator mode" section below.

Everything the user prints is captured into a per-case buffer so it can never
corrupt the stdout protocol. Stdlib only — this must run inside a network-less,
read-only container with nothing installed beyond CPython.
"""
import copy
import io
import json
import math
import signal
import sys
import time
import traceback
from collections import Counter, deque

TRUNC = 4096  # per-field cap for output/stdout/error (DESIGN.md §5.3)
USER_FILENAME = "<user_code>"  # frames with this filename are the user's code


class TimeLimitExceeded(Exception):
    """Raised by the SIGALRM handler when a single test case runs too long."""


class ValidatorError(Exception):
    """A problem-authored `custom_validator` failed to load or raised while
    checking a case — a problem-authoring bug, never the submitter's fault
    (unlike a `runtime_error`, which is the submission's own code failing).
    Reported as `judge_error`, same as any other internal fault."""


def _alarm_handler(signum, frame):
    raise TimeLimitExceeded()


def _truncate(text):
    if text is None:
        return None
    if len(text) <= TRUNC:
        return text
    return text[:TRUNC] + "…(truncated)"


def _format_output(value):
    """Render the actual return value. Compact JSON (matching how expected values
    are formatted client-side, so Output and Expected look identical), falling back
    to repr for values JSON can't represent (sets, custom objects, etc.)."""
    try:
        return json.dumps(value, separators=(",", ":"))
    except (TypeError, ValueError):
        try:
            return repr(value)
        except Exception:
            return "<unrepresentable>"


# --- ListNode/TreeNode codec -----------------------------------------------
# Converts between JSON-representable test-case data and the object graphs a
# linked-list/tree problem's function actually takes/returns. Scope: a single
# ListNode/TreeNode, or a flat `List[ListNode]`/`List[TreeNode]` (each element
# built/flattened independently by the same single-node codec, e.g. "merge
# these k sorted lists") — not a node whose own fields hold a list of other
# nodes referencing arbitrary other nodes (a graph's neighbor list); that case
# gets its own separate `"GraphNode"` type below (`_build_graph`/
# `_encode_graph`), not a mode of this one. Combined with "operations" mode
# (below) only for the constructor's args (`params` describes the constructor,
# same as function mode's params describe its one function) — a later method
# call's own args/return value are never decoded, since there's no per-method
# param typing anywhere in the schema.
#
# `"CyclicListNode"` (below `_build_cyclic_list`/`_encode_cyclic_node`) is a
# deliberately separate type, not a mode of `"ListNode"`: `_build_list` builds
# a straight-line list by walking a flat array once, which has no way to make
# `next` point back to an earlier node, and that must stay true — problems
# that declare `"ListNode"` param/return types rely on it meaning
# "straight-line list". Cycle-detection problems need a genuinely cyclic
# structure as input, so they get their own type instead of overloading this
# one.


class ListNode:
    """The harness's own linked-list node, built while deserializing input.

    The user's submission defines its *own* `class ListNode` (that's the
    starter-code convention) — a different class object
    from this one. That's fine: `_flatten_list` walks back out by attribute
    name (`val`/`next`), not `isinstance`, so it doesn't care whose class
    built the nodes the user's function returns.
    """

    def __init__(self, val=0, next=None):
        self.val = val
        self.next = next


class TreeNode:
    """The harness's own binary-tree node — see `ListNode` for why a separate,
    duck-typed class is fine even though the user's code defines its own."""

    def __init__(self, val=0, left=None, right=None):
        self.val = val
        self.left = left
        self.right = right


def _build_list(values):
    """Flat JSON array -> singly linked list. `[]`/`None` -> `None`."""
    head = None
    tail = None
    for v in values or []:
        node = ListNode(v)
        if head is None:
            head = node
        else:
            tail.next = node
        tail = node
    return head


def _flatten_list(node):
    """Linked list -> flat array, walking `.next` by duck-typed attribute
    access so it works on the user's own `ListNode` instances too."""
    out = []
    seen = set()  # a buggy solution's cycle shouldn't hang the harness
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        out.append(getattr(node, "val", None))
        node = getattr(node, "next", None)
    return out


def _build_tree(values):
    """Null-padded level-order array -> binary tree.

    `values[0]` is the root; each subsequent non-null value fills the next
    open child slot in BFS order. A `None` marks "no node here" and — unlike a
    full/perfect-tree encoding — does *not* reserve slots for its own
    (nonexistent) children, which keeps sparse trees' arrays short.
    """
    values = list(values or [])
    if not values or values[0] is None:
        return None
    it = iter(values)
    root = TreeNode(next(it))
    queue = [root]
    for node in queue:
        for side in ("left", "right"):
            try:
                v = next(it)
            except StopIteration:
                return root
            if v is not None:
                child = TreeNode(v)
                setattr(node, side, child)
                queue.append(child)
    return root


def _flatten_tree(root):
    """Binary tree -> null-padded level-order array (see `_build_tree`), trailing
    `None`s trimmed. Duck-typed on `val`/`left`/`right` (see `ListNode`)."""
    if root is None:
        return []
    out = []
    seen = set()  # a buggy solution's cycle shouldn't hang the harness
    queue = deque([root])
    while queue:
        node = queue.popleft()
        if node is None:
            out.append(None)
            continue
        if id(node) in seen:
            break  # cyclic structure — stop rather than loop forever
        seen.add(id(node))
        out.append(getattr(node, "val", None))
        queue.append(getattr(node, "left", None))
        queue.append(getattr(node, "right", None))
    while out and out[-1] is None:
        out.pop()
    return out


def _build_each(build_one):
    """Lift a single-node builder to a `List[...]` one: each element of the
    JSON array is built independently by `build_one` (e.g. each sub-array of
    a `List[ListNode]` input is its own flat-array-to-linked-list build)."""
    def build(values):
        return [build_one(v) for v in (values or [])]
    return build


def _flatten_each(flatten_one):
    """Lift a single-node flattener to a `List[...]` one — see `_build_each`."""
    def flatten(nodes):
        return [flatten_one(n) for n in (nodes or [])]
    return flatten


def _build_cyclic_list(payload):
    """`[values, pos]` -> a genuine cyclic linked list (the wire encoding for
    cycle-detection problems). `pos` is the index
    in `values` the last node's `next` should point back to, or `-1` for no
    cycle at all. `[]`/falsy `payload` -> `None`.

    Records each node's original index as `node._idx` (not in a shared/global
    map — the attribute lives on the node object itself, so it survives the
    user's code reassigning local variables) so a `"CyclicListNode"`-typed
    *return value* can later be identified by identity, not value — see
    `_encode_cyclic_node` for why value alone is ambiguous here.
    """
    values, pos = payload if payload else ([], -1)
    nodes = [ListNode(v) for v in values]
    for i, node in enumerate(nodes):
        node._idx = i
        if i + 1 < len(nodes):
            node.next = nodes[i + 1]
    if nodes and pos != -1:
        nodes[-1].next = nodes[pos]
    return nodes[0] if nodes else None


def _encode_cyclic_node(node):
    """A returned node -> its original index in `values` (identity-based, via
    the `_idx` attribute `_build_cyclic_list` stamped on each node), or `None`
    for no node / a node the harness didn't build itself (e.g. a submission
    that constructs a fresh node instead of returning one from the input
    graph — correctly scored as not matching whatever index was expected).

    Index-by-identity, not by `.val`, is the whole point of this codec:
    `values` can repeat (e.g. `[1, 2, 1, 2, 1]`), so "a node with value 1" is
    ambiguous — there could be two of them — but "the 3rd node built from
    `values`" isn't. A grader running in the same process as the reference
    solution could compare node references directly; this harness can't do
    that across the JSON test-case boundary, so it reconstructs identity
    through the index it already tagged at build time.
    """
    return getattr(node, "_idx", None)


class RandomListNode:
    """A linked-list node with a second pointer, `.random`, that can target any
    node in the list (or none) — the input to a "deep-copy this list" problem.
    Same duck-typing convention as `ListNode`/`TreeNode`: the user's submission
    defines its own class of the same shape, matched by attribute name."""

    def __init__(self, val=0, next=None, random=None):
        self.val = val
        self.next = next
        self.random = random


def _build_random_list(payload):
    """`[[val, random_index], ...]` -> a linked list with `.random` pointers
    (the wire encoding for `"RandomListNode"`).
    `random_index` is the index *into this same array* the node's `.random`
    should point to, or `None` for no random pointer. `[]`/falsy `payload` ->
    `None`.

    Two passes, like `_build_cyclic_list`: first chain `.next` sequentially in
    array order (a straight line — this problem's list is never itself
    cyclic), then a second pass resolves each `.random` now that every node
    exists, since a node's random target can be any index including one later
    in the array.
    """
    payload = payload or []
    nodes = [RandomListNode(v) for v, _ in payload]
    for i, node in enumerate(nodes):
        if i + 1 < len(nodes):
            node.next = nodes[i + 1]
    for node, (_, random_index) in zip(nodes, payload):
        if random_index is not None:
            node.random = nodes[random_index]
    return nodes[0] if nodes else None


def _encode_random_list(head):
    """A returned `RandomListNode` list -> `[[val, random_index], ...]`.

    Unlike `_encode_cyclic_node`, this can't identify nodes via an `_idx`
    stamp from build time: a deep-copy problem's whole point is
    that the submission returns a *fresh* deep copy, not any node from the
    input graph, so nothing the harness stamped on the input survives into
    the answer. The encoder instead re-derives indices purely from the
    returned graph's own shape: walk `.next` from `head` (the same cycle-safe
    id-`seen` guard as `_flatten_list`, since a buggy submission could return
    something cyclic) to fix a traversal order, then map each node's
    `.random` to that node's position in the *same* walk. A `.random` that
    doesn't land on any node from this traversal (a submission bug — e.g. it
    points outside the returned list) encodes as `None` rather than crashing.
    """
    order = []
    seen = set()
    node = head
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        order.append(node)
        node = getattr(node, "next", None)
    index_by_id = {id(n): i for i, n in enumerate(order)}
    out = []
    for n in order:
        random_node = getattr(n, "random", None)
        out.append([getattr(n, "val", None), index_by_id.get(id(random_node))])
    return out


class GraphNode:
    """A graph node — a value plus a list of neighbor nodes
    (rather than a single `.next`/`.left`/`.right`, this node's own field
    holds references to arbitrary *other* nodes). Same duck-typing convention
    as `ListNode`/`TreeNode`/`RandomListNode`: the user's submission defines
    its own class of the same shape, matched by attribute name."""

    def __init__(self, val=0, neighbors=None):
        self.val = val
        self.neighbors = neighbors if neighbors is not None else []


def _build_graph(adj_list):
    """Adjacency-list wire encoding -> a real node graph.
    `adj_list[i]` is the list of neighbor *values* for the node valued
    `i + 1` (1-indexed by value, not by array position). `[]`/falsy
    `adj_list` -> `None` (a genuinely empty graph), so a function typed
    `(node: Optional[GraphNode]) -> Optional[GraphNode]` sees `None`; a single
    isolated node is `[[]]` (one index, with an empty neighbor list), not `[]`.

    Two passes, like `_build_cyclic_list`/`_build_random_list`: first create
    one `GraphNode(val=i + 1)` per adjacency-list index, then a second pass
    resolves each node's `.neighbors` by looking up `nodes[v - 1]` for every
    neighbor value `v` — needed (not just convenient) because a node's
    neighbor list can reference any other index, including ones built later
    in the first pass.
    """
    adj_list = adj_list or []
    nodes = [GraphNode(i + 1) for i in range(len(adj_list))]
    for node, neighbor_values in zip(nodes, adj_list):
        node.neighbors = [nodes[v - 1] for v in neighbor_values]
    return nodes[0] if nodes else None


def _encode_graph(node):
    """A returned `GraphNode` graph -> the adjacency-list wire format (see
    `_build_graph`).

    Unlike `_encode_cyclic_node`, can't identify nodes via an `_idx` stamp
    from build time: a graph deep-copy's whole point is that the submission returns
    a *fresh* deep copy, not any node from the input graph, so nothing
    stamped at build time survives into the answer. Unlike
    `_encode_random_list`, also can't re-derive position from traversal
    order: a `RandomListNode` list has one unambiguous `.next` chain whose
    walk order *is* the wire index, but a graph has no intrinsic traversal
    order — the wire format's row for a node is keyed by that node's own
    `.val`, never by when it was visited.

    So instead: BFS from `node`, collecting every reachable node by identity
    (the same cycle-safe `seen`-id-set pattern as `_flatten_tree`/
    `_encode_random_list` — and load-bearing here even for a *correct*
    submission, unlike the other codecs, since a correctly cloned graph is
    inherently cyclic; that's the entire reason the clone algorithm needs a
    visited map, so a naive unbounded traversal would hang on correct output,
    not just buggy output). Then build the output array sized to the largest
    `.val` seen, with `output[node.val - 1]` set to that node's neighbor
    values — a node's position in the output always comes from its declared
    value, never from visit order.
    """
    if node is None:
        return []
    seen_ids = {id(node)}
    visited = [node]
    queue = deque([node])
    while queue:
        current = queue.popleft()
        for neighbor in getattr(current, "neighbors", None) or []:
            if id(neighbor) not in seen_ids:
                seen_ids.add(id(neighbor))
                visited.append(neighbor)
                queue.append(neighbor)
    max_val = max((getattr(n, "val", 0) or 0 for n in visited), default=0)
    out = [[] for _ in range(max_val)]
    for n in visited:
        v = getattr(n, "val", None)
        if isinstance(v, int) and 1 <= v <= max_val:
            out[v - 1] = [getattr(nb, "val", None) for nb in (getattr(n, "neighbors", None) or [])]
    return out


class Iterator:
    """A minimal iterator interface: `hasNext()`/`next()` over a fixed
    sequence. The harness pre-builds one of these from a flat `List[int]` and
    hands it to the submission's constructor (for an `"Iterator"`-typed param);
    the submission never constructs an `Iterator` itself, only wraps one it's
    given. A list + index cursor is the whole
    implementation — there's no state beyond "where am I in the sequence"."""

    def __init__(self, nums=None):
        self._values = list(nums or [])
        self._index = 0

    def hasNext(self):
        return self._index < len(self._values)

    def next(self):
        value = self._values[self._index]
        self._index += 1
        return value


def _build_iterator(nums):
    """Flat `List[int]` -> a pre-bound `Iterator` positioned at the start —
    the wire encoding for a `kind: "operations"` constructor arg typed
    `"Iterator"` (`arg_lists[0] == [nums]`, same shape as a `TreeNode`- or
    `ListNode`-typed constructor's `[root]`/`[head]`). `None`/falsy `nums` -> an empty,
    exhausted iterator, same as `Iterator()`'s own default."""
    return Iterator(nums)


def _encode_iterator(_value):
    """Never actually called: `"Iterator"` can only appear in `params`
    (a constructor arg) — `ReturnType` (backend/app/schemas/problem.py) has no
    `"Iterator"` member, and `kind: "operations"` doesn't use `return_type` at
    all. Kept only so `_CODECS` stays a uniform 2-tuple like every other entry;
    raises instead of silently no-op-passing-through an `Iterator` object (which
    JSON can't serialize anyway), so a future encode-path bug fails loudly
    rather than shipping a broken result."""
    raise NotImplementedError("Iterator has no return-direction codec")


_CODECS = {
    "ListNode": (_build_list, _flatten_list),
    "TreeNode": (_build_tree, _flatten_tree),
    "List[ListNode]": (_build_each(_build_list), _flatten_each(_flatten_list)),
    "List[TreeNode]": (_build_each(_build_tree), _flatten_each(_flatten_tree)),
    "CyclicListNode": (_build_cyclic_list, _encode_cyclic_node),
    "RandomListNode": (_build_random_list, _encode_random_list),
    "GraphNode": (_build_graph, _encode_graph),
    "Iterator": (_build_iterator, _encode_iterator),
}


def _decode_args(args, params):
    """Convert each arg whose declared `params[i].type` names a codec."""
    args = list(args)
    for i, p in enumerate(params or []):
        codec = _CODECS.get(p.get("type"))
        if codec and i < len(args):
            args[i] = codec[0](args[i])
    return args


def _encode_result(value, return_type):
    """Convert a function's return value back to JSON if `return_type` names a
    codec; otherwise pass it through unchanged (today's behavior)."""
    codec = _CODECS.get(return_type)
    return codec[1](value) if codec else value


# --- operations mode (design/class-replay problems, DESIGN.md §12) ---------
# A test case's `input` is `[ops, args]` — `ops[0]` is the class name (e.g.
# ["VendingMachine","insertCoin","select"] / [[items],[25],["cola"]]), `args` the
# parallel per-op positional args. `expected` is the
# parallel per-op result list, with `expected[0]` always null (the constructor
# has no return value). One test case is one full instantiate-and-replay trace.


def _run_operations(cls, ops, arg_lists, params):
    """Instantiate `cls` via the first op's args, then call each subsequent op
    as a method on that instance. Returns `(instance, results)` — the per-op
    result list (the constructor's own slot is always None, matching the wire
    convention), plus the live instance itself so a `custom_validator` (below)
    can make further calls beyond this replay (e.g. a round-trip check).

    `params` describes the *constructor's* parameters (same convention as
    function mode, where `params` describes the one function) and is run
    through `_decode_args` so a `ListNode`/`TreeNode`-typed constructor arg
    (e.g. a `TreeIterator(root: TreeNode)` constructor) is built before the call. Method
    calls after the constructor are never decoded — there's no per-method
    param typing in the schema, and no current problem needs it.

    Any failure here — a misspelled op name (`AttributeError`), a user bug, a
    mismatched `ops`/`args` length — propagates up and is caught by `run`'s
    existing per-case exception handling, same as a function-mode call
    raising: the whole test case becomes `runtime_error`, not a partial result.
    """
    instance = cls(*_decode_args(arg_lists[0] if arg_lists else [], params))
    results = [None]
    for op, op_args in zip(ops[1:], arg_lists[1:]):
        results.append(getattr(instance, op)(*op_args))
    return instance, results


# --- comparison modes (DESIGN.md §5.4) -------------------------------------

def _is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _float_equal(a, b, eps):
    if _is_number(a) and _is_number(b):
        if math.isnan(a) and math.isnan(b):
            return True
        return abs(a - b) <= eps
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_float_equal(x, y, eps) for x, y in zip(a, b))
    return a == b


def _hashable_key(value):
    """Recursively convert `value` to something hashable, for `_multiset_equal`'s
    `Counter` fast path below. A list (a "list of groups" answer, or a row of
    cells) becomes a tuple of its own converted
    elements, so nesting doesn't defeat the fast path; anything already
    hashable passes through unchanged. Raises `TypeError` (same as bare
    `hash()` would) if some element still isn't hashable after conversion
    (e.g. a dict) — the caller falls back to its O(n²) loop in that case.
    """
    if isinstance(value, list):
        return tuple(_hashable_key(v) for v in value)
    return value


def _multiset_equal(a, b):
    """Order-insensitive equality at the top level, tolerant of unhashable items.

    Fast path: if every element of `a`/`b` is hashable (after `_hashable_key`
    converts any nested list to a tuple), compare `Counter`s — O(n). This
    matters because `compare()` runs *untimed* (deliberately, so a fixed
    harness routine can't itself be blamed for a time_limit_exceeded — see the
    "custom validator mode" section below) but is still bounded by the
    worker's own outer wall-clock kill (worker/judging.py's `run_judgement`).
    The naive O(n²) loop below — remove-by-value from a shrinking list — takes
    tens of seconds once n reaches the tens of thousands (an `unordered`-mode
    hidden case with a ~100k-element expected list is realistic), which blows
    that wall clock and reports a *correct* answer as a container-level
    failure instead of `accepted`.

    Falls back to the O(n²) loop only when an element still isn't hashable
    after conversion (e.g. a dict) — correct for any equality-comparable type,
    just slow; no seed problem's `unordered` cases hit that today.
    """
    if not isinstance(a, list) or not isinstance(b, list):
        return a == b
    if len(a) != len(b):
        return False
    try:
        return Counter(_hashable_key(x) for x in a) == Counter(_hashable_key(x) for x in b)
    except TypeError:
        pass  # an element unhashable even after conversion — fall through
    remaining = list(b)
    for item in a:
        for i, candidate in enumerate(remaining):
            if candidate == item:
                del remaining[i]
                break
        else:
            return False
    return True


def compare(actual, expected, comparison):
    """Decide whether `actual` matches `expected` under the problem's compare mode.

    The mode comes from the problem's `comparison` config so different problems can
    judge correctly: `exact` (default, `==`), `unordered` (multiset equality — order
    doesn't matter), `float_tolerance` (within epsilon — for floating-point answers),
    or `any_of` (matches any of several acceptable answers). Unknown modes fall back
    to strict equality, the safe default.
    """
    mode = (comparison or {}).get("mode", "exact")
    if mode == "unordered":
        return _multiset_equal(actual, expected)
    if mode == "float_tolerance":
        return _float_equal(actual, expected, (comparison or {}).get("epsilon", 1e-6))
    if mode == "any_of":
        candidates = expected if isinstance(expected, list) else [expected]
        return any(actual == candidate for candidate in candidates)
    # "exact" and any unknown mode fall back to strict equality.
    return actual == expected


# --- custom validator mode (DESIGN.md §5.4) ---------------------------------
# `comparison.mode == "custom_validator"` bypasses `compare()` entirely: the
# problem author supplies `validator_code`, a Python script defining
# `def validate(actual, expected, args, instance=None) -> bool`. Loaded once
# (like `user_code`) before the test-case loop, then called per case instead
# of `compare()`. Runs in the same sandboxed container as the submission
# itself — DESIGN.md §5.8's containment is per-container, not per-script, and
# problem authors are the operator (trusted: problems load only through the
# operator-run `app.cli seed`, DESIGN.md §7.1), so this needs no
# separate trust boundary from the rest of the harness.
#
# `instance` is the live `kind: "operations"` object (None for `kind:
# "function"`), letting a validator make *further* calls beyond the harness's
# own replay — e.g. a round-trip check (`instance.decode(actual[-1]) ==
# original_url`) or a statistical property that needs many extra calls
# (`pickIndex()`/`randPoint()`-style problems). `args` is the test case's input
# exactly as it was *before* the submission ran — a separate deep copy from the
# one the submission received — so a validator can check structure against the
# original input (e.g. "same multiset as the input" without a fixed
# `expected`). It has to be a separate copy: a submission that overwrites its
# own input and returns it would otherwise make any such check compare the
# answer with itself.
#
# Any exception raised while `validate()` runs is reported as `judge_error`
# (see `except ValidatorError` in `run()`) — including one that surfaces from
# a call the validator made *into the submission* (e.g. `instance.decode(...)`
# raising because the submission's own `encode` was broken). The harness has
# no way to attribute such an exception to the validator's own logic vs. the
# submission it's exercising, and doesn't try to; a validator whose extra
# calls can raise on a broken submission should catch that itself and return
# `False` if a clean "wrong answer" is preferred over "judge error".


def _load_validator(validator_code):
    """Compile+exec a `custom_validator` script once, returning its `validate`
    function. Raises `ValidatorError` on a bad script (syntax error, missing
    `validate`) — a problem-authoring bug, so callers must not attribute it to
    the submission (no `runtime_error`)."""
    namespace = {}
    try:
        exec(compile(validator_code, "<validator>", "exec"), namespace)
    except BaseException as exc:
        raise ValidatorError("failed to load: %s" % exc) from exc
    validate_fn = namespace.get("validate")
    if not callable(validate_fn):
        raise ValidatorError("custom validator must define a 'validate' function")
    return validate_fn


# --- traceback filtering ----------------------------------------------------

def _format_user_traceback(exc):
    """Format a traceback showing only frames from the user's code.

    Frames from the harness itself are filtered out (kept only if their filename is
    `USER_FILENAME`), so a runtime error shows the user *their* stack — not our
    plumbing — and doesn't reveal harness internals.
    """
    frames = [f for f in traceback.extract_tb(exc.__traceback__)
              if f.filename == USER_FILENAME]
    parts = []
    if frames:
        parts.append("Traceback (most recent call last):\n")
        parts.extend(traceback.format_list(frames))
    parts.extend(traceback.format_exception_only(type(exc), exc))
    return "".join(parts)


def _error_result(test_case_id, exc, stdout=""):
    return {
        "test_case_id": test_case_id,
        "status": "runtime_error",
        "runtime_ms": 0,
        "output": None,
        "stdout": _truncate(stdout),
        "error": _truncate(_format_user_traceback(exc)),
    }


# --- execution --------------------------------------------------------------

def run(payload):
    """Compile the user's code once, then run it against each test case.

    The flow: `compile`/`exec` the submission into a fresh namespace (a top-level
    error — syntax, import — becomes a single `runtime_error`), look up the target
    function (or, for `kind == "operations"`, the target class), and — for
    `comparison.mode == "custom_validator"` — load the validator too (also once,
    also fail-fast, but as `judge_error` rather than `runtime_error`: a bad
    validator is a problem-authoring bug, never the submitter's). Then loop the
    cases. Per case we deep-copy the input (so one case mutating its args can't
    leak into the next), arm a `SIGALRM` for the per-case time limit, capture
    stdout, invoke the user's code, and decide pass/fail — via the validator if
    one was loaded, else `compare()`.

    By default every case runs (to report an honest X/N passed); a problem can opt
    into `stop_on_first_failure` to save compute. Either way total time is bounded
    by the per-case SIGALRM here plus the worker's outer wall-clock kill.
    """
    kind = payload.get("kind", "function")
    function_name = payload.get("function_name")
    class_name = payload.get("class_name")
    user_code = payload["user_code"]
    test_cases = payload.get("test_cases", [])
    comparison = payload.get("comparison", {"mode": "exact"})
    params = payload.get("params", [])
    return_type = payload.get("return_type", "")
    time_limit_ms = int(payload.get("time_limit_ms", 2000))
    time_limit_s = max(time_limit_ms, 1) / 1000.0
    # Default: run every case (for an X/N passed count). A problem can opt into
    # fail-fast to save compute on obviously-wrong solutions. Total time is bounded
    # either way by the per-case SIGALRM and the worker's wall-clock kill.
    stop_on_first_failure = bool(payload.get("stop_on_first_failure", False))

    first_id = test_cases[0].get("id", 0) if test_cases else 0
    real_stdout = sys.stdout

    # Compile + exec the user module with stdout captured. ListNode/TreeNode/
    # RandomListNode/GraphNode/Iterator are pre-bound in the namespace
    # (by convention starter_code/solutions show them only as a comment, never
    # live code) so a type hint like `head: ListNode` resolves
    # at def-time, and code that constructs a new node works even if the user
    # deleted the reference comment. Harmless for problems that never touch
    # any of these names. CyclicListNode isn't in this dict — it's a
    # wire-format tag, not its own class; nodes built under that codec are
    # plain ListNode instances.
    namespace = {
        "ListNode": ListNode, "TreeNode": TreeNode, "RandomListNode": RandomListNode,
        "GraphNode": GraphNode, "Iterator": Iterator,
    }
    sys.stdout = io.StringIO()
    try:
        exec(compile(user_code, USER_FILENAME, "exec"), namespace)
    except BaseException as exc:  # any top-level failure → single runtime_error
        return [_error_result(first_id, exc)]
    finally:
        sys.stdout = real_stdout

    if kind == "operations":
        cls = namespace.get(class_name)
        if not callable(cls):
            return [{
                "test_case_id": first_id,
                "status": "runtime_error",
                "runtime_ms": 0,
                "output": None,
                "stdout": "",
                "error": "Class '%s' not found" % class_name,
            }]
    else:
        func = namespace.get(function_name)
        if not callable(func):
            return [{
                "test_case_id": first_id,
                "status": "runtime_error",
                "runtime_ms": 0,
                "output": None,
                "stdout": "",
                "error": "Function '%s' not found" % function_name,
            }]

    # Load the custom validator once, alongside the user's code — a bad
    # validator (problem-authoring bug) fails fast as a single judge_error
    # result, distinct from the submission's own runtime_error above.
    validate_fn = None
    if (comparison or {}).get("mode") == "custom_validator":
        try:
            validate_fn = _load_validator(comparison.get("validator_code", ""))
        except ValidatorError as exc:
            return [{
                "test_case_id": first_id,
                "status": "judge_error",
                "runtime_ms": 0,
                "output": None,
                "stdout": "",
                "error": _truncate("custom validator: %s" % exc),
            }]

    results = []
    for tc in test_cases:
        tc_id = tc.get("id", 0)
        args = copy.deepcopy(tc.get("input", []))  # user mutation can't leak across cases
        # A second, untouched copy for the validator: the submission can mutate
        # `args` in place, and a validator comparing against *that* would be
        # checking the answer against whatever the submission wrote there.
        # Taken before the timer starts so its cost isn't billed to the user.
        validator_args = copy.deepcopy(args) if validate_fn is not None else None
        expected = tc.get("expected")

        buffer = io.StringIO()
        sys.stdout = buffer
        signal.setitimer(signal.ITIMER_REAL, time_limit_s)
        start = time.perf_counter()
        instance = None
        try:
            # Decode/encode inside the timed try so a codec bug reports as this
            # case's runtime_error rather than crashing the whole payload. A
            # custom validator's extra calls (e.g. a round trip via `instance`,
            # or a many-call statistical check) run under the same SIGALRM here
            # too, so a runaway validator reports time_limit_exceeded rather
            # than hanging the container — but `compare()` (below, after the
            # timer is disarmed) deliberately stays untimed: it's fixed harness
            # logic, not problem-authored code that could hang or run away.
            if kind == "operations":
                ops, arg_lists = args
                instance, actual = _run_operations(cls, ops, arg_lists, params)
            else:
                actual = _encode_result(func(*_decode_args(args, params)), return_type)
            if validate_fn is not None:
                try:
                    passed = bool(validate_fn(actual=actual, expected=expected,
                                               args=validator_args, instance=instance))
                except TimeLimitExceeded:
                    raise
                except BaseException as exc:  # noqa: BLE001 - validator may raise anything
                    raise ValidatorError(str(exc)) from exc
        except TimeLimitExceeded:
            signal.setitimer(signal.ITIMER_REAL, 0)
            sys.stdout = real_stdout
            results.append({
                "test_case_id": tc_id,
                "status": "time_limit_exceeded",
                "runtime_ms": time_limit_ms,
                "output": None,
                "stdout": _truncate(buffer.getvalue()),
                "error": None,
            })
            if stop_on_first_failure:
                break
            continue
        except ValidatorError as exc:
            # The problem's validator itself failed — not the submission's
            # fault, so this is judge_error, not runtime_error (AGENTS.md).
            # Only raised after `actual` is already computed (see above), so
            # it's always safe to include in the result.
            elapsed_ms = round((time.perf_counter() - start) * 1000, 3)
            signal.setitimer(signal.ITIMER_REAL, 0)
            sys.stdout = real_stdout
            results.append({
                "test_case_id": tc_id,
                "status": "judge_error",
                "runtime_ms": elapsed_ms,
                "output": _truncate(_format_output(actual)),
                "stdout": _truncate(buffer.getvalue()),
                "error": _truncate("custom validator: %s" % exc),
            })
            if stop_on_first_failure:
                break
            continue
        except BaseException as exc:  # noqa: BLE001 - user code may raise anything
            elapsed_ms = round((time.perf_counter() - start) * 1000, 3)
            signal.setitimer(signal.ITIMER_REAL, 0)
            sys.stdout = real_stdout
            results.append({
                "test_case_id": tc_id,
                "status": "runtime_error",
                "runtime_ms": elapsed_ms,
                "output": None,
                "stdout": _truncate(buffer.getvalue()),
                "error": _truncate(_format_user_traceback(exc)),
            })
            if stop_on_first_failure:
                break
            continue

        elapsed_ms = round((time.perf_counter() - start) * 1000, 3)
        signal.setitimer(signal.ITIMER_REAL, 0)
        sys.stdout = real_stdout
        # A validator (if loaded) already decided `passed` above, inside the
        # timed block. Otherwise `compare()` runs here, same as always —
        # untimed, after the SIGALRM is disarmed (unchanged by
        # custom_validator's addition; see the comment above the try block).
        if validate_fn is None:
            passed = compare(actual, expected, comparison)
        results.append({
            "test_case_id": tc_id,
            "status": "passed" if passed else "wrong_answer",
            "runtime_ms": elapsed_ms,
            "output": _truncate(_format_output(actual)),
            "stdout": _truncate(buffer.getvalue()),
            "error": None,
        })
        if not passed and stop_on_first_failure:
            break

    return results


def main():
    signal.signal(signal.SIGALRM, _alarm_handler)
    # Payload channel: stdin by default (the `docker run -i` path). Under
    # Kubernetes there's no stdin pipe, so the runner mounts the payload as a file
    # and points JUDGE_PAYLOAD_FILE at it. Same JSON either way.
    import os
    payload_file = os.environ.get("JUDGE_PAYLOAD_FILE")
    raw = open(payload_file, encoding="utf-8").read() if payload_file else sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write("harness: invalid payload JSON: %s\n" % exc)
        sys.exit(2)
    results = run(payload)
    sys.stdout.write(json.dumps({"results": results}))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
