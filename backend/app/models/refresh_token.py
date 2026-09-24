"""The `refresh_tokens` table: long-lived, revocable sessions (DESIGN.md §3.6)."""
import uuid
from datetime import datetime

from sqlalchemy import UUID, Boolean, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin, TimestampMixin


class RefreshToken(Base, PKMixin, TimestampMixin):
    """A stored, revocable session — the counterpart to the stateless JWT.

    Access tokens are short-lived and can't be revoked; long-lived sessions live
    here instead. Each row is one issued token, stored only as `token_hash` (a
    DB leak yields nothing usable). Key behaviors:

    * **Rotation** — every refresh consumes the current token (sets `revoked_at`)
      and issues a new row, so a token is used at most once.
    * **Reuse detection** — presenting an already-revoked token means it was
      stolen and replayed, so the service revokes *all* of the user's sessions.
    * `expires_at` bounds a session's lifetime; `persistent` records whether it
      was a "keep me signed in" login, carried across rotations so a session-only
      login stays session-only.

    (The rotation/reuse logic lives in `services/auth_service.py`.)

    The `user_id` FK isn't auto-indexed by Postgres, but every `user_id` lookup
    here (`_revoke_all`, the change-password revoke, reuse detection) also filters
    on `revoked_at IS NULL` — so the index is partial on that, staying small (only
    a user's live sessions) rather than growing with revoked/expired history.
    """

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_user_id_active", "user_id",
              postgresql_where=text("revoked_at IS NULL")),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set (together with `revoked_at`) only when a *rotation* consumed the token, never by
    # logout or reuse detection. It is what lets a replay a few seconds after a rotation be
    # treated as a concurrent request rather than theft (see `REFRESH_GRACE_SECONDS`).
    rotated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    persistent: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
