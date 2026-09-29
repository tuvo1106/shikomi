"""The `problems` table: a coding problem plus everything needed to judge it."""
from sqlalchemy import Boolean, CheckConstraint, Integer, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, PKMixin, TimestampMixin

DIFFICULTIES = ("easy", "medium", "hard")


class Problem(Base, PKMixin, TimestampMixin):
    """A problem: its prose and the judging config every language shares.

    Notable columns:

    * `slug` — the stable, URL-friendly id used in routes (`/problems/design-vending-machine`);
      unique. `title` is the display name and can change without breaking links.
    * `kind` — `"function"` (default): the harness calls the variant's
      `function_name(...)` once per test case. `"operations"`: a design/class-replay
      problem (a cache, a stack with extra queries, a state machine) — the harness
      instantiates the variant's `class_name` once per test case and replays a
      sequence of method calls against it (judge/harness.py's `_run_operations`).
      `"sql"`: a query against a seeded schema (judge/harness_sql.py). Shared by
      every language: it decides the *shape* of the test cases, which are shared too.

    * `comparison` (JSONB) — how the judge decides "correct" (default exact match);
      an object so we can add modes (unordered, float-tolerance) without a schema
      change. `{"mode": "custom_validator", "validator_code": {"python": "..."}}`
      hands the check to problem-authored code (judge/harness.py, DESIGN.md §5.4)
      for "any output satisfying property P" problems no fixed answer can express.
      `validator_code` maps each language to its validator, since each harness runs
      only its own language (migration e5a8c2f41d93 converted the old one-string form);
      app/comparison.py picks the submission's, and `ProblemIn` requires one per
      language, in a language whose harness can run it (only Python so far,
      docs/adr/0007-custom-validators-in-every-language.md).
    * `time_limit_ms` / `memory_limit_mb` — the sandbox caps enforced per case.
    * `is_published` — draft vs live; an unpublished problem is invisible to
      every API caller, so an operator can load a draft without exposing it.
    * `tags` / `constraints` — Postgres `text[]` arrays (naturally list-shaped).
    * `collections` — same shape as `tags`, but for operator-curated sets
      (e.g. `"gang-of-four"`) rather than technique/topic — kept
      separate so mixing the two vocabularies doesn't muddy `tags`' per-tag
      counts. Free-form like `tags` (no DB/schema-level enum), enforced only
      by authoring convention.

    **What is not here.** The language, starter code, function/class name, params
    and return type live on `languages` (`ProblemLanguage`, one row per language
    the problem is offered in; docs/adr/0005-multi-language-problems.md). The rules
    that tie them to `kind` (a function name for `"function"`, only `"mysql"` for
    `"sql"`, a `custom_validator` for every language) span both tables, so no CHECK can
    express them; `ProblemIn` enforces them for every variant, and the seed loader
    is the only writer.

    `languages`, `test_cases` and `solutions` are children. `cascade="all, delete-orphan"` +
    `passive_deletes=True` mean deleting a problem lets the DB's `ON DELETE
    CASCADE` remove them (rather than the ORM loading and deleting each row),
    while `order_by` keeps them in authoring order.
    """

    __tablename__ = "problems"
    __table_args__ = (
        CheckConstraint("kind IN ('function', 'operations', 'sql')", name="ck_problems_kind"),
    )

    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    difficulty: Mapped[str] = mapped_column(Text, nullable=False)
    statement_md: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="function")
    comparison: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=lambda: {"mode": "exact"})
    time_limit_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="2000")
    memory_limit_mb: Mapped[int] = mapped_column(Integer, nullable=False, server_default="256")
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    constraints: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    collections: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")

    # `lazy="selectin"`: nearly every read of a problem (list items, detail, the
    # judge) needs its languages, and an async session can't lazy-load on attribute
    # access, so they come along in one extra query whenever problems are loaded.
    languages: Mapped[list["ProblemLanguage"]] = relationship(
        back_populates="problem", cascade="all, delete-orphan",
        order_by="ProblemLanguage.ordinal", passive_deletes=True, lazy="selectin")
    test_cases: Mapped[list["TestCase"]] = relationship(
        back_populates="problem", cascade="all, delete-orphan",
        order_by="TestCase.ordinal", passive_deletes=True)
    solutions: Mapped[list["Solution"]] = relationship(
        back_populates="problem", cascade="all, delete-orphan",
        order_by="Solution.ordinal", passive_deletes=True)


# Imported at the bottom (not the top) to break the import cycle: these modules
# import Problem too. By now Problem is defined, so SQLAlchemy can resolve the
# "ProblemLanguage"/"TestCase"/"Solution" relationship strings above.
from app.models.problem_language import ProblemLanguage  # noqa: E402
from app.models.solution import Solution  # noqa: E402
from app.models.test_case import TestCase  # noqa: E402
