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
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.judge_budget import fits_job_timeout, max_cases_within_job_timeout
from app.sandbox import ALL_NODE_TYPES, profile_for, rust_method_name
from app.schemas.solution import SolutionIn

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
    def _custom_validator_requires_python_and_code(self) -> "ProblemIn":
        # judge/harness.py's custom_validator mode (DESIGN.md §5.4) is
        # Python-only for v1 (harness.js/harness_sql.py don't implement it) and
        # needs the problem author's `validator_code` to actually check
        # anything — reject a missing/empty one at authoring time rather than
        # letting every submission to the problem silently pass/fail against
        # whatever the harness falls back to. The comparison is shared, so a
        # second language would be judged by a harness that can't run it.
        if self.comparison.get("mode") == "custom_validator":
            if [v.language for v in self.languages] != ["python"]:
                raise ValueError(
                    "comparison mode 'custom_validator' is only supported when "
                    "'python' is the problem's only language")
            code = self.comparison.get("validator_code")
            if not isinstance(code, str) or not code.strip():
                raise ValueError(
                    "comparison mode 'custom_validator' requires a non-empty 'validator_code' string")
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


class TestCaseIn(BaseModel):
    ordinal: int
    input: list[Any]
    expected: Any = None
    is_sample: bool = False


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
            for op in ops[1:]:
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
        declared = [v.language for v in self.languages]
        for sol in self.solutions:
            if isinstance(sol.code, str):
                if len(declared) != 1:
                    raise ValueError(
                        f"solution '{sol.title}': give 'code' as a map of language to code "
                        "when the problem has several languages")
                sol.code = {declared[0]: sol.code}
            if not sol.code:
                raise ValueError(f"solution '{sol.title}' has no code")
            extra = set(sol.code) - set(declared)
            if extra:
                raise ValueError(
                    f"solution '{sol.title}' has code for {sorted(extra)}, "
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
