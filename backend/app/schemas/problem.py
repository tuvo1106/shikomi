"""Problem schemas, split by audience — the split is a security boundary.

Two tiers, deliberately different shapes:

* **Read** (`ProblemListItem`, `ProblemDetail`, `SampleCase`) — what any
  logged-in user sees. Crucially, only *sample* cases appear; hidden test cases
  never have a response schema that could serialize them.
* **Write** (`ProblemFile`, built from `ProblemIn` + `TestCaseIn` + `SolutionIn`) —
  the shape of a problem file, validated by `app.cli seed`. Never exposed over HTTP.

`Literal[...]` types (`Difficulty`, `UserStatus`) restrict a string to a fixed
set, so an invalid value is rejected at the boundary and shows up as an enum in
the OpenAPI docs.
"""
import ast
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.comparison import validator_codes
from app.judge_budget import fits_job_timeout, max_cases_within_job_timeout
from app.sandbox import ALL_NODE_TYPES, profile_for, rust_method_name
from app.schemas.solution import SolutionIn, WrongSolutionIn

UserStatus = Literal["solved", "attempted", "unsolved"]
Difficulty = Literal["easy", "medium", "hard"]
# "" = plain JSON return (the harness no-ops); anything else must be a codec the
# harness actually implements (judge/harness.py's `_CODECS`) — rejecting a typo
# like "listnode" here beats it silently no-op'ing and surfacing later as a
# confusing wrong_answer/runtime_error on every submission to the problem.
# "List[ListNode]"/"List[TreeNode]" convert each element of a JSON array
# independently. "CyclicListNode" is a separate type from "ListNode" (not a
# mode of it) for problems needing a genuinely cyclic structure — e.g. "return
# the node where the cycle begins" — since "ListNode" must keep meaning
# "straight-line list" (judge/harness.py's codec-scope comment has the full
# reasoning). "RandomListNode" is likewise its own type, for a list whose
# nodes carry a second `.random` pointer to an arbitrary node (or none).
# "GraphNode" is likewise its own type, for a graph represented as node
# objects (a `.val` plus a `.neighbors` list of other nodes, rather than a
# flat list).
# `return_type` doesn't apply to "operations" mode (a class-replay problem's
# methods have no declared return type to decode against) — see the codec
# note in judge/harness.py for what *is* combined with "operations" mode
# (constructor `params` only).
ReturnType = Literal[
    "", "ListNode", "TreeNode", "List[ListNode]", "List[TreeNode]", "CyclicListNode",
    "RandomListNode", "GraphNode",
]
# "function" (default): the harness calls `function_name` once per test case.
# "operations": a design/class-replay problem (a cache, a state machine) — the
# harness instantiates `class_name` once per test case and replays a sequence of
# method calls against it (judge/harness.py's `_run_operations`).
# "sql": a query against a seeded schema (DESIGN.md §13, judge/harness_sql.py)
# — there's no function/class to invoke, so neither function_name nor
# class_name applies (see _exactly_one_name_for_kind below).
Kind = Literal["function", "operations", "sql"]
# "python" (default): judged by judge/harness.py under CPython — its
# ListNode/TreeNode codec applies to a function's params/return, and to an
# "operations" problem's constructor params too, but never to a method call's own args/
# return. "js": judged by judge/harness.js under Node (DESIGN.md §13) —
# function-mode only, no "operations" kind and no ListNode/TreeNode codecs on
# that path at all. "rust": compiled by rustc and judged by judge/harness_rs/
# (DESIGN.md §13, docs/adr/0004-rust-judge-compile-in-sandbox.md), function and
# operations mode, with every node codec (`SandboxProfile.node_types`);
# its method calls decode their args by the user's signature. "mysql": judged by
# judge/harness_sql.py against an
# ephemeral MariaDB instance (DESIGN.md §13, docs/adr/0002-sql-judge-engine-
# mysql-vs-mariadb.md) — always paired with kind="sql", never any other kind.
Language = Literal["python", "js", "rust", "mysql"]


class ParamSpec(BaseModel):
    """One function parameter's `{name, type}`, used to render/format inputs."""

    name: str
    type: str


class SampleCase(BaseModel):
    """A visible example case. Only sample cases are exposed — hidden cases never
    appear in any public schema, so they can't leak through serialization."""

    ordinal: int
    input: list[Any]
    expected: Any = None


# --- public read models -----------------------------------------------------

class ProblemListItem(BaseModel):
    id: uuid.UUID
    slug: str
    title: str
    difficulty: str
    tags: list[str]
    # The problem's languages, default first — lets the catalog show which
    # languages a problem can be solved in without fetching each detail.
    languages: list[str]
    user_status: UserStatus


class ProblemListResponse(BaseModel):
    items: list[ProblemListItem]
    total: int


class ProblemFacets(BaseModel):
    """Every distinct tag/collection in the published catalog, for the filter
    dropdowns — which can't be derived from a single page of results."""

    tags: list[str]
    collections: list[str]


class LanguageVariantOut(BaseModel):
    """One language a problem is offered in, as the workspace needs it: what to
    pre-fill the editor with, how to label the params, and the statement note.
    """

    language: str
    starter_code: str
    function_name: str | None = None
    class_name: str | None = None
    params: list[ParamSpec]
    return_type: str
    note_md: str = ""


class ProblemDetail(BaseModel):
    """The workspace's view of a problem.

    `languages` is ordered, default first (docs/adr/0005-multi-language-
    problems.md); a one-language problem simply has one entry. Everything
    outside it (statement, kind, samples) is shared by every language.
    """

    id: uuid.UUID
    slug: str
    title: str
    difficulty: str
    statement_md: str
    kind: str
    languages: list[LanguageVariantOut]
    tags: list[str]
    constraints: list[str]
    sample_cases: list[SampleCase]
    has_solutions: bool
    user_status: UserStatus


# --- write models (seed loading) --------------------------------------------

# The fields that were top-level before problems had several languages. A file
# may still put them at the top level (one language); `ProblemIn` lifts them into
# a one-element `languages` list, and `app.cli` doesn't warn about them.
LEGACY_LANGUAGE_FIELDS = (
    "language", "starter_code", "function_name", "class_name", "params", "return_type")

# Every node codec name a param may declare (`SandboxProfile.node_types` says which each
# language implements).
_NODE_PARAM_TYPES = ALL_NODE_TYPES


def _is_index(x: Any, size: int) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and 0 <= x < size


def _operations_case(case_input: Any) -> tuple[list, list]:
    """An operations case's `(ops, args)`: `input` is `[ops, args]`, with `ops[0]`
    the class name and `args[0]` the constructor's argument list. Either is [] for
    a case that isn't shaped so (the harness reports that case; the validators that
    read ops or constructor args just find nothing to check)."""
    if isinstance(case_input, list) and len(case_input) == 2:
        ops, args = case_input
        return (ops if isinstance(ops, list) else [], args if isinstance(args, list) else [])
    return [], []


# The keyword call judge/harness.py makes to a Python validator (`_run_validator`).
VALIDATOR_CALL = ("actual", "expected", "args", "probe_results")


def _is_generator(fn: ast.FunctionDef) -> bool:
    """Whether `fn`'s own body yields (a nested function's `yield` doesn't count)."""
    todo = list(fn.body)
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            return True
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            todo.extend(ast.iter_child_nodes(node))
    return False


def _module_bindings(tree: ast.Module, name: str) -> list[ast.AST]:
    """Every statement that binds `name` at module scope: a def or class of that
    name, an assignment, `for` or `with` target, walrus, or import, in any block
    (`if`, `try`, ...). Nested scopes (functions, classes, lambdas,
    comprehensions) bind their own names, so they aren't searched."""
    found, todo = [], list(tree.body)
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                found.append(node)
            todo.extend(node.decorator_list)
            continue  # its body is its own scope
        if isinstance(node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            continue
        if isinstance(node, ast.AnnAssign) and node.value is None:
            continue  # a bare annotation (`validate: object`) binds nothing
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Store):
            found.append(node)
        elif isinstance(node, ast.alias) and (node.asname or node.name.split(".")[0]) == name:
            found.append(node)
        todo.extend(ast.iter_child_nodes(node))
    return found


def validator_call_problem(validator_code: str) -> str | None:
    """Why judge/harness.py would refuse the Python validator, or None when it
    wouldn't, or when that can't be told without running it. Parsed, never
    executed: seed validation runs no problem code.

    It decides only the clear case: `validate` is bound once at module scope, by
    an undecorated `def`. A decorator, a second def, an assignment or an import
    of the name means what runs isn't simply that def, so it's None, and the
    harness judges the real object when it loads it (so does a script that
    doesn't parse). In the clear case it mirrors `_load_validator`:

    * an `async def` or a generator is refused: the judge would get a coroutine
      or generator, which is always truthy, so every case would pass;
    * the call `validate(actual=, expected=, args=, probe_results=)` must bind:
      each of the four names a parameter it can be passed by keyword (or a
      `**kwargs`), and any other parameter a default, so a leftover `instance`
      next to `probe_results` is refused.
    """
    if not isinstance(validator_code, str):
        return "the validator isn't source code"
    try:
        tree = ast.parse(validator_code)
    except SyntaxError:
        return None
    bindings = _module_bindings(tree, "validate")
    if len(bindings) != 1 or not isinstance(bindings[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
        return None
    fn = bindings[0]
    if fn.decorator_list:
        return None
    if isinstance(fn, ast.AsyncFunctionDef):
        return ("the Python validator is `async def`: the judge calls it without awaiting, "
                "and a coroutine is always truthy, so every case would pass")
    if _is_generator(fn):
        return ("the Python validator is a generator (it has `yield`): the judge would get a "
                "generator, which is always truthy, so every case would pass")
    a = fn.args
    positional = a.posonlyargs + a.args
    defaulted = {p.arg for p in positional[len(positional) - len(a.defaults):]}
    defaulted |= {p.arg for p, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None}
    by_keyword = {p.arg for p in a.args + a.kwonlyargs}
    if (any(p.arg not in defaulted for p in a.posonlyargs)
            or (any(name not in by_keyword for name in VALIDATOR_CALL) and not a.kwarg)
            or not all(p.arg in VALIDATOR_CALL or p.arg in defaulted for p in a.args + a.kwonlyargs)):
        return ("the Python validator must be `def validate(actual, expected, args, "
                "probe_results)`, callable with just those four by keyword: the older "
                "`instance` form is gone, and extra calls on an operations instance are the "
                "cases' probes (docs/adr/0007-custom-validators-in-every-language.md)")
    return None


def _operations_ctor_args(case_input: Any) -> list:
    """An operations case's constructor arguments, or [] (see `_operations_case`)."""
    _, args = _operations_case(case_input)
    first = args[0] if args else []
    return first if isinstance(first, list) else []


def _malformed_node_wire(node_type: str, wire: Any) -> str | None:
    """Why `wire` isn't a well-formed encoding of `node_type`'s *structure*, or None.

    Checks the indices that tie nodes together, which a harness would otherwise
    wrap (Python) or refuse (Rust): a cycle position, a random pointer, a graph
    neighbour. Node values are `_node_values`' business. A `CyclicListNode`
    *output* is a node index (or null), not a list, so it isn't checked here.
    """
    if wire is None:
        return None
    if node_type.startswith("List["):
        inner = node_type[len("List["):-1]
        return next((m for m in map(lambda w: _malformed_node_wire(inner, w), wire or []) if m), None)
    if node_type == "CyclicListNode":
        if wire == []:
            return None  # the empty list
        if not (isinstance(wire, list) and len(wire) == 2 and isinstance(wire[0], list)):
            return "expected [values, pos]"
        values, pos = wire
        if pos != -1 and not _is_index(pos, len(values)):
            return f"cycle position {pos!r} is neither -1 nor an index into its {len(values)} values"
    elif node_type == "RandomListNode" and isinstance(wire, list):
        for i, pair in enumerate(wire):
            if not (isinstance(pair, list) and len(pair) == 2):
                return f"node {i} is not [val, random_index]"
            if pair[1] is not None and not _is_index(pair[1], len(wire)):
                return f"node {i}'s random index {pair[1]!r} is not an index into its {len(wire)} nodes"
    elif node_type == "GraphNode" and isinstance(wire, list):
        for i, row in enumerate(wire):
            for nb in row if isinstance(row, list) else [row]:
                if not _is_index(nb - 1 if isinstance(nb, int) and not isinstance(nb, bool) else None, len(wire)):
                    return f"node {i + 1} lists neighbour {nb!r}, but nodes are numbered 1 to {len(wire)}"
    return None


def _node_values(node_type: str, wire: Any):
    """Every node value in `wire`, a test case's encoding of `node_type` (DESIGN.md §5.3).

    Only values a node struct stores are yielded: a cycle position, a random
    pointer's index or a graph's neighbour numbers are the codec's own bookkeeping.
    Malformed wire data yields nothing here; decoding it is the harness's job.
    """
    if not isinstance(wire, list):
        return
    if node_type.startswith("List["):
        for item in wire:
            yield from _node_values(node_type[len("List["):-1], item)
    elif node_type in ("ListNode", "TreeNode"):
        yield from (v for v in wire if v is not None)
    elif node_type == "CyclicListNode" and len(wire) == 2 and isinstance(wire[0], list):
        yield from wire[0]
    elif node_type == "RandomListNode":
        yield from (pair[0] for pair in wire if isinstance(pair, list) and pair)


class LanguageVariantIn(BaseModel):
    """One entry of a problem file's `languages` list (DESIGN.md §7.1)."""

    language: Language
    starter_code: str
    function_name: str | None = None
    class_name: str | None = None
    params: list[ParamSpec] = Field(default_factory=list)
    return_type: ReturnType = ""
    # A short language-specific addendum to the shared statement (Markdown).
    note_md: str = ""


class ProblemIn(BaseModel):
    """A problem's metadata as `app.cli seed` loads it (a full replace on update).

    This is the schema a seed file is validated against (DESIGN.md §7.1), so its
    validators are the authoring rules. Defaults make a minimal file ergonomic: omit `slug` to auto-derive it,
    `is_published=False` so new problems start as drafts. `default_factory` (not a
    bare `[]`/`{}`) avoids the classic mutable-default trap where every instance
    would share one list/dict.

    `languages` is ordered, default first. The rules tying a language to the
    shared `kind` and `comparison` are checked for **every** variant, since any
    of them can be submitted to.
    """

    slug: str | None = None  # auto-generated from title if omitted
    title: str
    difficulty: Difficulty
    statement_md: str
    kind: Kind = "function"
    languages: list[LanguageVariantIn] = Field(min_length=1)
    comparison: dict = Field(default_factory=lambda: {"mode": "exact"})
    # gt=0: the harness floors 0/negative to a 1ms timeout (judge/harness.py), which
    # doesn't crash but makes every submission bogusly time out. Upper bounds are
    # sane caps on a sandboxed judge run, not derived from any hard system limit.
    time_limit_ms: int = Field(default=2000, gt=0, le=30_000)
    memory_limit_mb: int = Field(default=256, gt=0, le=2048)
    is_published: bool = False
    tags: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    # Operator-curated set membership (e.g. "gang-of-four"); a problem can be
    # in more than one. Free-form like `tags`, no enum. Only used to filter
    # (`?collection=`, §4.2), so it isn't added to ProblemListItem/ProblemDetail.
    collections: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _lift_single_language_form(cls, data: Any) -> Any:
        # A file written before problems had several languages carries its one
        # language's fields at the top level. Lift them into `languages` so both
        # forms load: problem directories outside this repo keep working, and a
        # one-language problem stays as terse as it always was. Using both forms
        # at once is ambiguous (which one wins?), so it's an error.
        if not isinstance(data, dict):
            return data
        legacy = {k: data[k] for k in LEGACY_LANGUAGE_FIELDS if k in data}
        if not legacy:
            return data
        if "languages" in data:
            raise ValueError(
                f"use either 'languages' or the top-level {sorted(legacy)}, not both")
        rest = {k: v for k, v in data.items() if k not in legacy}
        return {**rest, "languages": [{"language": "python", **legacy}]}

    @model_validator(mode="after")
    def _languages_are_unique(self) -> "ProblemIn":
        names = [v.language for v in self.languages]
        if len(names) != len(set(names)):
            raise ValueError("each language may appear only once in 'languages'")
        return self

    @model_validator(mode="after")
    def _exactly_one_name_for_kind(self) -> "ProblemIn":
        # The harness looks up function_name (kind="function") or class_name
        # (kind="operations") in the exec'd namespace — the wrong one being set
        # (or neither) would only surface later as a confusing "not found" on
        # every submission, instead of a clear validation error at seed time.
        # kind="sql" invokes neither — the harness runs the submission as a raw
        # query (judge/harness_sql.py), so both must be unset.
        for v in self.languages:
            where = f"language '{v.language}': "
            if self.kind == "function":
                if not v.function_name:
                    raise ValueError(where + "function_name is required when kind is 'function'")
                if v.class_name:
                    raise ValueError(where + "class_name must not be set when kind is 'function'")
            elif self.kind == "operations":
                if not v.class_name:
                    raise ValueError(where + "class_name is required when kind is 'operations'")
                if v.function_name:
                    raise ValueError(
                        where + "function_name must not be set when kind is 'operations'")
            elif v.function_name or v.class_name:  # kind == "sql"
                raise ValueError(
                    where + "function_name/class_name must not be set when kind is 'sql'")
        return self

    @model_validator(mode="after")
    def _language_supports_kind_and_codecs(self) -> "ProblemIn":
        # judge/harness.js only implements function mode (DESIGN.md §13), and each
        # harness implements its own set of node codecs
        # (JS none; Rust all, the decode-only Iterator being prelude.rs `IntIter`). Reject an
        # unsupported combination here rather than letting it surface as a
        # confusing runtime_error from the harness on every submission.
        for v in self.languages:
            profile = profile_for(v.language)
            lang = v.language
            if profile.function_mode_only and self.kind != "function":
                raise ValueError(f"language '{lang}' only supports kind 'function'")
            declared = [v.return_type] + [p.type for p in v.params]
            for node_type in declared:
                if node_type in _NODE_PARAM_TYPES and node_type not in profile.node_types:
                    raise ValueError(
                        f"language '{lang}' does not support the '{node_type}' node type"
                        + (f" (it supports: {', '.join(sorted(profile.node_types))})"
                           if profile.node_types else ""))
        return self

    @model_validator(mode="after")
    def _same_params_in_every_language(self) -> "ProblemIn":
        # The test cases are shared, and a case's `input` is positional: one JSON
        # value per param. A variant with a different param count could never
        # match them. Names and types may differ (they're per-language display).
        counts = {len(v.params) for v in self.languages}
        if len(counts) > 1:
            raise ValueError("every language must declare the same number of params")
        return self

    @model_validator(mode="after")
    def _memory_fits_the_sandbox(self) -> "ProblemIn":
        # A language can need a floor under memory_limit_mb: Rust's rustc compiles
        # the submission inside the same limit (~75MB peak, ADR-0004), so below its
        # profile's floor a problem could fail to *compile* on the judge. The limit
        # is shared, so it must clear every language's floor.
        for v in self.languages:
            floor = profile_for(v.language).min_memory_limit_mb
            if self.memory_limit_mb < floor:
                raise ValueError(
                    f"language '{v.language}' needs memory_limit_mb >= {floor}: "
                    "its judge's own work (rustc, for Rust) runs inside the same limit")
        return self

    @model_validator(mode="after")
    def _custom_validator_for_every_language(self) -> "ProblemIn":
        # A custom validator is code, and each harness runs only its own language's
        # (DESIGN.md §5.4, docs/adr/0007-custom-validators-in-every-language.md), so
        # `validator_code` maps language → source, one entry per language the problem
        # is offered in. The bare-string form every existing file uses is Python's
        # validator (app/comparison.py `validator_codes`, the one definition of that
        # rule); it's stored as the map, so a row is never the string form.
        #
        # Each rule is checked at authoring time rather than left to surface later:
        # a language whose harness can't run validators, or one with no validator,
        # would make every submission in it a judge_error; an empty validator would
        # judge nothing. A key for a language the problem doesn't offer is almost
        # certainly a typo (`"rs"`), and would be silently dead code.
        if self.comparison.get("mode") != "custom_validator":
            return self
        code = validator_codes(self.comparison)
        if not code:
            raise ValueError(
                "comparison mode 'custom_validator' requires 'validator_code': a map of "
                "language to validator source (or one Python source string)")
        offered = [v.language for v in self.languages]
        for lang in offered:
            if not profile_for(lang).custom_validator:
                raise ValueError(
                    f"language '{lang}' does not support comparison mode 'custom_validator'")
            source = code.get(lang)
            if not isinstance(source, str) or not source.strip():
                raise ValueError(
                    f"comparison mode 'custom_validator' needs a non-empty validator for "
                    f"language '{lang}' in 'validator_code'")
        extra = sorted(set(code) - set(offered))
        if extra:
            raise ValueError(
                f"'validator_code' has a validator for {extra}, which the problem's "
                f"'languages' doesn't list")
        # The harness refuses a Python validator its call can't bind to, which
        # includes the older `instance` form: that ran next to the submission,
        # where it could be read and forged (ADR-0007). A validator whose
        # parameters can't be read without running it is left to the harness,
        # which reports it on load.
        problem = validator_call_problem(code.get("python", ""))
        if problem:
            raise ValueError(problem)
        self.comparison = {**self.comparison, "validator_code": code}
        return self

    @model_validator(mode="after")
    def _sql_kind_and_language_are_paired(self) -> "ProblemIn":
        # kind="sql" and language="mysql" always travel together — kind picks
        # the invocation shape (a raw query, not a function/class call),
        # language picks the sandbox image (judge/harness_sql.py's engine).
        # Neither makes sense without the other, so reject the mismatch at
        # authoring time rather than letting it surface as a confusing
        # "kind '...' is not supported by the SQL harness" runtime_error, or a
        # 'mysql'-language problem trying to run through the python harness.
        for v in self.languages:
            if (self.kind == "sql") != (v.language == "mysql"):
                raise ValueError("kind 'sql' and language 'mysql' must be set together")
            if self.kind == "sql":
                if v.params:
                    raise ValueError(
                        "kind 'sql' does not use params — the submission is a raw query")
                if v.return_type:
                    raise ValueError(
                        "kind 'sql' does not use return_type — rows are compared directly")
        return self


# The most probe calls one case may make. A probe runs the submission's code inside
# the case's `time_limit_ms` (so the judge budget is unaffected), and results travel
# back on the result pipe, so the cap bounds that payload. The largest real use, a
# per-index distribution check, makes 8000.
MAX_PROBE_CALLS = 20_000


class ProbeIn(BaseModel):
    """One judge-added call, made on an operations case's instance after the replay
    (judge/harness.py "probes"; docs/adr/0007-custom-validators-in-every-language.md).

    Its results go to a probe-form custom validator as `probe_results`, so a property
    that needs more calls than the case makes (a round trip, a distribution) can be
    checked in the trusted parent, without the live instance.

    `refs` fills argument `i` with the result of the case's own op `j` (`{i: j}`),
    for a round trip: `{"op": "decode", "args": [null], "refs": {"0": 1}}`. `repeat`
    makes the same call n times.
    """

    # Unlike a problem file's top level (unknown keys only warn there), a probe's are
    # refused: a typo like `"repeats"` or `"ref"` would otherwise be dropped, and the
    # probe would quietly make one call, or pass a literal, instead of what was meant.
    model_config = ConfigDict(extra="forbid")

    op: str = Field(min_length=1)
    args: list[Any] = Field(default_factory=list)
    refs: dict[int, int] = Field(default_factory=dict)
    repeat: int = Field(default=1, ge=1, le=MAX_PROBE_CALLS)

    @model_validator(mode="after")
    def _refs_fill_real_args(self) -> "ProbeIn":
        for i in self.refs:
            if not 0 <= i < len(self.args):
                raise ValueError(
                    f"probe {self.op!r}: refs names argument {i}, but it has {len(self.args)}")
        # A ref is for a round trip (one call on one result), `repeat` for a
        # distribution (many calls on literals). Keeping them apart means a repeated
        # call's arguments are always the small literals written here, so the
        # harness's per-call copy of them costs nothing inside the time limit.
        if self.refs and self.repeat > 1:
            raise ValueError(f"probe {self.op!r}: refs and repeat can't be combined")
        return self


class TestCaseIn(BaseModel):
    ordinal: int
    input: list[Any]
    expected: Any = None
    is_sample: bool = False
    probes: list[ProbeIn] | None = None


class ProblemFile(ProblemIn):
    """One problem file, whole: metadata plus its test cases and solutions.

    `app.cli seed` validates every file against this *before* writing anything, so
    each rule here is checked while a bad file can still be rejected cleanly. That
    ordering is the point: a problem is only ever written together with its cases,
    never as a published row waiting for cases that then fail validation.

    Unknown keys are ignored (Pydantic's default) rather than rejected, so a file
    carrying a field this version doesn't know still loads; the seed command warns
    about them instead.
    """

    # min_length=1: a problem with no cases judges every submission 0/0, which
    # aggregates to `accepted` — a missing or misspelled `test_cases` key would
    # otherwise publish a problem that accepts anything.
    test_cases: list[TestCaseIn] = Field(min_length=1)
    solutions: list[SolutionIn] = Field(default_factory=list)
    # Code the judge must reject (`WrongSolutionIn`): test data for
    # judge/tests/test_seed_solutions.py, never written to the database.
    wrong_solutions: list[WrongSolutionIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ordinals(self) -> "ProblemFile":
        # Duplicate case ordinals collide in build_verdict_results (worker/judging.py),
        # which keys revealed-case lookup by ordinal — a collision can reveal the
        # wrong hidden case's input/expected to the client.
        ordinals = [tc.ordinal for tc in self.test_cases]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("test case ordinals must be unique")
        # Solutions have a DB unique constraint (uq_solution_ordinal); checked here too
        # so a duplicate is a per-file validation error, not a crash mid-write.
        ordinals = [sol.ordinal for sol in self.solutions]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("solution ordinals must be unique")
        return self

    def _node_wires(self, tc: "TestCaseIn"):
        """`(variant, where, node_type, wire)` for every node-typed argument and
        expected output in case `tc` (an any_of case contributes each option). In an
        operations case, `params` describe the constructor, whose arguments are the
        case's first argument list; its method calls take no declared types."""
        if self.kind == "operations":
            ctor_args = _operations_ctor_args(tc.input)
            for v in self.languages:
                for i, p in enumerate(v.params):
                    if p.type in _NODE_PARAM_TYPES and p.type != "Iterator" and i < len(ctor_args):
                        yield v, f"constructor argument {p.name}", p.type, ctor_args[i]
            return
        if self.kind != "function":
            return
        expected_options = self.comparison.get("mode") == "any_of"
        for v in self.languages:
            for i, p in enumerate(v.params):
                if p.type in _NODE_PARAM_TYPES and i < len(tc.input):
                    yield v, p.name, p.type, tc.input[i]
            if v.return_type:
                outputs = tc.expected if expected_options and isinstance(tc.expected, list) else [tc.expected]
                for out in outputs:
                    yield v, "the expected output", v.return_type, out

    @model_validator(mode="after")
    def _node_wires_are_well_formed(self) -> "ProblemFile":
        # The indices inside a node encoding (a cycle's position, a random pointer, a
        # graph neighbour) must point at a real node. harness.py silently wraps a bad
        # one (`nodes[-1]` is the last node), harness_rs refuses it, so a case like
        # that would pass its Python reference and fail every Rust submission. It's
        # an authoring bug in every language, so it's refused here for all of them.
        for tc in self.test_cases:
            for _, where, node_type, wire in self._node_wires(tc):
                if node_type == "CyclicListNode" and where == "the expected output":
                    continue  # the answer is a node's index (or null), not a list
                problem = _malformed_node_wire(node_type, wire)
                if problem:
                    raise ValueError(f"test case {tc.ordinal}: {where} is not a valid {node_type}: {problem}")
        return self

    @model_validator(mode="after")
    def _node_values_fit_each_language(self) -> "ProblemFile":
        # The test cases are shared, but a language's node struct may hold less than
        # JSON does (Rust's `val` is an i32; `SandboxProfile.node_value_range`). A value
        # that doesn't fit would fail every submission in that language with a decode
        # error, so it fails here instead, at seed time.
        for tc in self.test_cases:
            for v, where, node_type, wire in self._node_wires(tc):
                value_range = profile_for(v.language).node_value_range
                if value_range is None:
                    continue
                lo, hi = value_range
                for x in _node_values(node_type, wire):
                    if isinstance(x, bool) or not isinstance(x, int) or not lo <= x <= hi:
                        raise ValueError(
                            f"test case {tc.ordinal}: {where} holds the node value {x!r}, but "
                            f"language '{v.language}' stores a {node_type} value as an integer "
                            f"in [{lo}, {hi}]")
        return self

    def authoring_warnings(self) -> list[str]:
        """What loads but deserves the author's attention; `app.cli` prints these.

        A custom validator with no wrong solution in some language: nothing proves
        that language's validator rejects anything (`WrongSolutionIn`). A warning,
        since a problem set adds them over time.
        """
        if self.comparison.get("mode") != "custom_validator":
            return []
        warnings = []
        covered = {lang for sol in self.wrong_solutions for lang in sol.code}
        unproven = [v.language for v in self.languages if v.language not in covered]
        if unproven:
            warnings.append(
                f"its custom validator has no wrong solution for {unproven}, so nothing proves "
                "it rejects a wrong answer there; add one to 'wrong_solutions'")
        return warnings

    @model_validator(mode="after")
    def _probes_feed_a_probe_validator(self) -> "ProblemFile":
        # Probes (`ProbeIn`) exist only to give a custom validator more results to
        # check, so each misuse would otherwise be silent: judge/harness.py sends a
        # case's probes only to a custom validator on an operations instance, and
        # drops them anywhere else. A ref must name
        # one of the case's own ops (1..len-1; 0 is the constructor, which returns
        # nothing), or the harness reports a judge_error on every submission.
        cases = [tc for tc in self.test_cases if tc.probes]
        if not cases:
            return self
        if self.kind != "operations":
            raise ValueError("test case probes need kind 'operations' (they call the instance)")
        if self.comparison.get("mode") != "custom_validator":
            raise ValueError("test case probes need comparison mode 'custom_validator' to check them")
        for tc in cases:
            ops, _ = _operations_case(tc.input)
            for probe in tc.probes:
                for source in probe.refs.values():
                    if not 1 <= source < len(ops):
                        raise ValueError(
                            f"test case {tc.ordinal}: probe {probe.op!r} refs op {source}, but "
                            f"the case's ops are 1..{len(ops) - 1}")
            calls = sum(p.repeat for p in tc.probes)
            if calls > MAX_PROBE_CALLS:
                raise ValueError(
                    f"test case {tc.ordinal}: its probes make {calls} calls, over the "
                    f"{MAX_PROBE_CALLS} limit")
        return self

    @model_validator(mode="after")
    def _ops_are_rust_methods(self) -> "ProblemFile":
        # The Rust harness dispatches each op name to a method by generating source
        # (harness.rs `operations_glue`), after mapping it to snake_case. An op that
        # can't be a Rust identifier, or two ops that map to the same method
        # (`getState` and `get_state`), would fail every Rust submission; caught here.
        # So would an op named `new`, the constructor's name (`rust_method_name`).
        if self.kind != "operations" or not any(v.language == "rust" for v in self.languages):
            return self
        methods: dict[str, str] = {}
        for tc in self.test_cases:
            ops, _ = _operations_case(tc.input)
            # A probe's op is dispatched the same way, so it needs the same mapping.
            for op in ops[1:] + [p.op for p in tc.probes or []]:
                method = rust_method_name(op) if isinstance(op, str) else None
                if method is None:
                    raise ValueError(f"test case {tc.ordinal}: op {op!r} can't be a Rust method name")
                other = methods.setdefault(method, op)
                if other != op:
                    raise ValueError(
                        f"ops {other!r} and {op!r} both map to the Rust method `{method}`")
        return self

    @model_validator(mode="after")
    def _has_a_sample_case(self) -> "ProblemFile":
        # Run judges only the sample cases, so with none it would judge 0/0 — which
        # aggregates to `accepted` for any code, the same hole min_length closes for
        # Submit. The workspace also renders its worked examples from the samples.
        if not any(tc.is_sample for tc in self.test_cases):
            raise ValueError("at least one test case must have is_sample: true")
        return self

    @model_validator(mode="after")
    def _solution_code_per_language(self) -> "ProblemFile":
        # A solution's `code` is a {language: code} map. A plain string is the
        # one-language shorthand (and every file written before problems had
        # several languages), so it's only unambiguous with exactly one language.
        # A wrong solution's `code` follows the same rules, so the seed tests can
        # run it in each language it names.
        declared = [v.language for v in self.languages]
        for what, sol in [("solution", s) for s in self.solutions] + [
                ("wrong solution", s) for s in self.wrong_solutions]:
            if isinstance(sol.code, str):
                if len(declared) != 1:
                    raise ValueError(
                        f"{what} '{sol.title}': give 'code' as a map of language to code "
                        "when the problem has several languages")
                sol.code = {declared[0]: sol.code}
            if not sol.code:
                raise ValueError(f"{what} '{sol.title}' has no code")
            extra = set(sol.code) - set(declared)
            if extra:
                raise ValueError(
                    f"{what} '{sol.title}' has code for {sorted(extra)}, "
                    "which the problem doesn't list in 'languages'")
        # Every language needs a reference solution, or nothing (the seed-solution
        # tests in judge/tests/) proves that language's signature and harness can
        # pass the cases at all.
        if self.solutions:
            covered = {lang for sol in self.solutions for lang in sol.code}
            missing = [lang for lang in declared if lang not in covered]
            if missing:
                raise ValueError(f"no solution has code for {missing}")
        return self

    @model_validator(mode="after")
    def _fits_judge_budget(self) -> "ProblemFile":
        # A full run (cases x time limit + slack) must finish inside arq's judge
        # job_timeout, or arq cancels the job before the runner's own kill fires
        # (app/judge_budget.py). Checked against *this file's* cases and limit
        # together, so changing both in one edit is judged on the new numbers,
        # and for every language, since each has its own startup slack.
        n = len(self.test_cases)
        for v in self.languages:
            if not fits_job_timeout(n, self.time_limit_ms, v.language):
                most = max_cases_within_job_timeout(self.time_limit_ms, v.language)
                raise ValueError(
                    f"{n} test cases at {self.time_limit_ms} ms each cannot finish inside the "
                    f"judge job timeout for language '{v.language}'; use at most {most} cases, "
                    "or a shorter time limit")
        return self
