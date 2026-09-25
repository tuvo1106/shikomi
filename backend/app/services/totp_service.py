"""Two-factor auth business logic: enrollment, verifying a second factor, disabling.

`app/totp.py` owns the crypto-ish primitives; this module owns the *rules* around them.
The lifecycle in one breath: `begin_setup` mints and stores an (encrypted) secret that is
**not yet active**; `enable` activates it only once the user proves their authenticator
produces valid codes (so a botched setup can't lock anyone out) and hands back one-time
recovery codes; from then on login demands a code via `verify_second_factor`; `disable`
undoes it all. (DESIGN.md §4.1)

Every check that "spends" something — a TOTP time step, a recovery code — does so with a
*conditional UPDATE* and trusts its row count, not a read-then-write, so two simultaneous
requests carrying the same code can't both succeed.
"""
from datetime import datetime, timezone

from sqlalchemy import delete, or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import totp
from app.audit import audit
from app.errors import APIError
from app.models import RecoveryCode, User
from app.security import verify_password


def _is_totp_code(code: str) -> bool:
    """Six digits (spaces allowed) is a TOTP code; anything else is tried as a recovery code.

    One input field on the client, one rule here: recovery codes are 16 hex chars, so the
    two shapes can never be confused.
    """
    compact = code.replace(" ", "")
    return len(compact) == totp.DIGITS and compact.isascii() and compact.isdigit()


async def _replace_recovery_codes(session: AsyncSession, user: User) -> list[str]:
    """Discard every existing recovery code for `user` and issue a fresh set.

    Wholesale replacement (never edit-in-place) means "regenerate" reliably invalidates
    codes the user may have leaked or lost. Returns the plaintext codes — the only time
    they exist; only their hashes are stored.
    """
    await session.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    codes = totp.generate_recovery_codes()
    session.add_all(RecoveryCode(user_id=user.id, code_hash=totp.hash_recovery_code(c))
                    for c in codes)
    return codes


async def begin_setup(session: AsyncSession, user: User) -> tuple[str, str]:
    """Start (or restart) enrollment: store a new pending secret, return it and its URI.

    Calling it again before `enable` just replaces the pending secret, so an abandoned
    scan is harmless. It does *not* turn 2FA on — `totp_enabled` stays false.

    Returns:
        `(secret, otpauth_uri)`. The secret is shown so users can type it in when they
        can't scan the QR code.

    Raises:
        APIError(409, TOTP_ALREADY_ENABLED): disable 2FA first to re-enroll.
    """
    if user.totp_enabled:
        raise APIError(409, "TOTP_ALREADY_ENABLED", "Two-factor authentication is already on.")
    secret = totp.generate_secret()
    user.totp_secret_enc = totp.seal(secret)
    await session.commit()
    return secret, totp.provisioning_uri(secret, user.email)


async def enable(session: AsyncSession, user: User, code: str) -> list[str]:
    """Activate 2FA after the user proves their authenticator works; return recovery codes.

    Raises:
        APIError(409, TOTP_ALREADY_ENABLED)
        APIError(400, TOTP_NOT_STARTED): `begin_setup` hasn't been called.
        APIError(400, INVALID_CODE): the code doesn't match the pending secret.
    """
    if user.totp_enabled:
        raise APIError(409, "TOTP_ALREADY_ENABLED", "Two-factor authentication is already on.")
    if not user.totp_secret_enc:
        raise APIError(400, "TOTP_NOT_STARTED", "Start two-factor setup first.")
    step = totp.verify(totp.unseal(user.totp_secret_enc), code)
    if step is None:
        raise APIError(400, "INVALID_CODE", "That code isn't right. Check the app and try again.")
    user.totp_enabled = True
    user.totp_last_step = step  # the code that proved setup can't be replayed at login
    codes = await _replace_recovery_codes(session, user)
    await session.commit()
    audit("auth.2fa.enabled", user_id=user.id, email=user.email)
    return codes


async def verify_second_factor(session: AsyncSession, user: User, code: str) -> bool:
    """Check a TOTP code or a recovery code for an enrolled user, spending it if valid.

    Returns True at most once per code. A TOTP code is refused unless its time step is
    newer than the last one accepted (`totp_last_step`), claimed with a conditional UPDATE.
    A recovery code is burned by stamping `used_at` under `WHERE used_at IS NULL`. Callers
    own the failure handling (lockout counting, audit), since it differs by endpoint.
    """
    if not user.totp_enabled or not user.totp_secret_enc:
        return False
    if _is_totp_code(code):
        step = totp.verify(totp.unseal(user.totp_secret_enc), code, last_step=user.totp_last_step)
        if step is None:
            return False
        result = await session.execute(
            update(User).where(User.id == user.id,
                               or_(User.totp_last_step.is_(None), User.totp_last_step < step))
            .values(totp_last_step=step))
        await session.commit()
        return result.rowcount == 1
    result = await session.execute(
        update(RecoveryCode)
        .where(RecoveryCode.user_id == user.id,
               RecoveryCode.code_hash == totp.hash_recovery_code(code),
               RecoveryCode.used_at.is_(None))
        .values(used_at=datetime.now(timezone.utc)))
    await session.commit()
    if result.rowcount == 1:
        audit("auth.2fa.recovery_code_used", user_id=user.id, email=user.email)
        return True
    return False


async def disable(session: AsyncSession, user: User, password: str, code: str) -> None:
    """Turn 2FA off. Needs the password *and* a valid code, so a stolen access token alone
    can't strip the second factor.

    Raises:
        APIError(400, TOTP_NOT_ENABLED)
        APIError(400, INVALID_CURRENT_PASSWORD)
        APIError(400, INVALID_CODE)
    """
    if not user.totp_enabled:
        raise APIError(400, "TOTP_NOT_ENABLED", "Two-factor authentication isn't on.")
    if not verify_password(password, user.password_hash):
        raise APIError(400, "INVALID_CURRENT_PASSWORD", "Current password is incorrect.")
    if not await verify_second_factor(session, user, code):
        raise APIError(400, "INVALID_CODE", "That code isn't right.")
    await clear(session, user.id)
    audit("auth.2fa.disabled", user_id=user.id, email=user.email)


async def clear(session: AsyncSession, user_id) -> None:
    """Remove all 2FA state for a user (shared by `disable` and the ops CLI)."""
    await session.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
    await session.execute(update(User).where(User.id == user_id).values(
        totp_enabled=False, totp_secret_enc=None, totp_last_step=None))
    await session.commit()


async def regenerate_recovery_codes(session: AsyncSession, user: User, code: str) -> list[str]:
    """Replace the user's recovery codes (proving possession of the authenticator first).

    Raises:
        APIError(400, TOTP_NOT_ENABLED), APIError(400, INVALID_CODE)
    """
    if not user.totp_enabled:
        raise APIError(400, "TOTP_NOT_ENABLED", "Two-factor authentication isn't on.")
    if not await verify_second_factor(session, user, code):
        raise APIError(400, "INVALID_CODE", "That code isn't right.")
    codes = await _replace_recovery_codes(session, user)
    await session.commit()
    audit("auth.2fa.recovery_codes_regenerated", user_id=user.id, email=user.email)
    return codes
