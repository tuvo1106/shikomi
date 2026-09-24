"""Auth business logic: registration, login, and the refresh-token lifecycle.

This is where the session security model lives (DESIGN.md §3.6). The design in one
breath: a short-lived stateless JWT for access, backed by a long-lived *rotating*
refresh token that IS stored (hashed) so it can be revoked. Every refresh burns
the old token and mints a new one; presenting an already-burned token means a copy
leaked, so we nuke every session for that user. Routers own the HTTP/cookie side;
this module owns the DB rules.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.config import get_settings
from app.errors import APIError
from app.models import RefreshToken, User
from app.password_policy import enforce_password_policy
from app.queue import Queue
from app.schemas.auth import RegisterRequest
from app.services import account_service
from app.security import (
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)

settings = get_settings()


# A real bcrypt hash of a random throwaway password. `authenticate` checks against
# it when the email doesn't exist, so an unknown email costs the same bcrypt work
# as a wrong password instead of returning measurably faster.
_DUMMY_HASH = hash_password(generate_refresh_token())


async def register(session: AsyncSession, data: RegisterRequest,
                   queue: Queue) -> tuple[str, User]:
    """Handle a signup without revealing whether the email is already registered.

    The HTTP response is the same 202 either way; only the *inbox* learns which
    case happened:

    - **new email** → create the (unverified) account and mail a verify link;
    - **taken email** → change nothing and mail the existing owner a "you already
      have an account" note with a password-reset link.

    That only works because an unverified account can't log in (`authenticate`):
    otherwise "register, then try logging in with the password I just chose"
    would succeed for a new email and fail for a taken one — the same oracle
    one request later.

    Usernames are handled differently: a taken username is a 409, because the
    user has to pick another one and there's no inbox to tell them privately.
    It's checked *before* the email, independent of it — if a taken email
    skipped the username check, pairing a known-taken username with a guessed
    email would turn the 409-vs-202 difference back into an email oracle.

    Both paths do the same expensive work (bcrypt, a token, one email) so their
    timing is close; the new-account path's extra insert is a few milliseconds
    against the email send.

    Returns:
        `("created", new_user)` or `("existing", owner)`, for the caller's audit
        log only — never for the response.

    Raises:
        APIError(422, WEAK_PASSWORD / BREACHED_PASSWORD): checked first, so a
            rejected password gets the same answer whether or not the email exists.
        APIError(409, USERNAME_TAKEN): the username belongs to another account.
    """
    await enforce_password_policy(data.password, email=data.email, username=data.username)
    password_hash = hash_password(data.password)  # on both paths, for timing parity
    if await _username_taken(session, data.username):
        raise APIError(409, "USERNAME_TAKEN", "That username is taken — choose another.")
    existing = await _user_by_email(session, data.email)
    if existing is not None:
        await account_service.enqueue_account_email(
            queue, account_service.EMAIL_EXISTING, existing.email)
        return "existing", existing

    user = User(email=data.email, username=data.username, password_hash=password_hash)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        # Lost a race with a concurrent signup between the checks above and this
        # insert; the unique constraints (not the pre-checks) are the real
        # guarantee. Work out which one fired and answer as if we'd seen it first.
        await session.rollback()
        if await _username_taken(session, data.username):
            raise APIError(409, "USERNAME_TAKEN", "That username is taken — choose another.")
        existing = await _user_by_email(session, data.email)
        if existing is None:
            raise
        await account_service.enqueue_account_email(
            queue, account_service.EMAIL_EXISTING, existing.email)
        return "existing", existing
    await session.refresh(user)
    await account_service.enqueue_account_email(
        queue, account_service.EMAIL_VERIFY, user.email)
    return "created", user


async def _username_taken(session: AsyncSession, username: str) -> bool:
    return await session.scalar(select(User.id).where(User.username == username)) is not None


async def _user_by_email(session: AsyncSession, email: str) -> User | None:
    return await session.scalar(select(User).where(User.email == email))


async def authenticate(session: AsyncSession, email: str, password: str) -> User | None:
    """Return the user iff the email exists, the password matches, and the email
    is verified; else None.

    Returns `None` for "no such user", "wrong password", *and* "not verified yet",
    so the caller emits one identical error (anti-enumeration). The unverified
    case is what makes register's uniform 202 hold: without it, logging in with
    the password you just registered would succeed for a new email and fail for a
    taken one. It's checked *after* the password so it adds no timing difference.

    An unknown email still runs bcrypt, against `_DUMMY_HASH`, so it can't be
    told apart from a wrong password by response time either.
    """
    user = await _user_by_email(session, email)
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.password_hash) or not user.email_verified:
        return None
    return user


async def issue_refresh_token(session: AsyncSession, user: User, persistent: bool = True) -> str:
    """Mint a new refresh token for `user`; store its hash, return the raw value.

    The raw token goes to the client (in an httpOnly cookie); only its hash is
    persisted. `persistent` records "keep me signed in" so rotation can preserve
    the session-vs-persistent nature of the cookie.
    """
    raw = generate_refresh_token()
    expires = datetime.now(timezone.utc) + timedelta(seconds=settings.jwt_refresh_ttl_seconds)
    session.add(RefreshToken(
        user_id=user.id, token_hash=hash_refresh_token(raw), expires_at=expires,
        persistent=persistent))
    await session.commit()
    return raw


async def _revoke_all(session: AsyncSession, user_id) -> None:
    """Revoke every still-active session for a user (the reuse-detection hammer)."""
    await session.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc)))
    await account_service.end_rotation_grace(session, user_id)
    await session.commit()


# How long after a rotation the just-consumed token is still honoured (as a concurrent request,
# not theft). Covers two tabs refreshing at once, or a client retrying after a lost response.
# Short on purpose: the window is also how long a thief who replays a stolen token *right after*
# the victim rotated it would go undetected.
REFRESH_GRACE_SECONDS = 10


async def rotate_refresh_token(session: AsyncSession, raw_token: str) -> tuple[User, str, bool]:
    """Validate the presented token, consume it, and issue its replacement.

    This is the heart of the rotation scheme. Cases, in order:

    * unknown hash -> `INVALID_REFRESH` (never a valid token).
    * already revoked -> either a **concurrent request** (the token was rotated within
      `REFRESH_GRACE_SECONDS`: issue an extra token, change nothing else) or **reuse**
      (anything older, or revoked by logout/reuse detection: a burned token is being
      replayed, so the original was likely stolen; revoke *all* the user's sessions).
    * expired -> `REFRESH_EXPIRED`.
    * the user no longer exists (deleted after the token was issued) -> `INVALID_REFRESH`.
    * otherwise -> consume it and add a fresh row, carrying `persistent` forward.

    *Consuming is the arbiter.* The check "is it still unrevoked?" and the act "revoke it" used
    to be two steps, so two simultaneous requests with one token both passed the check and both
    succeeded: a stolen token became two live session chains and reuse detection never fired.
    Now the revoke is a single conditional `UPDATE ... WHERE revoked_at IS NULL`, and only the
    request whose UPDATE changed a row proceeds; the loser falls into the "already revoked" case
    above, which is exactly what it is.

    The grace window mints a *new* token for the loser rather than returning the winner's,
    because only the hash of the winner's token is stored. The cost: a thief who replays within
    the window gets a session too, so the window is short and **single-use** (spending it clears
    `rotated_at`, so a second replay is reuse and revokes everything). Only rotation sets
    `rotated_at`, and logout, a password change and reuse detection all clear it
    (`account_service.end_rotation_grace`), so a token can't be replayed into a session the user
    just ended. An expired token is refused in grace too.

    Returns:
        (user, new_raw_token, persistent) - the caller re-issues the access token
        and sets the refresh cookie to `new_raw_token`.

    Raises:
        APIError(401): INVALID_REFRESH / REFRESH_REUSE / REFRESH_EXPIRED.
    """
    token_hash = hash_refresh_token(raw_token)
    row = (await session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash))).scalar_one_or_none()

    if row is None:
        raise APIError(401, "INVALID_REFRESH", "Invalid refresh token.")

    now = datetime.now(timezone.utc)
    if row.revoked_at is None:
        if row.expires_at <= now:
            raise APIError(401, "REFRESH_EXPIRED", "Refresh token expired.")
        # The claim: exactly one concurrent caller sees rowcount == 1.
        claimed = await session.execute(
            update(RefreshToken)
            .where(RefreshToken.id == row.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now, rotated_at=now))
        if claimed.rowcount == 1:
            return await _issue_successor(session, row)
        # Lost the race: someone consumed it between our read and our UPDATE.
        await session.refresh(row)

    if (row.rotated_at is not None and row.expires_at > now
            and now - row.rotated_at <= timedelta(seconds=REFRESH_GRACE_SECONDS)):
        # Grace is single-use: spend it with a conditional UPDATE so a second replay of the same
        # token (a thief hammering the window) finds it gone and is treated as reuse.
        spent = await session.execute(
            update(RefreshToken)
            .where(RefreshToken.id == row.id, RefreshToken.rotated_at == row.rotated_at)
            .values(rotated_at=None))
        if spent.rowcount == 1:
            audit("auth.refresh.concurrent", user_id=row.user_id)
            return await _issue_successor(session, row)

    # Reuse of an already-consumed token -> treat as theft, kill all sessions.
    await _revoke_all(session, row.user_id)
    audit("auth.refresh.reuse", user_id=row.user_id)
    raise APIError(401, "REFRESH_REUSE", "Refresh token reuse detected; please log in again.")


async def _issue_successor(session: AsyncSession, row: RefreshToken) -> tuple[User, str, bool]:
    """Commit the (already-applied) consume and mint the replacement for `row`."""
    user = await session.get(User, row.user_id)
    if user is None:
        # The user was deleted after this token was issued; treat it like any
        # other invalid token rather than dereferencing None below.
        await session.commit()
        raise APIError(401, "INVALID_REFRESH", "Invalid refresh token.")
    new_raw = generate_refresh_token()
    expires = datetime.now(timezone.utc) + timedelta(seconds=settings.jwt_refresh_ttl_seconds)
    session.add(RefreshToken(
        user_id=user.id, token_hash=hash_refresh_token(new_raw), expires_at=expires,
        persistent=row.persistent))
    await session.commit()
    return user, new_raw, row.persistent


async def revoke_refresh_token(session: AsyncSession, raw_token: str) -> None:
    """Revoke a single session (logout). No-op if the token is unknown/already gone."""
    token_hash = hash_refresh_token(raw_token)
    row = (await session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash))).scalar_one_or_none()
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        # Logging out ends the session, so a predecessor replayed inside its grace window must
        # not revive it.
        await account_service.end_rotation_grace(session, row.user_id)
        await session.commit()
