"""Unverified-signup purge cron (`worker/accounts.py`)."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import get_settings
from app.models import EmailToken, Submission, User
from app.models.email_token import PURPOSE_VERIFY
from worker import accounts as accounts_mod
from worker.accounts import HARD_CAP_TTLS

TTL = timedelta(seconds=get_settings().email_verify_ttl_seconds)


async def _age(session_factory, email, by):
    async with session_factory() as s:
        user = await s.scalar(select(User).where(User.email == email))
        user.created_at = datetime.now(timezone.utc) - by
        await s.commit()


async def _emails(session_factory):
    async with session_factory() as s:
        return set((await s.scalars(select(User.email))).all())


async def _purge(session_factory, monkeypatch):
    monkeypatch.setattr(accounts_mod, "SessionLocal", session_factory)
    await accounts_mod.purge_unverified_users({})


async def test_purges_old_unverified_and_frees_the_username(
        session_factory, make_user, monkeypatch):
    await make_user(email="old@example.com", username="squatter", verified=False)
    await _age(session_factory, "old@example.com", TTL + timedelta(hours=1))

    await _purge(session_factory, monkeypatch)

    assert "old@example.com" not in await _emails(session_factory)
    # The name is free again: the real owner can register with it.
    await make_user(email="owner@example.com", username="squatter")


async def test_spares_recent_unverified(session_factory, make_user, monkeypatch):
    await make_user(email="new@example.com", username="new", verified=False)

    await _purge(session_factory, monkeypatch)

    assert "new@example.com" in await _emails(session_factory)


async def test_spares_old_verified(session_factory, make_user, monkeypatch):
    await make_user(email="v@example.com", username="v")
    await _age(session_factory, "v@example.com", TTL * 10)

    await _purge(session_factory, monkeypatch)

    assert "v@example.com" in await _emails(session_factory)


async def test_spares_old_unverified_with_a_live_verify_link(
        session_factory, make_user, monkeypatch):
    """A resend issues a fresh link; someone mid-verification isn't purged."""
    user, _ = await make_user(email="resent@example.com", username="resent", verified=False)
    await _age(session_factory, "resent@example.com", TTL + timedelta(hours=1))
    async with session_factory() as s:
        s.add(EmailToken(user_id=user["id"], token_hash="h", purpose=PURPOSE_VERIFY,
                         expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
        await s.commit()

    await _purge(session_factory, monkeypatch)

    assert "resent@example.com" in await _emails(session_factory)


async def test_purge_cascades_tokens(session_factory, make_user, monkeypatch):
    user, _ = await make_user(email="old@example.com", username="old", verified=False)
    await _age(session_factory, "old@example.com", TTL + timedelta(hours=1))
    async with session_factory() as s:
        s.add(EmailToken(user_id=user["id"], token_hash="h", purpose=PURPOSE_VERIFY,
                         expires_at=datetime.now(timezone.utc) - timedelta(hours=1)))
        await s.commit()

    await _purge(session_factory, monkeypatch)

    async with session_factory() as s:
        assert (await s.scalars(select(EmailToken))).all() == []


async def test_hard_cap_beats_a_live_verify_link(session_factory, make_user, monkeypatch):
    """Resend is unauthenticated, so a squatter could renew links forever."""
    user, _ = await make_user(email="squat@example.com", username="squat", verified=False)
    await _age(session_factory, "squat@example.com", TTL * HARD_CAP_TTLS + timedelta(hours=1))
    async with session_factory() as s:
        s.add(EmailToken(user_id=user["id"], token_hash="h", purpose=PURPOSE_VERIFY,
                         expires_at=datetime.now(timezone.utc) + timedelta(hours=1)))
        await s.commit()

    await _purge(session_factory, monkeypatch)

    assert "squat@example.com" not in await _emails(session_factory)


async def test_spares_unverified_account_with_submissions(
        session_factory, make_user, make_problem, monkeypatch):
    """The purge's cascade would delete submissions, so an account with any is kept."""
    user, _ = await make_user(email="legacy@example.com", username="legacy", verified=False)
    pid, _ = await make_problem()
    async with session_factory() as s:
        s.add(Submission(user_id=user["id"], problem_id=pid,
                         code="x", status="accepted"))
        await s.commit()
    await _age(session_factory, "legacy@example.com", TTL * 100)

    await _purge(session_factory, monkeypatch)

    assert "legacy@example.com" in await _emails(session_factory)
