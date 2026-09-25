"""Auth HTTP endpoints and all cookie handling (DESIGN.md §3.6, §4.1).

This router owns the *transport* half of auth — reading/writing the refresh
cookie, translating service results into responses — while `auth_service` /
`account_service` own the rules. The cookie split is the crux of the security
model: the **access token** is returned in the JSON body (the SPA holds it in
memory), while the **refresh token** rides in an httpOnly cookie the browser
stores but JavaScript can't read, so an XSS bug can't exfiltrate it.

Auth endpoints depend on `rate_limit_auth` to blunt brute force, and login/reset
responses are intentionally uniform so they never reveal whether an email exists.
"""
import hmac
import uuid

import jwt
from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.config import get_settings
from app.db import get_session
from app.errors import APIError
from app.queue import Queue, get_queue
from app.rate_limit import _client_ip, rate_limit_auth
from app.schemas.auth import (
    Login2FARequest,
    MfaChallenge,
    RecoveryCodesResponse,
    TotpCodeRequest,
    TotpDisableRequest,
    TotpSetupResponse,
    AccessTokenResponse,
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    MessageResponse,
    PasswordResetConfirm,
    PasswordResetRequest,
    RegisterRequest,
    ResendVerificationRequest,
    UserOut,
    VerifyEmailRequest,
)
from app.security import (
    create_access_token,
    create_mfa_token,
    current_user,
    decode_mfa_token,
    hash_token,
    password_fingerprint,
)
from app.services import account_service, auth_service, totp_service
from app.models import User

settings = get_settings()
router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "refresh_token"
COOKIE_PATH = "/api/v1/auth"
# Readable (non-httpOnly) hint mirroring the refresh cookie's lifetime, so the
# SPA can skip the /auth/refresh probe (and its guaranteed 401) when logged out.
SESSION_HINT_COOKIE = "has_session"


def _set_refresh_cookie(response: Response, token: str, persistent: bool = True) -> None:
    """Set the httpOnly refresh cookie plus the readable `has_session` hint.

    Flags that matter: `httponly` (JS can't read it — XSS mitigation), `secure` in
    prod only (so it works over http on localhost), `samesite="lax"` (sent on
    top-level navigations but not cross-site POSTs — CSRF mitigation), and a narrow
    `path` so it's only attached to `/auth/*` requests. `SameSite=Lax` is what
    keeps `/auth/refresh` — the only endpoint that authenticates by cookie — from
    being CSRF'd; every other endpoint reads the `Authorization` header, which a
    cross-site page can't attach. That holds only while access tokens stay out of
    cookies (DESIGN.md §3.6, "CSRF"). `persistent` decides
    session-cookie vs max-age (survives browser close). The non-httpOnly hint lets
    the SPA skip the refresh probe when logged out (see client.ts).
    """
    max_age = settings.jwt_refresh_ttl_seconds if persistent else None
    secure = settings.env == "prod"
    response.set_cookie(
        REFRESH_COOKIE, token, max_age=max_age,
        httponly=True, secure=secure, samesite="lax", path=COOKIE_PATH,
    )
    response.set_cookie(
        SESSION_HINT_COOKIE, "1", max_age=max_age,
        httponly=False, secure=secure, samesite="lax", path="/",
    )


def _clear_session_cookies(response: Response) -> None:
    """Expire both auth cookies (logout). Paths must match how they were set."""
    response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH)
    response.delete_cookie(SESSION_HINT_COOKIE, path="/")


@router.post("/register", status_code=202, response_model=MessageResponse)
async def register(data: RegisterRequest, request: Request, _: None = Depends(rate_limit_auth),
                   queue: Queue = Depends(get_queue),
                   session: AsyncSession = Depends(get_session)):
    """POST /auth/register — start a signup; the next step is always the inbox.

    The same 202 whether the email is new (account created, verify link sent) or
    already registered (nothing created, the owner is emailed instead) — see
    `auth_service.register` for why the difference only shows in the inbox. 409
    only for a taken *username*; 422 for a rejected password.
    """
    outcome, user = await auth_service.register(session, data, queue)
    # The audit trail is internal, so it records which case this was.
    event = "auth.register" if outcome == "created" else "auth.register.existing_email"
    audit(event, user_id=user.id, email=user.email, ip=_client_ip(request))
    return MessageResponse(
        message="Check your email to finish signing up — we've sent you a link.")


@router.post("/login", response_model=LoginResponse | MfaChallenge)
async def login(data: LoginRequest, request: Request, response: Response,
                _: None = Depends(rate_limit_auth),
                queue: Queue = Depends(get_queue),
                session: AsyncSession = Depends(get_session)):
    """POST /auth/login — verify credentials, set the refresh cookie, return a JWT.

    If the account has two-factor auth on, a correct password isn't enough: it returns
    an `MfaChallenge` (no session, no cookie) and the client finishes at `/auth/login/2fa`.
    The prompt only appears *after* the password checks out, so it can't be used to probe
    which emails are registered. Failure counts deliberately survive a correct password
    here (see below), or an attacker who knows the password could reset the counter
    between guesses at the code.

    Two brute-force defenses wrap the credential check: the per-IP cap (the
    `rate_limit_auth` dependency) and a per-account lockout. Failures are counted
    against the *email string* whether or not the account exists — so lockout can't
    be used to probe which emails are registered (anti-enumeration).
    """
    ip = _client_ip(request)
    # Refuse early if this account is currently locked out.
    locked = await queue.get_login_lock(data.email)
    if locked:
        audit("auth.login.blocked", email=data.email, ip=ip)
        raise APIError(429, "ACCOUNT_LOCKED", "Too many failed attempts. Try again later.",
                       headers={"Retry-After": str(locked)})

    user = await auth_service.authenticate(session, data.email, data.password)
    if user is None:
        # Count the failure and lock the account once it crosses the threshold.
        count = await queue.incr_login_failures(data.email, settings.login_failure_window_seconds)
        audit("auth.login.failure", email=data.email, ip=ip)
        if count >= settings.login_max_failures:
            await queue.set_login_lock(data.email, settings.login_lockout_seconds)
            audit("auth.login.locked", email=data.email, ip=ip)
        # One message for an unknown email, a wrong password, and an unverified
        # account alike (§4.1) — so it has to make sense for all three.
        raise APIError(401, "INVALID_CREDENTIALS",
                       "Incorrect email or password. Just signed up? "
                       "Confirm your email first.")

    if user.totp_enabled:
        # Password is right, second factor still owed: no session yet. Do NOT clear the
        # failure counter — wrong codes count against it, and clearing it on every correct
        # password would let someone who knows the password guess codes without limit.
        audit("auth.login.mfa_required", user_id=user.id, email=user.email, ip=ip)
        return MfaChallenge(mfa_token=create_mfa_token(user))

    await queue.clear_login_failures(data.email)  # good login resets the counter
    token = await auth_service.issue_refresh_token(session, user, persistent=data.remember)
    _set_refresh_cookie(response, token, persistent=data.remember)
    audit("auth.login.success", user_id=user.id, email=user.email, ip=ip)
    return LoginResponse(access_token=create_access_token(user), user=UserOut.model_validate(user))


@router.post("/login/2fa", response_model=LoginResponse)
async def login_2fa(data: Login2FARequest, request: Request, response: Response,
                    _: None = Depends(rate_limit_auth),
                    queue: Queue = Depends(get_queue),
                    session: AsyncSession = Depends(get_session)):
    """POST /auth/login/2fa — finish a two-factor login: redeem the challenge with a code.

    `code` is a 6-digit authenticator code or a one-time recovery code. Wrong codes count
    against the same per-account lockout as wrong passwords, and a valid TOTP code works
    once (its time step is recorded), so a shoulder-surfed or intercepted code is useless.
    401 INVALID_MFA_TOKEN if the challenge is expired/forged — start the login over.
    """
    ip = _client_ip(request)
    invalid = APIError(401, "INVALID_MFA_TOKEN", "Your sign-in expired. Please start again.")
    try:
        claims = decode_mfa_token(data.mfa_token)
        user = await session.get(User, uuid.UUID(claims["sub"]))
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        raise invalid
    if user is None or not user.totp_enabled:
        raise invalid
    if not hmac.compare_digest(str(claims.get("pv", "")), password_fingerprint(user)):
        raise invalid  # the password changed since this challenge was issued

    locked = await queue.get_login_lock(user.email)
    if locked:
        audit("auth.login.blocked", email=user.email, ip=ip)
        raise APIError(429, "ACCOUNT_LOCKED", "Too many failed attempts. Try again later.",
                       headers={"Retry-After": str(locked)})

    if not await totp_service.verify_second_factor(session, user, data.code):
        count = await queue.incr_login_failures(user.email, settings.login_failure_window_seconds)
        audit("auth.2fa.failure", user_id=user.id, email=user.email, ip=ip)
        if count >= settings.login_max_failures:
            await queue.set_login_lock(user.email, settings.login_lockout_seconds)
            audit("auth.login.locked", email=user.email, ip=ip)
        raise APIError(401, "INVALID_CODE", "That code isn't right.")

    await queue.clear_login_failures(user.email)
    token = await auth_service.issue_refresh_token(session, user, persistent=data.remember)
    _set_refresh_cookie(response, token, persistent=data.remember)
    audit("auth.login.success", user_id=user.id, email=user.email, ip=ip, mfa=True)
    return LoginResponse(access_token=create_access_token(user), user=UserOut.model_validate(user))


@router.post("/refresh", response_model=AccessTokenResponse)
async def refresh(request: Request, response: Response,
                  session: AsyncSession = Depends(get_session)):
    """POST /auth/refresh — rotate the refresh cookie and mint a new access token.

    The SPA calls this on startup (to restore a session) and after an access token
    expires. Rotation swaps the cookie for a fresh one each time. 401 if there's no
    cookie or the token is invalid/expired/reused.
    """
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise APIError(401, "NO_REFRESH", "No refresh token.")
    user, new_token, persistent = await auth_service.rotate_refresh_token(session, token)
    _set_refresh_cookie(response, new_token, persistent=persistent)
    return AccessTokenResponse(access_token=create_access_token(user))


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response,
                 session: AsyncSession = Depends(get_session)):
    """POST /auth/logout — revoke the current session and clear the cookies (204)."""
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        await auth_service.revoke_refresh_token(session, token)
    _clear_session_cookies(response)


@router.get("/me", response_model=UserOut)
async def me(user=Depends(current_user)):
    """GET /auth/me — the authenticated user; how the SPA rehydrates on load."""
    return UserOut.model_validate(user)


@router.post("/verify-email", response_model=UserOut)
async def verify_email(data: VerifyEmailRequest, queue: Queue = Depends(get_queue),
                       session: AsyncSession = Depends(get_session)):
    """POST /auth/verify-email — consume a verification token; returns the user.

    Also clears the address's failed-login count and any lock. An unverified
    account's sign-in attempts fail — and must still count toward the lockout,
    or "never locks with the right password" would distinguish a new signup
    from a taken email — so a new user who tries to sign in before clicking the
    link can lock themselves out. Following the link proves they own the inbox,
    which an attacker racking up failures can't do.
    """
    user = await account_service.verify_email(session, data.token)
    await queue.clear_login_failures(user.email)
    audit("auth.email.verified", user_id=user.id, email=user.email)
    return UserOut.model_validate(user)


@router.post("/resend-verification", status_code=202, response_model=MessageResponse)
async def resend_verification(data: ResendVerificationRequest,
                              _: None = Depends(rate_limit_auth),
                              queue: Queue = Depends(get_queue)):
    """POST /auth/resend-verification — re-send the verify link for an email.

    Unauthenticated, because unverified accounts can't log in. Always the same
    202 (anti-enumeration), like the password-reset request.
    """
    await account_service.resend_verification_email(queue, data.email)
    return MessageResponse(
        message="If that email has an unverified account, a new link is on its way.")


@router.post("/password-reset/request", status_code=202, response_model=MessageResponse)
async def request_password_reset(data: PasswordResetRequest, request: Request,
                                 _: None = Depends(rate_limit_auth),
                                 queue: Queue = Depends(get_queue)):
    """POST /auth/password-reset/request — email a reset link if the account exists.

    Always returns the same 202 message (anti-enumeration): the response never
    reveals whether the address is registered.
    """
    await account_service.request_password_reset(queue, data.email)
    # Logged whether or not the address exists — the audit trail is internal, and
    # the response stays uniform (so this doesn't undo anti-enumeration).
    audit("auth.password_reset.requested", email=data.email, ip=_client_ip(request))
    return MessageResponse(message="If that email is registered, a reset link is on its way.")


@router.post("/password-reset/confirm", response_model=MessageResponse)
async def confirm_password_reset(data: PasswordResetConfirm, request: Request,
                                 _: None = Depends(rate_limit_auth),
                                 session: AsyncSession = Depends(get_session)):
    """POST /auth/password-reset/confirm — set a new password via a reset token."""
    user = await account_service.reset_password(session, data.token, data.password)
    audit("auth.password_reset.completed", user_id=user.id, ip=_client_ip(request))
    return MessageResponse(message="Your password has been reset. You can now sign in.")


@router.post("/change-password", response_model=MessageResponse)
async def change_password(data: ChangePasswordRequest, request: Request,
                          _: None = Depends(rate_limit_auth),
                          user=Depends(current_user),
                          session: AsyncSession = Depends(get_session)):
    """POST /auth/change-password — signed-in password change (Settings page).

    Preserves the caller's current session (via the refresh cookie) while revoking
    every other session, so changing the password doesn't log the user out here.
    """
    current = request.cookies.get(REFRESH_COOKIE)
    await account_service.change_password(
        session, user, data.current_password, data.new_password,
        keep_token_hash=hash_token(current) if current else None)
    audit("auth.password.changed", user_id=user.id, ip=_client_ip(request))
    return MessageResponse(message="Your password has been updated.")


# --- two-factor auth management (signed-in users) ---------------------------------------

@router.post("/2fa/setup", response_model=TotpSetupResponse)
async def totp_setup(_: None = Depends(rate_limit_auth), user=Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    """POST /auth/2fa/setup — mint a pending secret; the client shows it as a QR code.

    Doesn't turn 2FA on: that happens at `/2fa/enable` once a code proves it works.
    409 TOTP_ALREADY_ENABLED if it's already on.
    """
    secret, uri = await totp_service.begin_setup(session, user)
    return TotpSetupResponse(secret=secret, otpauth_uri=uri)


@router.post("/2fa/enable", response_model=RecoveryCodesResponse)
async def totp_enable(data: TotpCodeRequest, _: None = Depends(rate_limit_auth),
                      user=Depends(current_user), session: AsyncSession = Depends(get_session)):
    """POST /auth/2fa/enable — verify a first code, turn 2FA on, return the recovery codes.

    The recovery codes are shown exactly once (only their hashes are stored).
    """
    return RecoveryCodesResponse(
        recovery_codes=await totp_service.enable(session, user, data.code))


async def _guard_guessing(request: Request, queue: Queue, user, action):
    """Run a code/password-gated 2FA action under the same per-account lockout as login.

    `/2fa/disable` and `/2fa/recovery-codes` check a 6-digit code (and, for disable, the
    password) for someone holding only an access token. With just the per-IP limit, that's
    a guessing oracle a rotating attacker can grind; counting wrong answers against the
    account's failure counter (shared with `/login` and `/login/2fa`) and refusing while
    locked makes the "a stolen session alone can't strip 2FA" claim hold up.
    """
    ip = _client_ip(request)
    locked = await queue.get_login_lock(user.email)
    if locked:
        audit("auth.login.blocked", email=user.email, ip=ip)
        raise APIError(429, "ACCOUNT_LOCKED", "Too many failed attempts. Try again later.",
                       headers={"Retry-After": str(locked)})
    try:
        return await action()
    except APIError as e:
        if e.code in ("INVALID_CODE", "INVALID_CURRENT_PASSWORD"):
            count = await queue.incr_login_failures(user.email, settings.login_failure_window_seconds)
            audit("auth.2fa.failure", user_id=user.id, email=user.email, ip=ip)
            if count >= settings.login_max_failures:
                await queue.set_login_lock(user.email, settings.login_lockout_seconds)
                audit("auth.login.locked", email=user.email, ip=ip)
        raise


@router.post("/2fa/disable", response_model=MessageResponse)
async def totp_disable(data: TotpDisableRequest, request: Request,
                       _: None = Depends(rate_limit_auth), queue: Queue = Depends(get_queue),
                       user=Depends(current_user), session: AsyncSession = Depends(get_session)):
    """POST /auth/2fa/disable — turn 2FA off; needs the password and a valid code."""
    await _guard_guessing(request, queue, user,
                          lambda: totp_service.disable(session, user, data.password, data.code))
    return MessageResponse(message="Two-factor authentication is off.")


@router.post("/2fa/recovery-codes", response_model=RecoveryCodesResponse)
async def totp_recovery_codes(data: TotpCodeRequest, request: Request,
                              _: None = Depends(rate_limit_auth), queue: Queue = Depends(get_queue),
                              user=Depends(current_user),
                              session: AsyncSession = Depends(get_session)):
    """POST /auth/2fa/recovery-codes — replace the recovery codes (old ones stop working)."""
    codes = await _guard_guessing(
        request, queue, user,
        lambda: totp_service.regenerate_recovery_codes(session, user, data.code))
    return RecoveryCodesResponse(recovery_codes=codes)
