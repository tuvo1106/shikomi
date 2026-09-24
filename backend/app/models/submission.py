"""The `submissions` table: one attempt (Run or Submit) and its judged verdict."""
import uuid

from sqlalchemy import UUID, Boolean, Float, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin, TimestampMixin

# The states a submission ends in. "pending"/"running" are the non-terminal ones
# the client polls through; anything here means the verdict is final. (DESIGN.md §3.5)
TERMINAL_STATUSES = (
    "accepted", "wrong_answer", "runtime_error", "time_limit_exceeded",
    "memory_limit_exceeded", "output_limit_exceeded", "judge_error",
)


class Submission(Base, PKMixin, TimestampMixin):
    """A code attempt and, once judged, its result.

    Lifecycle: created `pending` → worker sets `running` → a terminal status (see
    `TERMINAL_STATUSES`). The client polls the row until it's terminal.

    * `code` — the exact source submitted (kept so the user can view/reload it).
    * `status` — the current verdict state.
    * `verdict_detail` (JSONB) — per-case results + pass counts; JSON because the
      shape varies. Hidden-case inputs are redacted here except the first failing
      one.
    * `runtime_ms` — summed per-case runtime (Float; sub-millisecond matters).
    * `is_run` — True for "Run" (sample cases only, unthrottled, no history) vs a
      real "Submit". This one flag distinguishes the two flows across the app.

    The composite index `(user_id, problem_id, created_at DESC)` makes "this
    user's recent submissions for this problem" — the history query — an index
    scan instead of a sort. The DESC in the index matches the query's ORDER BY so
    Postgres reads rows already in the right order.

    The composite index `(problem_id, is_run, status)` serves the stats queries
    (`runtime_percentile`/`runtime_distribution`, which filter on all three)
    without a full-table scan.

    The partial index on `updated_at WHERE status IN ('pending', 'running')` serves
    the once-a-minute sweeper (`worker/sweeper.py`), whose two passes look only for
    unfinished rows. Only a handful of rows are ever unfinished, so the index stays
    tiny while the table grows without bound; without it both passes would scan
    the whole table every minute.
    """

    __tablename__ = "submissions"
    __table_args__ = (
        Index("ix_submissions_user_problem_created",
              "user_id", "problem_id", text("created_at DESC")),
        Index("ix_submissions_problem_is_run_status",
              "problem_id", "is_run", "status"),
        Index("ix_submissions_unfinished_updated", "updated_at",
              postgresql_where=text("status IN ('pending', 'running')")),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    problem_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("problems.id"), nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    verdict_detail: Mapped[dict] = mapped_column(JSONB, nullable=True)
    runtime_ms: Mapped[float] = mapped_column(Float, nullable=True)
    is_run: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
