"""The `users` table: accounts, credentials, verification, and two-factor state."""
from sqlalchemy import BigInteger, Boolean, Text
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, PKMixin, TimestampMixin

class User(Base, PKMixin, TimestampMixin):
    """A registered account.

    * `email` / `username` — `CITEXT` (case-insensitive text), so `Alice@x.com`
      and `alice@x.com` collide on the unique index — no manual lowercasing, and
      no way to register a case-variant duplicate.
    * `password_hash` — the bcrypt hash only; we never store the plaintext (see
      `app/security.py`).
    * `email_verified` — gates judging via `require_verified`; set by the
      verify-email flow (`services/account_service.py`).
    * `totp_secret_enc` / `totp_enabled` / `totp_last_step` — two-factor auth
      (`app/totp.py`, `services/totp_service.py`). The secret is stored **encrypted**
      (Fernet), never plaintext. It's written at enrollment *before* it's active:
      `totp_enabled` flips true only once the user proves their authenticator works, so a
      half-finished setup can't lock anyone out. `totp_last_step` is the newest 30s time
      step already accepted, which is what makes a code single-use.
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    username: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false")
    totp_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
