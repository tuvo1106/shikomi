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
# that path at all. "mysql": judged by judge/harness_sql.py against an
# ephemeral MariaDB instance (DESIGN.md §13, docs/adr/0002-sql-judge-engine-
# mysql-vs-mariadb.md) — always paired with kind="sql", never any other kind.
Language = Literal["python", "js", "mysql"]


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
    user_status: UserStatus


class ProblemListResponse(BaseModel):
    items: list[ProblemListItem]
    total: int


class ProblemFacets(BaseModel):
    """Every distinct tag/collection in the published catalog, for the filter
    dropdowns — which can't be derived from a single page of results."""

    tags: list[str]
    collections: list[str]


class ProblemDetail(BaseModel):
    id: uuid.UUID
    slug: str
    title: str
    difficulty: str
    statement_md: str
    starter_code: str
    kind: str
    language: str
    function_name: str | None = None
    class_name: str | None = None
    params: list[ParamSpec]
    return_type: str
    tags: list[str]
    constraints: list[str]
    sample_cases: list[SampleCase]
    has_solutions: bool
    user_status: UserStatus


# --- write models (seed loading) --------------------------------------------

class ProblemIn(BaseModel):
    """A problem's metadata as `app.cli seed` loads it (a full replace on update).

    This is the schema a seed file is validated against (DESIGN.md §7.1), so its
    validators are the authoring rules. Defaults make a minimal file ergonomic: omit `slug` to auto-derive it,
    `is_published=False` so new problems start as drafts. `default_factory` (not a
    bare `[]`/`{}`) avoids the classic mutable-default trap where every instance
    would share one list/dict.
    """

    slug: str | None = None  # auto-generated from title if omitted
    title: str
    difficulty: Difficulty
    statement_md: str
    kind: Kind = "function"
    language: Language = "python"
    function_name: str | None = None
    class_name: str | None = None
    starter_code: str
    params: list[ParamSpec] = Field(default_factory=list)
    return_type: ReturnType = ""
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

    @model_validator(mode="after")
    def _exactly_one_name_for_kind(self) -> "ProblemIn":
        # The harness looks up function_name (kind="function") or class_name
        # (kind="operations") in the exec'd namespace — the wrong one being set
        # (or neither) would only surface later as a confusing "not found" on
        # every submission, instead of a clear validation error at seed time.
        # kind="sql" invokes neither — the harness runs the submission as a raw
        # query (judge/harness_sql.py), so both must be unset.
        if self.kind == "function":
            if not self.function_name:
                raise ValueError("function_name is required when kind is 'function'")
            if self.class_name:
                raise ValueError("class_name must not be set when kind is 'function'")
        elif self.kind == "operations":
            if not self.class_name:
                raise ValueError("class_name is required when kind is 'operations'")
            if self.function_name:
                raise ValueError("function_name must not be set when kind is 'operations'")
        else:  # kind == "sql"
            if self.function_name or self.class_name:
                raise ValueError("function_name/class_name must not be set when kind is 'sql'")
        return self

    @model_validator(mode="after")
    def _js_requires_function_kind(self) -> "ProblemIn":
        # judge/harness.js only implements function mode (DESIGN.md §13) — no
        # "operations" kind, no ListNode/TreeNode codecs. Reject the combination
        # here rather than letting it surface as a confusing runtime_error from
        # the JS harness on every submission.
        if self.language == "js" and self.kind != "function":
            raise ValueError("language 'js' only supports kind 'function'")
        if self.language == "js" and self.return_type:
            raise ValueError("language 'js' does not support a ListNode/TreeNode return_type")
        if self.language == "js" and any(
            p.type in (
                "ListNode", "TreeNode", "List[ListNode]", "List[TreeNode]",
                "CyclicListNode", "RandomListNode", "GraphNode", "Iterator",
            )
            for p in self.params
        ):
            raise ValueError("language 'js' does not support a ListNode/TreeNode param type")
        return self

    @model_validator(mode="after")
    def _custom_validator_requires_python_and_code(self) -> "ProblemIn":
        # judge/harness.py's custom_validator mode (DESIGN.md §5.4) is
        # Python-only for v1 (harness.js/harness_sql.py don't implement it) and
        # needs the problem author's `validator_code` to actually check
        # anything — reject a missing/empty one at authoring time rather than
        # letting every submission to the problem silently pass/fail against
        # whatever the harness falls back to.
        if self.comparison.get("mode") == "custom_validator":
            if self.language != "python":
                raise ValueError(
                    "comparison mode 'custom_validator' is only supported for language 'python'")
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
        if (self.kind == "sql") != (self.language == "mysql"):
            raise ValueError("kind 'sql' and language 'mysql' must be set together")
        if self.kind == "sql":
            if self.params:
                raise ValueError("kind 'sql' does not use params — the submission is a raw query")
            if self.return_type:
                raise ValueError("kind 'sql' does not use return_type — rows are compared directly")
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
        # Duplicate ordinals collide in build_verdict_results (worker/judging.py),
        # which keys revealed-case lookup by ordinal — a collision can reveal the
        # wrong hidden case's input/expected to the client.
        ordinals = [tc.ordinal for tc in self.test_cases]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("test case ordinals must be unique")
        return self

    @model_validator(mode="after")
    def _fits_judge_budget(self) -> "ProblemFile":
        # A full run (cases x time limit + slack) must finish inside arq's judge
        # job_timeout, or arq cancels the job before the runner's own kill fires
        # (app/judge_budget.py). Checked against *this file's* cases and limit
        # together, so changing both in one edit is judged on the new numbers.
        n = len(self.test_cases)
        if not fits_job_timeout(n, self.time_limit_ms, self.language):
            most = max_cases_within_job_timeout(self.time_limit_ms, self.language)
            raise ValueError(
                f"{n} test cases at {self.time_limit_ms} ms each cannot finish inside the judge "
                f"job timeout; use at most {most} cases, or a shorter time limit")
        return self
