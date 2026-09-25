"""Direct auth_service unit tests for branches the API flow can't easily reach."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.errors import APIError
from app.models import RefreshToken, User
from app.security import hash_password, hash_refresh_token
from app.services import auth_service


async def _make_user(session_factory):
    async with session_factory() as s:
        user = User(email="e@x.com", username="ex", password_hash=hash_password("password123"))
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return user


async def test_rotate_rejects_expired_token(session_factory):
    user = await _make_user(session_factory)
    async with session_factory() as s:
        s.add(RefreshToken(user_id=user.id, token_hash=hash_refresh_token("raw"),
                           expires_at=datetime.now(timezone.utc) - timedelta(days=1)))
        await s.commit()

    async with session_factory() as s:
        with pytest.raises(APIError) as exc:
            await auth_service.rotate_refresh_token(s, "raw")
    assert exc.value.code == "REFRESH_EXPIRED"


async def test_rotate_rejects_unknown_token(session_factory):
    async with session_factory() as s:
        with pytest.raises(APIError) as exc:
            await auth_service.rotate_refresh_token(s, "never-issued")
    assert exc.value.code == "INVALID_REFRESH"


async def test_rotate_rejects_token_whose_user_was_deleted(session_factory):
    # refresh_tokens.user_id cascades on delete, so there's no user-deletion path
    # that leaves an orphaned token today — bypass the FK to simulate the
    # future/manual-op case this guards against.
    user = await _make_user(session_factory)
    async with session_factory() as s:
        s.add(RefreshToken(user_id=user.id, token_hash=hash_refresh_token("raw"),
                           expires_at=datetime.now(timezone.utc) + timedelta(days=1)))
        await s.commit()
    async with session_factory() as s:
        await s.execute(text("SET session_replication_role = replica"))
        await s.execute(text("DELETE FROM users WHERE id = :id"), {"id": user.id})
        await s.execute(text("SET session_replication_role = DEFAULT"))
        await s.commit()

    async with session_factory() as s:
        with pytest.raises(APIError) as exc:
            await auth_service.rotate_refresh_token(s, "raw")
    assert exc.value.code == "INVALID_REFRESH"


async def test_revoke_unknown_token_is_noop(session_factory):
    async with session_factory() as s:
        await auth_service.revoke_refresh_token(s, "not-a-real-token")  # must not raise


def test_user_id_index_exists():
    """The partial (user_id WHERE revoked_at IS NULL) index _revoke_all and the
    change-password revoke rely on."""
    index = next(idx for idx in RefreshToken.__table__.indexes
                if idx.name == "ix_refresh_tokens_user_id_active")
    assert [c.name for c in index.columns] == ["user_id"]


# --- rotation is an atomic claim, with a short grace window ------------------------------------

async def _issue(session_factory, user, raw="raw"):
    async with session_factory() as s:
        s.add(RefreshToken(user_id=user.id, token_hash=hash_refresh_token(raw),
                           expires_at=datetime.now(timezone.utc) + timedelta(days=1)))
        await s.commit()


async def _rows(session_factory, user):
    async with session_factory() as s:
        return (await s.execute(
            text("select revoked_at, rotated_at from refresh_tokens where user_id = :u"),
            {"u": user.id})).all()


async def test_a_replay_just_after_rotation_is_a_concurrent_request_not_theft(session_factory):
    """Two tabs refreshing together, or a retry after a lost response: the loser gets a token
    of its own and nobody is logged out."""
    user = await _make_user(session_factory)
    await _issue(session_factory, user)
    async with session_factory() as s:
        await auth_service.rotate_refresh_token(s, "raw")
    async with session_factory() as s:
        _, second, _ = await auth_service.rotate_refresh_token(s, "raw")   # within grace

    async with session_factory() as s:                    # the sibling is a live, usable token
        await auth_service.rotate_refresh_token(s, second)
    rows = await _rows(session_factory, user)
    # original (consumed) + first successor (still live: the replay did not revoke it) + the
    # sibling (consumed just above) + its own successor (live)
    assert sorted(revoked is None for revoked, _ in rows) == [False, False, True, True]


async def test_a_replay_after_the_grace_window_is_reuse_and_revokes_everything(session_factory):
    user = await _make_user(session_factory)
    await _issue(session_factory, user)
    async with session_factory() as s:
        _, live, _ = await auth_service.rotate_refresh_token(s, "raw")
    async with session_factory() as s:
        await s.execute(text("update refresh_tokens set rotated_at = :t where revoked_at is not null"),
                        {"t": datetime.now(timezone.utc)
                         - timedelta(seconds=auth_service.REFRESH_GRACE_SECONDS + 5)})
        await s.commit()

    async with session_factory() as s:
        with pytest.raises(APIError) as exc:
            await auth_service.rotate_refresh_token(s, "raw")
    assert exc.value.code == "REFRESH_REUSE"
    async with session_factory() as s:                    # the live successor was revoked too
        with pytest.raises(APIError) as exc:
            await auth_service.rotate_refresh_token(s, live)
    assert exc.value.code == "REFRESH_REUSE"


async def test_a_logged_out_token_is_never_in_grace(session_factory):
    """Logout sets `revoked_at` but not `rotated_at`, so replaying it right after is reuse."""
    user = await _make_user(session_factory)
    await _issue(session_factory, user)
    async with session_factory() as s:
        await auth_service.revoke_refresh_token(s, "raw")
    async with session_factory() as s:
        with pytest.raises(APIError) as exc:
            await auth_service.rotate_refresh_token(s, "raw")
    assert exc.value.code == "REFRESH_REUSE"


async def test_losing_the_claim_race_is_not_a_second_success(session_factory):
    """The bug: read-check-then-revoke let two requests both pass the check. Simulate the
    interleaving exactly: between this request's read and its UPDATE, another request rotates
    the token (and the moment passes). The UPDATE must change nothing and the request must be
    handled as reuse, not quietly succeed off its stale read."""
    user = await _make_user(session_factory)
    await _issue(session_factory, user)

    class Interleaved:
        """Delegates to a real session, but right after the first query (the read) lets a rival
        request rotate the token and age past the grace window."""
        def __init__(self, session):
            self._s, self._raced = session, False

        def __getattr__(self, name):
            return getattr(self._s, name)

        async def execute(self, *args, **kwargs):
            result = await self._s.execute(*args, **kwargs)
            if not self._raced:
                self._raced = True
                async with session_factory() as rival:
                    await auth_service.rotate_refresh_token(rival, "raw")
                    await rival.execute(text(
                        "update refresh_tokens set rotated_at = now() - interval '1 hour' "
                        "where revoked_at is not null"))
                    await rival.commit()
            return result

    async with session_factory() as s:
        with pytest.raises(APIError) as exc:
            await auth_service.rotate_refresh_token(Interleaved(s), "raw")
    assert exc.value.code == "REFRESH_REUSE"
    assert all(revoked is not None for revoked, _ in await _rows(session_factory, user))


async def test_the_grace_window_is_single_use(session_factory):
    """A thief hammering a just-rotated token gets one session at most: the second replay finds
    the grace spent and is reuse, which revokes every session."""
    user = await _make_user(session_factory)
    await _issue(session_factory, user)
    async with session_factory() as s:
        await auth_service.rotate_refresh_token(s, "raw")
    async with session_factory() as s:
        await auth_service.rotate_refresh_token(s, "raw")            # first replay: grace
    async with session_factory() as s:
        with pytest.raises(APIError) as exc:
            await auth_service.rotate_refresh_token(s, "raw")        # second: reuse
    assert exc.value.code == "REFRESH_REUSE"
    assert all(revoked is not None for revoked, _ in await _rows(session_factory, user))


async def test_logging_out_ends_the_grace_of_earlier_rotations(session_factory):
    """Rotate A -> B, log out B, then replay A inside its window: must not mint a session."""
    user = await _make_user(session_factory)
    await _issue(session_factory, user)
    async with session_factory() as s:
        _, b, _ = await auth_service.rotate_refresh_token(s, "raw")
    async with session_factory() as s:
        await auth_service.revoke_refresh_token(s, b)

    async with session_factory() as s:
        with pytest.raises(APIError) as exc:
            await auth_service.rotate_refresh_token(s, "raw")
    assert exc.value.code == "REFRESH_REUSE"


async def test_an_expired_token_gets_no_grace(session_factory):
    user = await _make_user(session_factory)
    await _issue(session_factory, user)
    async with session_factory() as s:
        await auth_service.rotate_refresh_token(s, "raw")
    async with session_factory() as s:
        await s.execute(text("update refresh_tokens set expires_at = now() - interval '1 second' "
                             "where revoked_at is not null"))
        await s.commit()
    async with session_factory() as s:
        with pytest.raises(APIError) as exc:
            await auth_service.rotate_refresh_token(s, "raw")
    assert exc.value.code == "REFRESH_REUSE"
