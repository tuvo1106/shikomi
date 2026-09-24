"""The `test_cases` table: one input→expected pair the judge runs code against."""
import uuid

from sqlalchemy import UUID, Boolean, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, PKMixin, TimestampMixin


class TestCase(Base, PKMixin, TimestampMixin):
    """A single judged case for a problem.

    * `input` (JSONB) — the positional args passed to the solution function, as a
      JSON array (e.g. `[[4,9,1,6], 7]` for `pair_sum(nums, target)`).
    * `expected` (JSONB, nullable) — the expected return value (any JSON, incl.
      `null`).
    * `ordinal` — order within the problem; unique per problem (the
      `uq_test_case_ordinal` constraint) so cases stay stably numbered.
    * `is_sample` — sample cases are shown to users and used by "Run"; hidden
      cases are only used on "Submit", so a solution can't be reverse-engineered
      from the examples.

    Storing input/expected as JSONB keeps arbitrary shapes (nested arrays, objects)
    without a bespoke serialization format.
    """

    __tablename__ = "test_cases"
    __table_args__ = (UniqueConstraint("problem_id", "ordinal", name="uq_test_case_ordinal"),)

    problem_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("problems.id", ondelete="CASCADE"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    input: Mapped[list] = mapped_column(JSONB, nullable=False)
    expected: Mapped[object] = mapped_column(JSONB, nullable=True)
    is_sample: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    problem: Mapped["Problem"] = relationship(back_populates="test_cases")  # noqa: F821
