"""The `solutions` table: editorial write-ups shown on a problem's Solutions tab."""
import uuid

from sqlalchemy import UUID, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, PKMixin, TimestampMixin


class Solution(Base, PKMixin, TimestampMixin):
    """One editorial approach for a problem (brute-force → optimal).

    A problem has several, `ordinal`-ordered worst-to-best (unique per problem via
    `uq_solution_ordinal`). The structured fields mirror the on-page sections:
    `intuition_md` and `algorithm_md` are Markdown prose, and the `*_complexity` /
    `*_complexity_reason` pairs render the "Complexity" block (the complexities
    are LaTeX, e.g. `O(n \\log n)`).

    The reference code is per language, in `codes` (`SolutionCode`): the idea and
    its complexity are shared, the implementation isn't. A solution needn't cover
    every language the problem offers; the workspace labels it "Rust only" and
    the like.
    """

    __tablename__ = "solutions"
    __table_args__ = (UniqueConstraint("problem_id", "ordinal", name="uq_solution_ordinal"),)

    problem_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("problems.id", ondelete="CASCADE"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    intuition_md: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm_md: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    time_complexity: Mapped[str] = mapped_column(Text, nullable=False)
    space_complexity: Mapped[str] = mapped_column(Text, nullable=False)
    time_complexity_reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    space_complexity_reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")

    problem: Mapped["Problem"] = relationship(back_populates="solutions")  # noqa: F821
    codes: Mapped[list["SolutionCode"]] = relationship(
        back_populates="solution", cascade="all, delete-orphan",
        order_by="SolutionCode.language", passive_deletes=True, lazy="selectin")


class SolutionCode(Base, PKMixin, TimestampMixin):
    """One solution's reference implementation in one language.

    Unique per `(solution_id, language)`. `lazy="selectin"` on `Solution.codes`
    loads them in one extra query with the solutions, which is what the
    Solutions tab always wants (an async session can't lazy-load on access).
    """

    __tablename__ = "solution_codes"
    __table_args__ = (
        UniqueConstraint("solution_id", "language", name="uq_solution_code_language"),)

    solution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("solutions.id", ondelete="CASCADE"), nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)

    solution: Mapped[Solution] = relationship(back_populates="codes")
