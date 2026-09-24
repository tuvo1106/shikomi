"""Account jobs for the arq worker: sending account emails and purging stale signups.

Kept apart from `worker/judge.py` (judging + its safety-net sweep) because this
has nothing to do with sandboxes or submissions — it's about accounts: mailing
them (`send_account_email`) and keeping the `users` table honest
(`purge_unverified_users`).
"""
import logging
from datetime import datetime, timedelta, timezone

from arq import Retry
from sqlalchemy import delete, exists, or_
from sqlalchemy.exc import DBAPIError

from app.audit import audit
from app.config import get_settings
from app.db import SessionLocal
from app.models import EmailToken, Submission, User
from app.models.email_token import PURPOSE_VERIFY
from app.services import account_service

logger = logging.getLogger("accounts")
settings = get_settings()

# Hard age cap, as a multiple of the verify-link TTL. Resend-verification is
# unauthenticated, so a live link can't shield an account forever: a squatter
# would just keep requesting fresh ones. Past this age it's purged regardless.
HARD_CAP_TTLS = 3

# arq attempts for the email job. Only used for transient DB errors *before* a token
# exists (SMTP failures are retried inside `send_best_effort`, without re-minting).
EMAIL_MAX_TRIES = 3
EMAIL_RETRY_DEFER_SECONDS = 5


async def send_account_email(ctx, kind: str, email: str) -> None:
    """Send a verify / existing-account / reset email (moved off the request path).

    The API enqueues `(kind, email)` for every register / resend / reset request,
    known address or not; this job does the lookup, the token minting and the SMTP
    conversation. That keeps slow SMTP out of the request (so its timing can't
    reveal whether an address is registered) and keeps raw tokens out of Redis.
    `send_best_effort` retries the SMTP hop itself (minting the token once, so a
    flaky mail server can't leave extra live links) and then swallows the failure.
    A transient DB error (including Postgres being unreachable), which happens before any token exists, is different: the
    job is safe to re-run, so it's retried via arq (`EMAIL_MAX_TRIES`). (DESIGN.md §4.1)
    """
    try:
        async with SessionLocal() as session:
            await account_service.deliver_account_email(session, kind, email)
    except (DBAPIError, OSError):  # OSError: asyncpg's raw connect failures aren't wrapped
        tries = ctx.get("job_try", 1)
        if tries >= EMAIL_MAX_TRIES:
            logger.exception("account email %s dropped after %d tries", kind, tries)
            return
        logger.warning("account email %s hit a DB error (try %d); retrying", kind, tries)
        raise Retry(defer=EMAIL_RETRY_DEFER_SECONDS * tries)


async def purge_unverified_users(ctx) -> None:
    """Cron (hourly): delete signups that never verified their email.

    Why this exists: `register` answers identically whether or not the email is
    taken (anti-enumeration), so an unverified row is invisible to everyone —
    it can't log in, yet it keeps holding its username (and its email, until the
    real owner registers). Without a purge, anyone could squat usernames by
    signing up with addresses they don't control. Deleting (rather than
    expiring) keeps `users` honest, and the real owner can simply register again.

    An account is purged only when *all* of these hold:

    - `email_verified` is false;
    - it has no submissions. An unverified account can't normally submit
      (`require_verified`), but the cascade below would wipe any history it
      did have, so a purge never destroys submissions;
    - it's older than the verify-link TTL, so the original link is dead too;
    - it has no live (unused, unexpired) verify token, *unless* it's older than
      `HARD_CAP_TTLS` × the TTL. "Resend verification" issues a fresh link, so
      someone actively verifying a day-old signup is left alone until that link
      lapses; but resend is unauthenticated, so the exemption must have a ceiling
      or a squatter could renew it forever.

    It's one atomic DELETE with the conditions in its WHERE clause, so a user
    who verifies mid-run can't be caught by a stale SELECT. Their `email_tokens`
    and `refresh_tokens` rows go with them via `ON DELETE CASCADE`.
    """
    now = datetime.now(timezone.utc)
    ttl = timedelta(seconds=settings.email_verify_ttl_seconds)
    cutoff = now - ttl
    hard_cutoff = now - HARD_CAP_TTLS * ttl
    has_submissions = exists().where(Submission.user_id == User.id)
    live_verify_token = exists().where(
        EmailToken.user_id == User.id,
        EmailToken.purpose == PURPOSE_VERIFY,
        EmailToken.used_at.is_(None),
        EmailToken.expires_at > now)

    async with SessionLocal() as session:
        result = await session.execute(
            delete(User).where(
                User.email_verified.is_(False),
                ~has_submissions,
                User.created_at < cutoff,
                or_(~live_verify_token, User.created_at < hard_cutoff)))
        await session.commit()

    if result.rowcount:
        logger.info("purged %d unverified account(s)", result.rowcount)
        audit("auth.unverified.purged", count=result.rowcount)
