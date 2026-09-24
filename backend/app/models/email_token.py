import uuid
from datetime import datetime

from sqlalchemy import UUID, DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin, TimestampMixin

# Single-use, hashed tokens for out-of-band email flows.
PURPOSE_VERIFY = "verify_email"
PURPOSE_RESET = "password_reset"


class EmailToken(Base, PKMixin, TimestampMixin):
    """Hashed, expiring, single-use tokens emailed to the user (never stored raw)."""
    __tablename__ = "email_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
