"""The `problem_languages` table: one language a problem can be solved in.

A problem is offered in one or more languages (docs/adr/0005-multi-language-
problems.md). Everything the *judge* needs that depends on the language lives
here, one row per language; everything that is the same in every language (the
statement, `kind`, `comparison`, the limits, and the test cases, which are plain
JSON) stays on `problems`. That split is what makes adding a third or fourth
language a data change, not a schema change.
"""
import uuid

from sqlalchemy import UUID, CheckConstraint, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, PKMixin, TimestampMixin

LANGUAGES = ("python", "js", "rust", "mysql")


class ProblemLanguage(Base, PKMixin, TimestampMixin):
    """One language variant of a problem: its signature, starter code and judge.

    * `language` selects the harness and sandbox image (`app/sandbox.py`,
      DESIGN.md §13). Unique per problem.
    * `ordinal` orders the variants; **0 is the default** the workspace opens in
      and the one a submission without a language is judged as.
    * `function_name` / `class_name` are per language because naming conventions
      differ (`merge_bookings` in Python and Rust, `mergeBookings` in JS). Which
      one is set follows the problem's `kind`, checked by `ProblemIn` for every
      variant (a CHECK can't read `problems.kind` from here).
    * `params` / `return_type` are per language too: the `type` strings are
      display hints in that language (`list[int]`, `Vec<i32>`), except the codec
      names (`ListNode`, …), which the harness decodes with (Python), or which make
      the glue define that node struct (Rust). `SandboxProfile.node_types` lists
      each language's.
    * `note_md` is a short language-specific addendum to the shared statement,
      e.g. "times don't fit in an `i32`" for Rust. Empty for most variants.
    """

    __tablename__ = "problem_languages"
    __table_args__ = (
        UniqueConstraint("problem_id", "language", name="uq_problem_language"),
        UniqueConstraint("problem_id", "ordinal", name="uq_problem_language_ordinal"),
        CheckConstraint(
            "language IN ('python', 'js', 'rust', 'mysql')", name="ck_problem_languages_language"),
    )

    problem_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("problems.id", ondelete="CASCADE"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    starter_code: Mapped[str] = mapped_column(Text, nullable=False)
    function_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    class_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    params: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # Non-empty only for a function returning a node graph (`ListNode`, …), in a
    # language with that codec: the harness flattens it back to JSON before
    # comparing. "" = plain JSON.
    return_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    note_md: Mapped[str] = mapped_column(Text, nullable=False, server_default="")

    problem: Mapped["Problem"] = relationship(back_populates="languages")  # noqa: F821
