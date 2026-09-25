"""The `recovery_codes` table: one-time backup codes for two-factor auth."""
import uuid
from datetime import datetime

from sqlalchemy import UUID, DateTime, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin, TimestampMixin


class RecoveryCode(Base, PKMixin, TimestampMixin):
    """A single-use code that stands in for the authenticator when the phone is lost.

    Like refresh tokens, only a SHA-256 hash is stored (the codes are random and
    high-entropy, so a fast hash is right), so a database leak yields nothing usable.
    A code is burned by setting `used_at` with a conditional UPDATE (`WHERE used_at IS
    NULL`), so two simultaneous logins can't both spend it. Regenerating or disabling 2FA
    deletes the user's rows — the set is replaced wholesale, never edited.
    """

    __tablename__ = "recovery_codes"
    __table_args__ = (Index("ix_recovery_codes_user_id", "user_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    code_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
