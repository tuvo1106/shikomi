"""The cryptographic core of authentication: password hashing, JWT access
tokens, opaque-token helpers, and the FastAPI dependencies that turn a request
into an authenticated `User`.

Why these pieces exist (the mental model):

* **Passwords** are never stored — only a slow, salted *bcrypt* hash. Slowness
  is the point: it caps how fast an attacker who steals the DB can guess.
* **Access tokens** are short-lived JWTs. A JWT is *stateless* — the server
  verifies it with a secret and trusts its claims without a DB lookup, so it
  can't be individually revoked; we keep its lifetime short (minutes) to bound
  the damage of a leak. Long-lived sessions live in the *refresh* token instead
  (see `services/auth_service.py`), which IS stored and revocable.
  Signing keys rotate without a mass logout: each token names its key by `kid`
  and we verify against the current key plus any retired-but-not-yet-expired
  ones (`_verification_keys`).
* **Opaque tokens** (refresh + email tokens) are random strings we store only as
  a SHA-256 hash, so a database leak yields nothing usable.

The `current_user` / `require_*` functions are FastAPI *dependencies*: declaring
one as a parameter makes FastAPI run it first and inject its result, and any
`APIError` it raises short-circuits the request. That's how a route says "this
needs a logged-in / verified user" declaratively. (DESIGN.md §3.6)
"""
import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.errors import APIError
from app.models import User

settings = get_settings()
# bcrypt only hashes the first 72 bytes and errors on longer input; slice so a
# very long password is accepted (truncated) rather than blowing up.
_BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    """Return a bcrypt hash suitable for storing in `users.password_hash`.

    `bcrypt.gensalt()` generates a fresh random salt and embeds it (plus the cost
    factor) into the returned string, so every hash is unique even for identical
    passwords and `verify_password` needs no separate salt column.
    """
    pw = password.encode()[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    """Check a plaintext password against a stored bcrypt hash.

    bcrypt reads the salt/cost back out of `password_hash`, re-hashes the input,
    and compares in constant time — so this is safe against timing attacks and
    needs nothing but the stored hash.
    """
    pw = password.encode()[:_BCRYPT_MAX_BYTES]
    return bcrypt.checkpw(pw, password_hash.encode())


def _kid(secret: str) -> str:
    """A short, stable fingerprint naming `secret` in a JWT's `kid` header.

    Derived from the key itself so there is no separate key-id config to keep in
    step with it: rotating is just moving the old secret into
    `JWT_PREVIOUS_SECRETS`. The `kid` is public (it rides in every token's header),
    so it's a truncated one-way hash — safe for the high-entropy secret prod
    requires, and only a lookup label, never trusted for the signature check.
    """
    return hashlib.sha256(b"jwt-kid:" + secret.encode()).hexdigest()[:8]


def _verification_keys() -> dict[str, str]:
    """`kid -> secret` for every key a token may legitimately be signed with:
    the current one first, then retired ones still inside their overlap window.
    """
    secrets_ = [settings.jwt_secret]
    secrets_ += [k.strip() for k in settings.jwt_previous_secrets.split(",") if k.strip()]
    return {_kid(k): k for k in secrets_}


def create_access_token(user: User) -> str:
    """Mint a signed, short-lived JWT identifying `user`.

    Claims: `sub` (the user id, the JWT "subject"), `typ` (so a refresh-shaped
    token can't pose as an access token), `iat`/`exp` (issued-at / expiry as UNIX seconds).
    Signed with HS256 (a shared-secret HMAC) using the *current* `settings.jwt_secret`
    and tagged with its `kid`; anyone with a secret can mint tokens, so it must
    stay server-side and rotate on leak. The token is *not* encrypted — its
    contents are readable, just not forgeable — so never put secrets in the payload.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "typ": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.jwt_access_ttl_seconds)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256",
                      headers={"kid": _kid(settings.jwt_secret)})


def password_fingerprint(user: User) -> str:
    """A short, non-reversible tag of the user's current password hash.

    Stamped into the MFA challenge so a password change or reset invalidates any challenge
    minted with the old password (the challenge is otherwise reusable until it expires).
    """
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:16]


def create_mfa_token(user: User) -> str:
    """A short-lived token proving "this user just supplied the right password".

    Issued instead of a session when the account has two-factor auth on, and redeemed
    (with a valid code) at `/auth/login/2fa`. It is signed like an access token but marked
    `typ: "mfa"`, and `decode_access_token` refuses anything that isn't an access token —
    so it can never be used as a bearer token: the password alone must not open the API.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "typ": "mfa",
        "pv": password_fingerprint(user),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=settings.mfa_token_ttl_seconds)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256",
                      headers={"kid": _kid(settings.jwt_secret)})


def _decode(token: str) -> dict:
    """Verify a JWT's signature + expiry and return its claims (any token type).

    The `kid` header only *selects* which key to check; the signature is what is
    trusted, so a forged or swapped `kid` fails verification. An unknown `kid` means the
    key was retired (or never ours), so it's rejected outright. A token with no `kid`
    predates key rotation; it's tried against each key rather than forcing a logout at
    deploy time.

    Raises `jwt.PyJWTError` (or a subclass like `ExpiredSignatureError`) if the key is
    unknown, the signature is wrong, or the token has expired.
    """
    keys = _verification_keys()
    kid = jwt.get_unverified_header(token).get("kid")
    if kid is None:
        candidates = list(keys.values())
    elif kid in keys:
        candidates = [keys[kid]]
    else:
        raise jwt.InvalidKeyError("unknown or retired signing key")
    for i, key in enumerate(candidates):
        try:
            return jwt.decode(token, key, algorithms=["HS256"])
        except jwt.InvalidSignatureError:
            if i == len(candidates) - 1:
                raise
    raise jwt.InvalidSignatureError("no key matched")  # unreachable; keeps types honest


def decode_access_token(token: str) -> dict:
    """Verify a bearer token and return its claims; callers convert failures into a 401.

    Only *access* tokens pass: `typ` is `"access"`, or absent for tokens minted before
    token types existed (they expire within minutes). A `typ: "mfa"` token — the
    password-only login step — is rejected, so it can never authenticate a request.
    """
    payload = _decode(token)
    if payload.get("typ") not in (None, "access"):
        raise jwt.InvalidTokenError("not an access token")
    return payload


def decode_mfa_token(token: str) -> dict:
    """Verify a login-challenge token (`create_mfa_token`) and return its claims.

    Raises `jwt.PyJWTError` if it's invalid, expired, or is an access token instead.
    """
    payload = _decode(token)
    if payload.get("typ") != "mfa":
        raise jwt.InvalidTokenError("not an mfa token")
    return payload


def generate_token() -> str:
    """A fresh, unguessable URL-safe token (used for refresh + email tokens).

    `secrets.token_urlsafe` draws from a cryptographically-secure RNG — unlike
    `random`, which is predictable and must never be used for security tokens.
    48 bytes ≈ 384 bits of entropy, far beyond brute-force range.
    """
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    """Hash an opaque token for storage (SHA-256, hex).

    Unlike passwords we use a *fast* hash here on purpose: these tokens are
    already high-entropy random strings, so slow hashing buys nothing, and we
    look them up on every refresh. Storing only the hash means a DB leak exposes
    no usable tokens, while an incoming token can still be matched by re-hashing
    it and comparing (see `auth_service.rotate_refresh_token`). (DESIGN.md §3.6)
    """
    return hashlib.sha256(token.encode()).hexdigest()


# Back-compat aliases: refresh-token call sites predate the generic names above.
generate_refresh_token = generate_token
hash_refresh_token = hash_token


# `auto_error=False`: return None instead of raising when the Authorization
# header is missing, so `current_user` can craft its own APIError shape.
_bearer = HTTPBearer(auto_error=False)


async def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    """FastAPI dependency: resolve the bearer token into the current `User`.

    Extracts the `Authorization: Bearer <jwt>` header, verifies the JWT, then
    loads the user by its `sub` claim. We still hit the DB (rather than trusting
    the token blindly) so a deleted/renamed account can't keep acting on a
    stale-but-unexpired token.

    Raises:
        APIError(401): no credentials, an invalid/expired token, a `sub` claim
            that isn't a well-formed UUID, or the user no longer exists.
            Attach this dependency to any route that requires authentication.
    """
    if credentials is None:
        raise APIError(401, "NOT_AUTHENTICATED", "Authentication required.")
    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.PyJWTError:
        raise APIError(401, "INVALID_TOKEN", "Invalid or expired token.")
    try:
        # `create_access_token` always sets `sub` to a real user id, so this
        # only matters for a token this process didn't mint itself — a leaked
        # jwt_secret used to forge one, or a payload shape from some other
        # signing path. Malformed input to a dependency should fail as 401
        # (an auth problem), never bubble up as an unhandled 500.
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError, AttributeError, TypeError):
        raise APIError(401, "INVALID_TOKEN", "Invalid or expired token.")
    user = await session.get(User, user_id)
    if user is None:
        raise APIError(401, "INVALID_TOKEN", "Invalid or expired token.")
    return user


async def require_verified(user: User = Depends(current_user)) -> User:
    """Dependency gating resource-consuming actions (judging) behind a verified
    email, so unverified/dummy accounts can't spend judge capacity. There is
    no exemption: an operator verifies an account out of band with
    `app.cli verify-email` (or `seed-dev-user`, which creates it verified).

    Raises:
        APIError(403, EMAIL_NOT_VERIFIED): the account hasn't verified its email.
    """
    if not user.email_verified:
        raise APIError(403, "EMAIL_NOT_VERIFIED", "Verify your email to continue.")
    return user
