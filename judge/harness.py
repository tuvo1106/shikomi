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
"custom validator mode" section below. An operations case may carry `probes`,
extra calls the harness makes after the replay for such a validator to check
(the "probes" section).

Everything the user prints is captured into a per-case buffer so it can never
corrupt the stdout protocol. Stdlib only — this must run inside a network-less,
read-only container with nothing installed beyond CPython.
"""
import copy
import inspect
import io
import json
import math
import os
import select
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


class MalformedResult(ValueError):
    """A returned list or tree that isn't one: it has a cycle, or (for a tree) a
    node reachable along two paths. The name is what the user sees in front of
    the message (`_format_user_traceback`), so it reads as a diagnosis."""


def _walk_next(head):
    """The nodes reachable from `head` along `.next`, in order.

    Shared by every encoder that walks a list (`_flatten_list`,
    `_encode_random_list`), so the cycle refusal and its message live in one place.

    Raises:
        MalformedResult: the list loops back on itself. Stopping quietly at the
            repeated node (the old behavior) could turn a wrong answer into a
            right one: a solution that forgets to clear its last node's `next`
            produces a cycle whose truncation equals the expected list exactly.
    """
    order = []
    seen = set()  # a buggy solution's cycle must neither hang the harness nor pass
    while head is not None:
        if id(head) in seen:
            raise MalformedResult(
                f"the returned list has a cycle: after {len(order)} node(s), a `next` "
                "points back to an earlier node (did you forget to set the last node's "
                "`next` to None?)")
        seen.add(id(head))
        order.append(head)
        head = getattr(head, "next", None)
    return order


def _flatten_list(node):
    """Linked list -> flat array, walking `.next` by duck-typed attribute
    access so it works on the user's own `ListNode` instances too. A cycle is
    refused (`_walk_next`); harness_rs can't get one, since a `Box` list can't
    be cyclic in safe Rust."""
    return [getattr(n, "val", None) for n in _walk_next(node)]


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


# The most entries an encoded tree may have, and the largest value a returned graph
# node may carry (harness_rs `nodes::MAX_ENCODED` matches). A tree may share subtrees, which can make its encoding exponentially
# larger than its node count; a runaway answer fails with a message instead of
# exhausting the sandbox's memory. Real answers are far smaller.
_MAX_TREE_ENTRIES = 2_000_000


def _tree_has_cycle(root):
    """Whether some node is its own ancestor: a three-colour depth-first search,
    iterative (trees in stress cases are deep), where reaching a node that's
    still on the current path is a cycle. A node reached twice along
    *different* paths (a shared subtree) is not one."""
    on_path, done = set(), set()
    stack = [(root, False)]
    while stack:
        node, leaving = stack.pop()
        if leaving:
            on_path.discard(id(node))
            done.add(id(node))
            continue
        if id(node) in done:
            continue
        if id(node) in on_path:
            return True
        on_path.add(id(node))
        stack.append((node, True))
        for child in (getattr(node, "right", None), getattr(node, "left", None)):
            if child is not None:
                stack.append((child, False))
    return False


def _flatten_tree(root):
    """Binary tree -> null-padded level-order array (see `_build_tree`), trailing
    `None`s trimmed. Duck-typed on `val`/`left`/`right` (see `ListNode`).

    Raises:
        MalformedResult: the tree has a cycle (a child pointing back to an
            ancestor), which has no finite encoding; stopping at the repeated
            node could turn a wrong answer into a right one, as `_flatten_list`
            explains. A subtree *shared* by two parents is accepted: it's a
            finite tree with a clean encoding, and memoized solutions ("all full
            binary trees of size n") build them on purpose. Also raised past
            `_MAX_TREE_ENTRIES`.
    """
    if root is None:
        return []
    if _tree_has_cycle(root):
        raise MalformedResult(
            "the returned tree has a cycle: a child points back to one of its ancestors")
    out = []
    queue = deque([root])
    while queue:
        if len(out) >= _MAX_TREE_ENTRIES:
            raise MalformedResult(
                f"the returned tree is too large to encode (over {_MAX_TREE_ENTRIES} entries)")
        node = queue.popleft()
        if node is None:
            out.append(None)
            continue
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
    returned graph's own shape: walk `.next` from `head` (refusing a `next`
    chain that loops, as `_flatten_list` does) to fix a traversal order, then map each node's
    `.random` to that node's position in the *same* walk. A `.random` that
    doesn't land on any node from this traversal (a submission bug — e.g. it
    points outside the returned list) encodes as `None` rather than crashing.
    """
    order = _walk_next(head)
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
    # Rows are keyed by value, so the largest value sizes the output; a buggy clone
    # can set any value, and a huge one would exhaust memory (harness_rs matches).
    if max_val > _MAX_TREE_ENTRIES:
        raise MalformedResult(f"the returned graph has a node valued {max_val}, too large to encode")
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
    convention), plus the live instance itself so a case's probes (below) can
    make further calls on it after this replay (e.g. a round-trip check).

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


# --- probes: extra calls the judge makes to check a result -------------------
# Some properties can't be read off one replay: that `decode(encode(x)) == x`, or
# that `pickIndex()` follows its weights over thousands of calls. Those calls
# need the live instance, which exists only in the child, next to the submission,
# but the validator that judges them must run in the trusted parent (see "custom
# validator mode" below). So the calls are test-case data instead (docs/adr/0007):
# after the replay, the child makes each probe's call on the same instance and
# returns the results as `probe_results`, separate from `actual`.
#
# A probe is `{"op": name, "args": [...], "refs": {"<arg index>": <op index>},
# "repeat": n}`; only `op` is required. `refs` fills an argument with the result
# of one of the case's own ops (`actual[i]`), for a round trip: `{"op": "decode",
# "args": [null], "refs": {"0": 1}}` decodes whatever op 1 returned. `repeat`
# makes the same call n times, each result its own entry. `app.schemas.problem`
# checks all of this at seed time.


class _ProbeFailed(Exception):
    """A submission method raised during a probe call. Carries which call it was,
    because the user never wrote that call and won't find it in their test case."""

    def __init__(self, call):
        super().__init__(call)
        self.call = call


def _run_probes(instance, probes, actual):
    """Make every probe's call(s) on `instance`, in order, and return the flat list
    of their results.

    `actual` must be the JSON snapshot the child reports (see `_execute_case`), not
    the live values: `refs` read from it, so a probe is fed exactly the value the
    parent and the validator see, and never has to copy an object the submission
    returned (which might not be copyable).

    Raises:
        _ProbeFailed: the submission's method raised (its bug: a runtime_error).
        ValidatorError: a probe is malformed, e.g. a `refs` index past the ops
            (an authoring bug that seed validation should have caught: a judge_error).
        TimeLimitExceeded: propagated untouched; probes run inside the case's limit.
    """
    total = _probe_count(probes)
    results = []
    for probe in probes:
        op = probe["op"]
        args = list(probe.get("args", []))
        try:
            for index, source in (probe.get("refs") or {}).items():
                args[int(index)] = actual[source]
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise ValidatorError("malformed probe %r: %s" % (probe, exc)) from exc
        for _ in range(probe.get("repeat", 1)):
            # A fresh copy per call, so a method that mutates its argument can't
            # change what the next repeat (or the snapshot behind a ref) holds.
            # Cheap: seed validation keeps `refs` to single calls, so a repeated
            # probe's arguments are the small literals the author wrote.
            call_args = copy.deepcopy(args)
            try:
                results.append(getattr(instance, op)(*call_args))
            except TimeLimitExceeded:
                raise
            except BaseException as exc:  # noqa: BLE001 - user code may raise anything
                raise _ProbeFailed("%s(), call %d of %d" % (op, len(results) + 1, total)) from exc
    return results


def _probe_count(probes):
    """How many results `probes` produce, from the parent's own copy of them."""
    return sum(p.get("repeat", 1) for p in probes or [])


# --- comparison modes (DESIGN.md §5.4) -------------------------------------

def _is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _json_equal(a, b):
    """`a == b` with JSON's types: a bool never equals a number.

    Python's `bool` is an `int`, so plain `==` has `True == 1` and `[False] == [0]`,
    and a submission returning `True` would pass a case expecting `1`. The JS and
    Rust harnesses compare strictly, so every compare mode goes through this to
    judge the same answer the same way in every language. Numbers still compare by
    value across int/float (`2 == 2.0`), as they do elsewhere. Containers recurse;
    a tuple still doesn't equal a list, as with `==`.
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return (type(a) is type(b) and len(a) == len(b)
                and all(_json_equal(x, y) for x, y in zip(a, b)))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_equal(a[k], b[k]) for k in a)
    return a == b


def _float_equal(a, b, eps):
    if _is_number(a) and _is_number(b):
        if math.isnan(a) and math.isnan(b):
            return True
        # Exact equality first: inf - inf is NaN, which no tolerance accepts, but an
        # infinite answer matching an infinite expectation is right. Opposite
        # infinities fall through to abs(-inf - inf) = inf > eps, as they should.
        return a == b or abs(a - b) <= eps
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_float_equal(x, y, eps) for x, y in zip(a, b))
    return _json_equal(a, b)


def _hashable_key(value):
    """Recursively convert `value` to something hashable, for `_multiset_equal`'s
    `Counter` fast path below. A list (a "list of groups" answer, or a row of
    cells) becomes a tuple of its own converted
    elements, so nesting doesn't defeat the fast path; anything already
    hashable passes through unchanged. Raises `TypeError` (same as bare
    `hash()` would) if some element still isn't hashable after conversion
    (e.g. a dict) — the caller falls back to its O(n²) loop in that case.

    A bool is tagged, so `True` and `1` (equal, with equal hashes, in Python) count
    as different elements, matching `_json_equal`.
    """
    if isinstance(value, bool):
        return (_BOOL_TAG, value)
    if isinstance(value, list):
        return tuple(_hashable_key(v) for v in value)
    return value


# Marks a bool inside `_hashable_key`'s output; a private object, so no JSON value
# can produce the same key.
_BOOL_TAG = object()


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
        return _json_equal(a, b)
    if len(a) != len(b):
        return False
    try:
        return Counter(_hashable_key(x) for x in a) == Counter(_hashable_key(x) for x in b)
    except TypeError:
        pass  # an element unhashable even after conversion — fall through
    remaining = list(b)
    for item in a:
        for i, candidate in enumerate(remaining):
            if _json_equal(candidate, item):
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
        return any(_json_equal(actual, candidate) for candidate in candidates)
    # "exact" and any unknown mode fall back to strict equality.
    return _json_equal(actual, expected)


# --- custom validator mode (DESIGN.md §5.4) ---------------------------------
# `comparison.mode == "custom_validator"` bypasses `compare()` entirely: the
# problem author supplies `validator_code`, a Python script defining `validate`.
# Loaded once before the test-case loop, then called per case instead of
# `compare()`. Problem authors are the operator (trusted: problems load only
# through the operator-run `app.cli seed`, DESIGN.md §7.1), so the validator
# itself needs no sandbox of its own — but *where* it runs matters, because its
# verdict is only as trustworthy as the process that computes it.
#
# `def validate(actual, expected, args, probe_results) -> bool` runs in the
# trusted **parent** (`_run_validator`), on the `actual` and `probe_results` the
# child sent back, exactly where `compare()` runs for the fixed-answer modes. The
# child never sees `expected` and never computes a verdict, so a submission can
# neither read the answer nor forge a pass by writing its own frame to the result
# pipe. `probe_results` is the flat list of the case's probe results (see "probes"
# above), `[]` when it has none. A validator without a `probe_results` parameter
# is refused (a judge_error): the older `validate(..., instance=None)` form made
# its extra calls through the live object, so an operations validator had to run
# in the child, where `expected` was readable and the verdict forgeable. Probes
# replaced it (ADR-0007), and seeding refuses it too.
#
# `args` is the test case's input
# exactly as it was *before* the submission ran — a separate deep copy from the
# one the submission received — so a validator can check structure against the
# original input (e.g. "same multiset as the input" without a fixed
# `expected`). It has to be a separate copy: a submission that overwrites its
# own input and returns it would otherwise make any such check compare the
# answer with itself. (In the parent that copy is free: the parent's `input`
# never crossed into the child, so nothing the submission does can touch it.)
#
# A parent-side validator sees `actual` after its JSON round trip through the
# result pipe — a tuple arrives as a list — the same value `compare()` judges
# for every other mode, so all modes agree on what the submission returned.
#
# Any exception raised while `validate()` runs (or loads) is reported as
# `judge_error`: it's the author's code. The user sees only a fixed line; the
# exception's own message goes to stderr (the judge's log), since it could quote
# `expected`, the same rule as the Rust harness. A probe call is made by the
# harness on the submission's object, so an exception there is the submission's
# `runtime_error`, labelled as a call the judge added.


# What the user sees when a custom validator fails to load or raises. Fixed,
# because the exception's own message is the author's code talking and could
# quote `expected`; `_validator_fault` logs the detail to stderr instead.
VALIDATOR_FAULT = "the problem's custom validator failed (a problem bug, not your code)"


def _validator_fault(test_case_id, exc):
    """The judge_error row for a validator that failed, with its detail logged."""
    sys.stderr.write("harness: custom validator %s\n" % exc)
    return _judge_error_result(test_case_id, VALIDATOR_FAULT)


def _load_validator(validator_code):
    """Compile+exec a `custom_validator` script once, returning its `validate`
    function. Raises `ValidatorError` on a bad script (syntax error, missing
    `validate`, no `probe_results` parameter) — a problem-authoring bug, so
    callers must not attribute it to the submission (no `runtime_error`)."""
    namespace = {}
    try:
        exec(compile(validator_code, "<validator>", "exec"), namespace)
    except BaseException as exc:
        raise ValidatorError("failed to load: %s" % exc) from exc
    validate_fn = namespace.get("validate")
    if not callable(validate_fn):
        raise ValidatorError("must define a 'validate' function")
    if not _takes_probe_results(validate_fn):
        raise ValidatorError("'validate' must take 'probe_results' (the older 'instance' "
                             "form is gone; docs/adr/0007-custom-validators-in-every-language.md)")
    return validate_fn


def _takes_probe_results(validate_fn):
    """Whether `validate_fn` can be passed `probe_results` by keyword, as
    `_run_validator` does: a parameter of that name that isn't positional-only,
    or a `**kwargs`. `app.schemas.problem`'s `_validate_params` applies the same
    rule at seed time."""
    try:
        params = inspect.signature(validate_fn).parameters
    except (TypeError, ValueError):  # a callable with no introspectable signature
        return False
    if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return True
    param = params.get("probe_results")
    return param is not None and param.kind != inspect.Parameter.POSITIONAL_ONLY


def _run_validator(validate_fn, actual, expected, args, budget_s, probe_results):
    """Call a validator in the parent and return its verdict.

    `probe_results` is the case's probe results (a list, possibly empty).

    The validator gets whatever is left of the case's time limit after the
    submission's own run (`budget_s`), under the same SIGALRM the child uses —
    so a slow or hanging validator still ends as `time_limit_exceeded` exactly
    as it did when it ran in the child, and a case's total time (submission +
    validator) stays within the `time_limit_ms` that app/judge_budget.py
    budgets for. Without an alarm, a hung validator here would hold the whole
    run until the worker's outer wall-clock kill.

    Raises:
        TimeLimitExceeded: the validator outran `budget_s`.
        ValidatorError: it raised — an authoring bug, reported as judge_error.
    """
    signal.setitimer(signal.ITIMER_REAL, budget_s)
    try:
        return bool(validate_fn(actual=actual, expected=expected, args=args,
                                probe_results=probe_results))
    except TimeLimitExceeded:
        raise
    except BaseException as exc:  # noqa: BLE001 - validator may raise anything
        raise ValidatorError("raised %s: %s" % (type(exc).__name__, exc)) from exc
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


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


# --- execution: a trusted parent and an untrusted child ----------------------
# The submission runs in a separate **child** process, not here. This process
# (the parent) holds every case's `expected`, decides pass/fail, and is the only
# one that writes the `{"results": [...]}` report to stdout. The child receives
# only the submission and each case's *input* — never `expected` — over a pipe,
# and returns the value the submission produced over a second, private pipe.
#
# Why: a submission running in the same process as the grader can read the
# expected answers out of memory and can write a forged report to the real
# stdout. Running it in a child closes both: pass/fail for every fixed-answer
# comparison is computed *here* from an `expected` the child never sees (so a
# forged return value can't match without already knowing the answer), and the
# child's own stdout goes to /dev/null while its results ride a separate pipe
# the parent frames — the child cannot reach the report stream at all. This
# mirrors the Rust harness's per-case process isolation (ADR-0004); Python keeps
# one child for the whole run and only respawns it after a timeout or a crash,
# so the per-case cost is a pipe round trip, not a process launch.
#
# A `custom_validator` runs here in the parent too, so no verdict and no
# `expected` ever come from or go to the child.

CHILD_FLAG = "--child"
_HANG = "__hang__"     # parent sentinel: no reply within the deadline
_CRASH = "__crash__"   # parent sentinel: the child died without replying
_SETUP_GRACE_S = 10.0    # generous ceiling for the one-time compile in the child
_KILL_GRACE_S = 0.5      # extra wall time past the case limit before the parent kills


def _judge_error_result(test_case_id, error):
    return {"test_case_id": test_case_id, "status": "judge_error", "runtime_ms": 0,
            "output": None, "stdout": "", "error": _truncate(error)}


# --- the child: runs the submission, never sees `expected` -------------------

class _Ctx:
    """The child's compiled, per-run state (set up once, reused per case)."""
    __slots__ = ("kind", "func", "cls", "params", "return_type", "time_limit_s", "real_stdout")


def _child_setup(setup):
    """Compile the submission. Returns `(ctx, err)`; exactly one is None. `err`
    is a message dict already shaped for the result pipe (a compile failure →
    the parent's single runtime_error)."""
    ctx = _Ctx()
    ctx.kind = setup.get("kind", "function")
    ctx.params = setup.get("params", [])
    ctx.return_type = setup.get("return_type", "")
    ctx.time_limit_s = max(int(setup.get("time_limit_ms", 2000)), 1) / 1000.0
    ctx.real_stdout = sys.stdout

    namespace = {
        "ListNode": ListNode, "TreeNode": TreeNode, "RandomListNode": RandomListNode,
        "GraphNode": GraphNode, "Iterator": Iterator,
    }
    sys.stdout = io.StringIO()
    try:
        exec(compile(setup["user_code"], USER_FILENAME, "exec"), namespace)
    except BaseException as exc:  # any top-level failure → single runtime_error
        return None, {"setup": "user_error", "error": _format_user_traceback(exc)}
    finally:
        sys.stdout = ctx.real_stdout

    if ctx.kind == "operations":
        ctx.cls = namespace.get(setup.get("class_name"))
        ctx.func = None
        if not callable(ctx.cls):
            return None, {"setup": "user_error", "error": "Class '%s' not found" % setup.get("class_name")}
    else:
        ctx.cls = None
        ctx.func = namespace.get(setup.get("function_name"))
        if not callable(ctx.func):
            return None, {"setup": "user_error", "error": "Function '%s' not found" % setup.get("function_name")}
    return ctx, None


def _execute_case(ctx, message):
    """Run one case in the child and return its outcome (no parent-side compare).

    The submission's `print()` output is captured per case (as before); its raw
    fd-1 writes go to /dev/null (redirected in `_child_main`). On the `ok` path
    the child returns the raw `actual` value for the parent to judge — it never
    sees `expected`, so it cannot decide pass/fail — plus `probe_results` when the
    case has probes, made on the same instance after the replay (and timed with
    it, since they run the submission's code)."""
    args = message["input"]  # a fresh parse each case, so nothing leaks across cases
    probes = message.get("probes")
    probe_results = None

    buffer = io.StringIO()
    sys.stdout = buffer
    signal.setitimer(signal.ITIMER_REAL, ctx.time_limit_s)
    start = time.perf_counter()
    try:
        if ctx.kind == "operations":
            ops, arg_lists = args
            instance, actual = _run_operations(ctx.cls, ops, arg_lists, ctx.params)
            if probes:
                # Snapshot first: an op may return a reference to the instance's
                # own state (`return self.items`), which a probe would then
                # change under `actual`. The JSON round trip is what the parent
                # receives anyway; a value it can't encode is the submission's
                # runtime_error, as it would be at `emit`.
                actual = json.loads(json.dumps(actual))
                probe_results = _run_probes(instance, probes, actual)
        else:
            actual = _encode_result(ctx.func(*_decode_args(args, ctx.params)), ctx.return_type)
    except TimeLimitExceeded:
        signal.setitimer(signal.ITIMER_REAL, 0)
        sys.stdout = ctx.real_stdout
        return {"status": "time_limit_exceeded", "stdout": _truncate(buffer.getvalue())}
    except ValidatorError as exc:  # a malformed probe (`_run_probes`): the author's bug
        signal.setitimer(signal.ITIMER_REAL, 0)
        sys.stdout = ctx.real_stdout
        return {"status": "probe_error", "error": str(exc)}
    except _ProbeFailed as exc:
        elapsed = round((time.perf_counter() - start) * 1000, 3)
        signal.setitimer(signal.ITIMER_REAL, 0)
        sys.stdout = ctx.real_stdout
        return {"status": "runtime_error", "runtime_ms": elapsed, "stdout": _truncate(buffer.getvalue()),
                "error": "In a call the judge added after your operations to check the result "
                         "(%s):\n%s" % (exc.call, _format_user_traceback(exc.__cause__))}
    except BaseException as exc:  # noqa: BLE001 - user code may raise anything
        elapsed = round((time.perf_counter() - start) * 1000, 3)
        signal.setitimer(signal.ITIMER_REAL, 0)
        sys.stdout = ctx.real_stdout
        return {"status": "runtime_error", "runtime_ms": elapsed,
                "stdout": _truncate(buffer.getvalue()), "error": _format_user_traceback(exc)}
    elapsed = round((time.perf_counter() - start) * 1000, 3)
    signal.setitimer(signal.ITIMER_REAL, 0)
    sys.stdout = ctx.real_stdout
    reply = {"status": "ok", "actual": actual, "runtime_ms": elapsed,
             "stdout": _truncate(buffer.getvalue())}
    if probe_results is not None:
        reply["probe_results"] = probe_results
    return reply


def _child_main(result_fd):
    """The child process: set up once, then answer one case per control line.

    Reads the setup message and each case's input from stdin (the parent's
    control channel); writes each outcome to `result_fd` (a private pipe). fd 1
    and fd 2 are pointed at /dev/null first, so a submission that writes straight
    to a raw descriptor can't reach the parent or the report — only this framed
    pipe carries anything the parent reads, and the parent still decides
    pass/fail itself."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.close(devnull)
    out = os.fdopen(result_fd, "w")

    def emit(message):
        out.write(json.dumps(message) + "\n")
        out.flush()

    setup_line = sys.stdin.readline()
    if not setup_line:
        return
    ctx, err = _child_setup(json.loads(setup_line))
    if err is not None:
        emit(err)
        return
    emit({"setup": "ok"})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        emit(_execute_case(ctx, json.loads(line)))


# --- the parent: orchestrates the child and writes the report ----------------

class _Child:
    """A live child process and the pipe the parent reads its results from."""
    __slots__ = ("proc", "reader")

    def __init__(self, proc, reader):
        self.proc, self.reader = proc, reader

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def read(self, deadline_s):
        """Read one framed message. Returns the parsed dict, or a sentinel dict
        `{"status": _HANG}` if the child produced nothing within `deadline_s`
        seconds, or `{"status": _CRASH}` if it closed the pipe first (died) or
        sent a frame that isn't valid JSON.

        A submission's `exec`'d code can write raw bytes to the result fd itself,
        so a malformed frame is treated as a dead, desynced child (_CRASH) rather
        than being allowed to raise out of here and abort the whole run. A
        well-formed forged frame still can't pass a case: the parent alone holds
        `expected` and decides."""
        ready, _, _ = select.select([self.reader], [], [], deadline_s)
        if not ready:
            return {"status": _HANG}
        line = self.reader.readline()
        if not line:
            return {"status": _CRASH}
        try:
            return json.loads(line)
        except ValueError:  # JSONDecodeError → treat the channel as desynced
            return {"status": _CRASH}

    def kill(self):
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.reader.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=1)
        except Exception:  # noqa: BLE001 - best-effort reap
            pass


def _spawn_child(setup):
    """Launch the child harness, hand it the setup, and return a `_Child` once it
    acknowledges (or a message dict if setup failed, so the caller can report it).
    The result pipe's write end is passed to the child by fd number; the parent
    keeps only the read end, so the pipe reports EOF the moment the child dies."""
    import subprocess

    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), CHILD_FLAG, str(write_fd)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        pass_fds=(write_fd,), start_new_session=True, text=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("JUDGE_")},
    )
    os.close(write_fd)  # the child holds the only write end now
    child = _Child(proc, os.fdopen(read_fd, "r"))
    child.send(setup)
    ack = child.read(_SETUP_GRACE_S)
    if ack is None or ack.get("setup") != "ok":
        child.kill()
        return None, ack
    return child, None


def _finalize(tc, reply, comparison, validate_fn=None, time_limit_s=None, probes=None):
    """Turn the child's per-case `reply` (or None for a hang/crash) into the
    report row, computing pass/fail here: with `compare()` for a fixed-answer
    problem, or with the custom validator the parent loaded (`validate_fn`, and
    `probes` the ones this case sent the child)."""
    tc_id = tc.get("id", 0)
    status = reply.get("status")
    if status == _CRASH:
        # The child died without answering — os._exit, a segfault, a killed
        # subprocess. The submission's own doing, so a runtime_error for this case
        # (not a judge fault), and the parent will respawn for the next.
        return {"test_case_id": tc_id, "status": "runtime_error", "runtime_ms": 0, "output": None,
                "stdout": "", "error": "the submission exited before returning a value"}
    if status in (_HANG, "time_limit_exceeded"):
        return {"test_case_id": tc_id, "status": "time_limit_exceeded", "runtime_ms": None,
                "output": None, "stdout": _truncate(reply.get("stdout", "")), "error": None}
    if status == "runtime_error":
        return {"test_case_id": tc_id, "status": "runtime_error", "runtime_ms": reply.get("runtime_ms", 0),
                "output": None, "stdout": _truncate(reply.get("stdout", "")),
                "error": _truncate(reply.get("error", ""))}
    if status == "probe_error":
        # Probes are test data the child already had, so their detail isn't secret
        # (unlike a validator's own exception). A forged frame can claim this too,
        # but only to fail its own case.
        return _judge_error_result(tc_id, reply.get("error", "malformed probe"))

    actual = reply.get("actual")
    if validate_fn is not None:
        # The child's reply is untrusted: a submission can write its own frame. A
        # forged `probe_results` is harmless (anything it claims, the submission's
        # methods could have returned), but a malformed one would reach the
        # validator and surface as a judge_error, blaming the author for the
        # submission's doing. So its shape is checked against the parent's own
        # copy of the probes, and a mismatch is the submission's runtime_error.
        probe_results = reply.get("probe_results") if probes else []
        if not isinstance(probe_results, list) or len(probe_results) != _probe_count(probes):
            return {"test_case_id": tc_id, "status": "runtime_error", "runtime_ms": reply.get("runtime_ms", 0),
                    "output": None, "stdout": _truncate(reply.get("stdout", "")),
                    "error": "the submission's reply didn't account for every check the judge ran"}
        # The remaining share of the case's limit. `runtime_ms` is the child's own
        # report, so clamp it: a lying child can't buy its validator extra time
        # (or none at all, which setitimer would read as "disarm"), and a
        # non-number (a forged frame) mustn't crash the parent.
        runtime_ms = reply.get("runtime_ms")
        runtime_s = runtime_ms / 1000.0 if _is_number(runtime_ms) and math.isfinite(runtime_ms) else 0.0
        budget_s = min(max(time_limit_s - runtime_s, 0.001), time_limit_s)
        try:
            passed = _run_validator(validate_fn, actual, tc.get("expected"),
                                    tc.get("input", []), budget_s, probe_results)
        except TimeLimitExceeded:
            return {"test_case_id": tc_id, "status": "time_limit_exceeded", "runtime_ms": None,
                    "output": None, "stdout": _truncate(reply.get("stdout", "")), "error": None}
        except ValidatorError as exc:
            return {**_validator_fault(tc_id, exc), "runtime_ms": reply.get("runtime_ms", 0),
                    "output": _truncate(_format_output(actual)),
                    "stdout": _truncate(reply.get("stdout", ""))}
    else:
        passed = compare(actual, tc.get("expected"), comparison)
    return {"test_case_id": tc_id, "status": "passed" if passed else "wrong_answer",
            "runtime_ms": reply.get("runtime_ms", 0), "output": _truncate(_format_output(actual)),
            "stdout": _truncate(reply.get("stdout", "")), "error": None}


def run(payload):
    """Judge the submission against every test case, in a child process.

    The parent compiles nothing user-supplied and never runs it: it spawns the
    child (`_spawn_child`), feeds it one case at a time, and for each reply
    computes pass/fail itself — with `compare()` against an `expected` the child
    never received, or with a custom validator it loaded here. A case's probes
    go to the child with its input. A case that hangs past the limit is killed and reported
    `time_limit_exceeded`; a case that crashes the child is reported
    `runtime_error`; either way the parent respawns a fresh child for the
    remaining cases. A top-level compile error (or a bad validator) surfaces from
    setup as a single runtime_error (or judge_error), exactly as before.

    By default every case runs (for an honest X/N); `stop_on_first_failure` ends
    at the first non-passing case.
    """
    kind = payload.get("kind", "function")
    test_cases = payload.get("test_cases", [])
    comparison = payload.get("comparison", {"mode": "exact"})
    time_limit_ms = int(payload.get("time_limit_ms", 2000))
    stop_on_first_failure = bool(payload.get("stop_on_first_failure", False))
    first_id = test_cases[0].get("id", 0) if test_cases else 0

    # A custom validator runs here, in the parent (see "custom validator mode"
    # above). It's loaded before any child starts, so a broken script fails once.
    validate_fn = None
    if (comparison or {}).get("mode") == "custom_validator":
        try:
            validate_fn = _load_validator(comparison.get("validator_code", ""))
        except ValidatorError as exc:
            return [_validator_fault(first_id, exc)]
    setup = {
        "user_code": payload["user_code"],
        "kind": kind,
        "function_name": payload.get("function_name"),
        "class_name": payload.get("class_name"),
        "params": payload.get("params", []),
        "return_type": payload.get("return_type", ""),
        "time_limit_ms": time_limit_ms,
    }
    time_limit_s = max(time_limit_ms, 1) / 1000.0

    deadline_s = max(time_limit_ms, 1) / 1000.0 + _KILL_GRACE_S
    child, err = _spawn_child(setup)
    if child is None:
        message = err.get("error") if err else "the submission could not be started"
        return [{"test_case_id": first_id, "status": "runtime_error", "runtime_ms": 0,
                 "output": None, "stdout": "", "error": _truncate(message)}]

    results = []
    try:
        for tc in test_cases:
            if child is None:  # respawn after a previous kill/crash
                child, err = _spawn_child(setup)
                if child is None:  # a compile that worked once should work again; if not, blame the case
                    message = (err or {}).get("error", "the submission could not be started")
                    results.append({"test_case_id": tc.get("id", 0), "status": "runtime_error",
                                    "runtime_ms": 0, "output": None, "stdout": "", "error": _truncate(message)})
                    if stop_on_first_failure:
                        break
                    continue

            message = {"input": tc.get("input", [])}
            # Probes only mean anything to a custom validator, on an instance.
            probes = tc.get("probes") if validate_fn is not None and kind == "operations" else None
            if probes:
                message["probes"] = probes
            child.send(message)
            reply = child.read(deadline_s)
            result = _finalize(tc, reply, comparison, validate_fn, time_limit_s, probes)
            results.append(result)

            if reply.get("status") in (_HANG, _CRASH, "time_limit_exceeded"):
                # A hang leaves the child unresponsive (or the SIGALRM path just
                # unwound through user state we don't trust); a crash already
                # ended it. Either way, start the next case with a clean child.
                child.kill()
                child = None
            if result["status"] != "passed" and stop_on_first_failure:
                break
    finally:
        if child is not None:
            child.kill()

    # `runtime_ms` is filled in for TLE rows here (the child, killed, can't report
    # it), keeping the field a number as every other row has it.
    for result in results:
        if result["status"] == "time_limit_exceeded" and result["runtime_ms"] is None:
            result["runtime_ms"] = time_limit_ms
    return results


def _set_nondumpable():
    """Mark this (parent) process non-dumpable on Linux, so the child — same uid —
    can't read the parent's memory (which holds every `expected`) through
    /proc/<pid>/mem or ptrace. Best-effort: a no-op where prctl isn't available
    (e.g. a developer's macOS), where the container boundary isn't in play anyway."""
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        PR_SET_DUMPABLE = 4
        libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    except Exception:  # noqa: BLE001 - defense in depth; never fail the run over it
        pass


def main():
    signal.signal(signal.SIGALRM, _alarm_handler)
    if len(sys.argv) > 2 and sys.argv[1] == CHILD_FLAG:
        _child_main(int(sys.argv[2]))
        return

    # Payload channel: stdin by default (the `docker run -i` path). Under
    # Kubernetes there's no stdin pipe, so the runner mounts the payload as a file
    # and points JUDGE_PAYLOAD_FILE at it. Either way it's read and parsed here in
    # the trusted parent, and the file is removed before any user code runs, so
    # the child (which never gets the path) can't read the expected answers back
    # out of it.
    payload_file = os.environ.get("JUDGE_PAYLOAD_FILE")
    if payload_file:
        with open(payload_file, encoding="utf-8") as fh:
            raw = fh.read()
        try:
            os.unlink(payload_file)
        except OSError as exc:
            # Fail closed: if we can't delete the payload we can't guarantee the
            # child can't read `expected` back at the fixed path, so refuse the run
            # (a judge-side fault, no report) rather than grade with it readable.
            # The k8s emptyDir is world-writable and we own the file, so this never
            # fires in practice; it guards against a future misconfiguration.
            sys.stderr.write("harness: could not remove payload file: %s\n" % exc)
            sys.exit(3)
    else:
        raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write("harness: invalid payload JSON: %s\n" % exc)
        sys.exit(2)
    _set_nondumpable()
    results = run(payload)
    sys.stdout.write(json.dumps({"results": results}))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
