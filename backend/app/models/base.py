"""The ORM foundation: the declarative base and the mixins every table reuses.

SQLAlchemy's *declarative* style maps a Python class to a table; all models
inherit `Base` so they share one metadata registry (which Alembic reads to
autogenerate migrations, and tests read to `create_all`). The two mixins factor
out the columns every table wants — a UUID primary key and audit timestamps — so
each model only declares what's unique to it. (DESIGN.md §3)
"""
import uuid
from datetime import datetime

from sqlalchemy import UUID, DateTime, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared declarative base — the registry all models attach their table to."""


class PKMixin:
    """A UUID primary key generated *by the database*.

    `server_default=gen_random_uuid()` means Postgres assigns the id on INSERT, so
    ids are consistent whether a row is created via the ORM or raw SQL and we never
    round-trip to fetch a sequence value. UUIDs (vs auto-increment ints) avoid
    leaking row counts and can be generated anywhere without collision.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


class TimestampMixin:
    """`created_at` / `updated_at`, timezone-aware and maintained by the DB.

    `DateTime(timezone=True)` stores `timestamptz` (always UTC) so times are
    unambiguous. `server_default=now()` lets the DB stamp them (not the app clock),
    and `updated_at` adds `onupdate=now()` so it bumps on every UPDATE automatically.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
