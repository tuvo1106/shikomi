"""The `solutions` table: editorial write-ups shown on a problem's Solutions tab."""
import uuid

from sqlalchemy import UUID, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, PKMixin, TimestampMixin


class Solution(Base, PKMixin, TimestampMixin):
    """One editorial approach for a problem (brute-force → optimal).

    A problem has several, `ordinal`-ordered worst-to-best (unique per problem via
    `uq_solution_ordinal`). The structured fields mirror the on-page sections:
    `intuition_md` and `algorithm_md` are Markdown prose, `code` is the reference
    Python, and the `*_complexity` / `*_complexity_reason` pairs render the
    "Complexity" block (the complexities are LaTeX, e.g. `O(n \\log n)`).
    """

    __tablename__ = "solutions"
    __table_args__ = (UniqueConstraint("problem_id", "ordinal", name="uq_solution_ordinal"),)

    problem_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("problems.id", ondelete="CASCADE"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    intuition_md: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm_md: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    code: Mapped[str] = mapped_column(Text, nullable=False)
    time_complexity: Mapped[str] = mapped_column(Text, nullable=False)
    space_complexity: Mapped[str] = mapped_column(Text, nullable=False)
    time_complexity_reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    space_complexity_reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")

    problem: Mapped["Problem"] = relationship(back_populates="solutions")  # noqa: F821
