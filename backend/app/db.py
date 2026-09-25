"""The database layer: one async engine + a session factory, shared app-wide.

Mental model:

* The **engine** owns the connection pool. It's created once at import and lives
  for the process — creating engines per request would defeat pooling. Because
  the app is `async`, we use `create_async_engine` (backed by asyncpg) so DB I/O
  yields to the event loop instead of blocking a thread.
* `pool_pre_ping=True` cheaply checks that a pooled connection is still alive
  before handing it out, so a connection dropped by the DB (restart, idle
  timeout) reconnects transparently instead of erroring mid-request.
* A **session** is the unit of work / identity map for one request. We make a
  fresh one per request (see `get_session`) so requests never share transaction
  state.
* `expire_on_commit=False`: by default SQLAlchemy expires ORM objects after
  `commit()`, so the next attribute access re-queries — which explodes in async
  code (a lazy load off the event loop). Disabling it lets us keep using an
  object after commit (e.g. serialize a just-created row) with no surprise query.

(DESIGN.md §2)
"""
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_settings = get_settings()

engine = create_async_engine(_settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a request-scoped session and cleans it up.

    Declaring `session: AsyncSession = Depends(get_session)` on a route gives it a
    fresh session; the `async with` guarantees the session (and its connection) is
    returned to the pool when the request ends, even on error. Tests override this
    dependency to bind sessions to their own throwaway engine.
    """
    async with SessionLocal() as session:
        yield session
