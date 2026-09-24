"""The `problems` table: a coding problem plus everything needed to judge it."""
from sqlalchemy import Boolean, CheckConstraint, Integer, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, PKMixin, TimestampMixin

DIFFICULTIES = ("easy", "medium", "hard")


class Problem(Base, PKMixin, TimestampMixin):
    """A problem: its prose, the function to implement, and the judging config.

    Notable columns:

    * `slug` — the stable, URL-friendly id used in routes (`/problems/design-vending-machine`);
      unique. `title` is the display name and can change without breaking links.
    * `language` — `"python"` (default), `"js"`, or `"mysql"`; selects which
      harness/sandbox image judges the submission (DESIGN.md §13). Independent
      of `kind` in general, but `"mysql"` always pairs with `kind="sql"` and
      nothing else (enforced in `ProblemIn`, backstopped below).
    * `kind` — `"function"` (default): the harness calls `function_name(...)`
      once per test case. `"operations"`: a design/class-replay problem (a cache,
      a stack with extra queries, a state machine) — the harness instantiates `class_name` once per
      test case and replays a sequence of method calls against it (judge/
      harness.py's `_run_operations`). `"sql"`: a query against a seeded schema
      (judge/harness_sql.py) — neither `function_name` nor `class_name`
      applies. Exactly one of `function_name`/`class_name` is set for
      `"function"`/`"operations"`, and neither is set for `"sql"`, matching
      `kind` (enforced in `ProblemIn`).
    * `function_name` / `starter_code` — the harness calls `function_name(...)`;
      the starter code is what pre-fills the editor. `function_name` is null for
      an `"operations"`-kind problem (see `class_name` below).
    * `class_name` — the class the harness instantiates for an `"operations"`-
      kind problem; null for a `"function"`-kind problem. `ProblemIn` enforces
      this pairing at the API boundary; `ck_problems_kind_name_consistency`
      backstops it at the DB level for any row written outside that path (e.g.
      a hand-written script), so a mismatch fails the write instead of
      surfacing later as a confusing "not found" from the harness.
    * `params` (JSONB) — `[{name, type}, …]`, describing the signature so the UI
      can render/format example inputs. JSONB stores structured data inline and is
      queryable, unlike a plain text blob. A `type` of `"ListNode"`/`"TreeNode"`
      also tells the judge harness to build that param from its flat/level-order
      JSON array instead of passing the array through as-is (`return_type` below
      is the same idea for the function's return value). Only meaningful for
      `"function"`-kind problems.
    * `return_type` — empty for ordinary JSON-returning functions; `"ListNode"`/
      `"TreeNode"` tells the harness to flatten the returned node graph back to a
      JSON array before comparing against `expected`.
    * `comparison` (JSONB) — how the judge decides "correct" (default exact match);
      an object so we can add modes (unordered, float-tolerance) without a schema
      change. `{"mode": "custom_validator", "validator_code": "..."}` hands the
      check to problem-authored Python (judge/harness.py, DESIGN.md §5.4) for
      "any output satisfying property P" problems no fixed answer can express —
      Python-only (`ck_problems_custom_validator_requires_python` below
      backstops `ProblemIn`'s pairing check).
    * `time_limit_ms` / `memory_limit_mb` — the sandbox caps enforced per case.
    * `is_published` — draft vs live; an unpublished problem is invisible to
      every API caller, so an operator can load a draft without exposing it.
    * `tags` / `constraints` — Postgres `text[]` arrays (naturally list-shaped).
    * `collections` — same shape as `tags`, but for operator-curated sets
      (e.g. `"gang-of-four"`) rather than technique/topic — kept
      separate so mixing the two vocabularies doesn't muddy `tags`' per-tag
      counts. Free-form like `tags` (no DB/schema-level enum), enforced only
      by authoring convention.

    `test_cases` and `solutions` are children. `cascade="all, delete-orphan"` +
    `passive_deletes=True` mean deleting a problem lets the DB's `ON DELETE
    CASCADE` remove them (rather than the ORM loading and deleting each row),
    while `order_by` keeps them in authoring order.
    """

    __tablename__ = "problems"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'function' AND function_name IS NOT NULL AND class_name IS NULL) OR "
            "(kind = 'operations' AND class_name IS NOT NULL AND function_name IS NULL) OR "
            "(kind = 'sql' AND function_name IS NULL AND class_name IS NULL)",
            name="ck_problems_kind_name_consistency",
        ),
        CheckConstraint("language IN ('python', 'js', 'mysql')", name="ck_problems_language"),
        # kind='sql' and language='mysql' always travel together — see the
        # `language`/`kind` docstring bullets above. Backstops ProblemIn's
        # `_sql_kind_and_language_are_paired` validator at the DB level.
        CheckConstraint(
            "(kind = 'sql') = (language = 'mysql')",
            name="ck_problems_sql_kind_mysql_language",
        ),
        # Backstops ProblemIn's `_custom_validator_requires_python_and_code`
        # validator: `comparison.mode == "custom_validator"` always pairs with
        # `language = 'python'` and a non-empty `validator_code` (harness.js/
        # harness_sql.py don't implement this mode — see `comparison` above).
        # `coalesce(..., '')` matters: without it, a row with no `validator_code`
        # key at all evaluates `length(NULL) > 0` to SQL NULL, and Postgres
        # treats a NULL CHECK result as passing — silently letting the exact
        # row this constraint exists to reject through.
        CheckConstraint(
            "(comparison->>'mode' IS DISTINCT FROM 'custom_validator') OR "
            "(language = 'python' AND length(coalesce(comparison->>'validator_code', '')) > 0)",
            name="ck_problems_custom_validator_requires_python",
        ),
    )

    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    difficulty: Mapped[str] = mapped_column(Text, nullable=False)
    statement_md: Mapped[str] = mapped_column(Text, nullable=False)
    # "python" (default): the harness (judge/harness.py) compiles/execs the
    # submission with CPython. "js": judge/harness.js runs it under Node's `vm`
    # module instead. "mysql": judge/harness_sql.py runs it as a query against
    # an ephemeral MariaDB instance (docs/adr/0002-sql-judge-engine-mysql-vs-
    # mariadb.md) — a different sandbox image either way (worker/judging.py's
    # IMAGE_BY_LANGUAGE), same protocol/verdict shape (DESIGN.md §13). Only
    # "function"-kind problems support "js"; "mysql" only ever pairs with
    # kind="sql".
    language: Mapped[str] = mapped_column(Text, nullable=False, server_default="python")
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="function")
    function_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    class_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    starter_code: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Non-empty only for a function returning a `ListNode`/`TreeNode` graph — tells
    # the judge harness to flatten the return value back to JSON before comparing
    # against `expected`. Empty string = plain JSON passthrough.
    return_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    comparison: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=lambda: {"mode": "exact"})
    time_limit_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="2000")
    memory_limit_mb: Mapped[int] = mapped_column(Integer, nullable=False, server_default="256")
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    constraints: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    collections: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")

    test_cases: Mapped[list["TestCase"]] = relationship(
        back_populates="problem", cascade="all, delete-orphan",
        order_by="TestCase.ordinal", passive_deletes=True)
    solutions: Mapped[list["Solution"]] = relationship(
        back_populates="problem", cascade="all, delete-orphan",
        order_by="Solution.ordinal", passive_deletes=True)


# Imported at the bottom (not the top) to break the import cycle: these modules
# import Problem too. By now Problem is defined, so SQLAlchemy can resolve the
# "TestCase"/"Solution" relationship strings above.
from app.models.solution import Solution  # noqa: E402
from app.models.test_case import TestCase  # noqa: E402
