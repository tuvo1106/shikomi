"""Out-of-band email flows: email verification and password reset.

Both flows share one primitive — a random, single-use, expiring token whose hash
is stored in `email_tokens` and whose raw value is emailed as a link. The pattern:
`_issue_token` creates one and mails the link; the user clicks; `_consume_token`
validates and burns it. Storing only the hash means a DB leak exposes no working
links; single-use + expiry limit a leaked link's window.

Anti-enumeration is deliberate here: `request_password_reset` returns normally
whether or not the email exists (the router always replies 202), so an attacker
can't probe which addresses are registered.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.email import send_best_effort
from app.errors import APIError
from app.models import EmailToken, RefreshToken, User
from app.models.email_token import PURPOSE_RESET, PURPOSE_VERIFY
from app.password_policy import enforce_password_policy
from app.queue import Queue
from app.security import generate_token, hash_password, hash_token, verify_password

settings = get_settings()
logger = logging.getLogger("app.account")

# Kinds of account email the worker can send (`deliver_account_email`).
EMAIL_VERIFY = "verify"
EMAIL_EXISTING = "existing"
EMAIL_RESET = "reset"


async def enqueue_account_email(queue: Queue, kind: str, email: str) -> None:
    """Ask the worker to send an account email, without ever failing the request.

    Every flow that mails (register, resend, reset) enqueues the *same* cheap job
    whether or not the address has an account, so the request's timing no longer
    depends on it: before, the paths that sent an SMTP message were seconds slower
    than the ones that didn't, which is a timing oracle for account existence.

    Best-effort by the same contract as `email.send_best_effort`: the user-visible
    state is already committed, so a Redis hiccup is logged, not raised — a raise
    would 500 an otherwise-successful signup, and on the reset path would be a
    status difference between known and unknown addresses.
    """
    try:
        await queue.enqueue_email(kind, email)
    except Exception:  # noqa: BLE001 — best-effort by contract (see docstring)
        logger.exception("could not enqueue %s email", kind)


async def deliver_account_email(session: AsyncSession, kind: str, email: str) -> None:
    """Worker side: look up `email` and send it the `kind` of message.

    A no-op for an unknown address, which is why the API can enqueue blindly.
    Tokens are minted here (not in the request) so they only ever exist in the DB
    and the outgoing message.
    """
    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        return
    if kind == EMAIL_VERIFY:
        await send_verification_email(session, user)  # a no-op once verified
    elif kind == EMAIL_EXISTING:
        await send_existing_account_email(session, user)
    elif kind == EMAIL_RESET:
        await send_password_reset_email(session, user)
    else:
        logger.error("unknown account email kind %r", kind)


async def _issue_token(session: AsyncSession, user: User, purpose: str, ttl: int) -> str:
    raw = generate_token()
    session.add(EmailToken(
        user_id=user.id, token_hash=hash_token(raw), purpose=purpose,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl)))
    await session.commit()
    return raw


async def _consume_token(session: AsyncSession, raw: str, purpose: str) -> EmailToken:
    """Validate a token for `purpose` and mark it used. 400 on any problem."""
    row = (await session.execute(
        select(EmailToken).where(EmailToken.token_hash == hash_token(raw)))).scalar_one_or_none()
    if row is None or row.purpose != purpose:
        raise APIError(400, "INVALID_TOKEN", "This link is invalid.")
    if row.used_at is not None:
        raise APIError(400, "TOKEN_USED", "This link has already been used.")
    if row.expires_at <= datetime.now(timezone.utc):
        raise APIError(400, "TOKEN_EXPIRED", "This link has expired.")
    row.used_at = datetime.now(timezone.utc)
    return row


async def send_verification_email(session: AsyncSession, user: User) -> None:
    if user.email_verified:
        return
    raw = await _issue_token(session, user, PURPOSE_VERIFY, settings.email_verify_ttl_seconds)
    link = f"{settings.frontend_base_url}/verify-email?token={raw}"
    # Best-effort: the account + token are already committed; a mail failure here
    # must not 500 an otherwise-successful registration (see send_best_effort).
    await send_best_effort(
        user.email, "Verify your shikomi email",
        f"Welcome to shikomi! Confirm your email address:\n\n{link}\n\n"
        "If you didn’t create this account, you can ignore this message.")


async def send_existing_account_email(session: AsyncSession, user: User) -> None:
    """Tell an existing account's owner that someone tried to register their email.

    The other half of register's anti-enumeration (`auth_service.register`): the
    HTTP response to a signup is identical whether or not the email is taken, so
    this email is the only place the difference shows — and only its owner can
    read it. It carries a reset link, since the likeliest sender is the owner
    themselves, having forgotten they signed up.
    """
    raw = await _issue_token(session, user, PURPOSE_RESET, settings.password_reset_ttl_seconds)
    link = f"{settings.frontend_base_url}/reset-password?token={raw}"
    await send_best_effort(
        user.email, "You already have a shikomi account",
        "Someone (hopefully you) just tried to create a shikomi account with this "
        "email address, but you already have one.\n\n"
        f"Sign in: {settings.frontend_base_url}/login\n\n"
        f"Forgot your password? Reset it (the link expires in an hour):\n\n{link}\n\n"
        "If this wasn’t you, you can ignore this message — nothing about your account "
        "has changed.")


async def resend_verification_email(queue: Queue, email: str) -> None:
    """Queue a fresh verify link for `email` (the worker skips unknown/verified ones).

    Takes an email rather than a session because unverified accounts can't log
    in. It always enqueues, so the caller's response and timing are identical for
    an unknown or already-verified address.
    """
    await enqueue_account_email(queue, EMAIL_VERIFY, email)


async def verify_email(session: AsyncSession, raw: str) -> User:
    """Consume a verify-email token and mark the owning user verified.

    Raises:
        APIError(400): INVALID_TOKEN / TOKEN_USED / TOKEN_EXPIRED (via
            `_consume_token`), or INVALID_TOKEN if the user was deleted after
            the token was issued.
    """
    row = await _consume_token(session, raw, PURPOSE_VERIFY)
    user = await session.get(User, row.user_id)
    if user is None:
        # The user was deleted after this token was issued.
        raise APIError(400, "INVALID_TOKEN", "This link is invalid.")
    user.email_verified = True
    await session.commit()
    return user


async def request_password_reset(queue: Queue, email: str) -> None:
    """Queue a reset link for `email`; the response is identical either way so the
    endpoint never reveals whether an address is registered."""
    await enqueue_account_email(queue, EMAIL_RESET, email)


async def send_password_reset_email(session: AsyncSession, user: User) -> None:
    """Issue a reset token for `user` and mail the link (worker side)."""
    raw = await _issue_token(session, user, PURPOSE_RESET, settings.password_reset_ttl_seconds)
    link = f"{settings.frontend_base_url}/reset-password?token={raw}"
    # Best-effort: the token is already committed; see send_best_effort.
    await send_best_effort(
        user.email, "Reset your shikomi password",
        f"We received a request to reset your password:\n\n{link}\n\n"
        "This link expires in an hour. If you didn’t ask for this, ignore this message.")


async def change_password(session: AsyncSession, user: User, current_password: str,
                          new_password: str, *, keep_token_hash: str | None = None) -> User:
    """Change the password of a signed-in user (Settings page).

    Unlike the reset flow (which proves ownership via an emailed token), here the
    user is already authenticated, so we re-check the *current* password as the
    proof-of-presence — a stolen access token alone shouldn't be able to change it.

    Rotating the password revokes every *other* session (`keep_token_hash` is the
    caller's current refresh token, preserved so they stay logged in here) — so a
    password change kicks any attacker with a lingering session back out.

    Raises:
        APIError(400, INVALID_CURRENT_PASSWORD): the current password is wrong.
        APIError(422, WEAK_PASSWORD): the new password fails the strength policy.
        APIError(422, BREACHED_PASSWORD): the new password appears in a known breach.
    """
    if not verify_password(current_password, user.password_hash):
        raise APIError(400, "INVALID_CURRENT_PASSWORD", "Current password is incorrect.")
    # Enforce policy before the commit so a rejected weak password changes nothing.
    await enforce_password_policy(new_password, email=user.email, username=user.username)
    user.password_hash = hash_password(new_password)
    stmt = (update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)))
    if keep_token_hash:
        stmt = stmt.where(RefreshToken.token_hash != keep_token_hash)
    await session.execute(stmt.values(revoked_at=datetime.now(timezone.utc)))
    await end_rotation_grace(session, user.id)
    await session.commit()
    return user


async def end_rotation_grace(session: AsyncSession, user_id) -> None:
    """Forget every recent rotation for `user_id`, so no consumed token is still "in grace".

    The grace window (`auth_service.REFRESH_GRACE_SECONDS`) lets a token that was rotated a
    moment ago be replayed once as a concurrent request. That must never outlive the user
    ending their sessions: after a logout, password change or reuse detection, replaying the
    *predecessor* of a just-revoked token would otherwise mint a fresh session. Clearing
    `rotated_at` makes any such replay plain reuse. (Does not commit; the caller's revoke does.)
    """
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.rotated_at.is_not(None))
        .values(rotated_at=None))


async def reset_password(session: AsyncSession, raw: str, new_password: str) -> User:
    """Consume a reset token and set the owning user's new password.

    Raises:
        APIError(400): INVALID_TOKEN / TOKEN_USED / TOKEN_EXPIRED (via
            `_consume_token`), or INVALID_TOKEN if the user was deleted after
            the token was issued; WEAK_PASSWORD / BREACHED_PASSWORD (422) if the
            new password fails the strength policy or appears in a breach.
    """
    row = await _consume_token(session, raw, PURPOSE_RESET)
    user = await session.get(User, row.user_id)
    if user is None:
        # The user was deleted after this token was issued.
        raise APIError(400, "INVALID_TOKEN", "This link is invalid.")
    # Enforce the policy *before* the commit: if it raises, nothing (including the
    # token's used_at) persists, so a rejected weak password doesn't burn the link.
    # The breach lookup is an HTTP call made inside this transaction, after the
    # `session.get` above autoflushed `used_at` — so the token's row stays locked
    # for up to BREACHED_PASSWORD_TIMEOUT_SECONDS. That only makes a second,
    # concurrent use of the *same* link wait, which serializes it anyway.
    await enforce_password_policy(new_password, email=user.email, username=user.username)
    user.password_hash = hash_password(new_password)
    # Receiving the email proves ownership → treat the address as verified, and
    # revoke every existing session so a leaked password can't linger (§3.6).
    user.email_verified = True
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc)))
    await end_rotation_grace(session, user.id)
    await session.commit()
    return user
