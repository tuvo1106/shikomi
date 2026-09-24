"""Auth flow tests (DESIGN.md §4.1, §3.6, §10.2)."""
import re

import jwt
import pytest
from sqlalchemy import text
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.routers import auth as auth_router

_settings = get_settings()

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/auth/me"

CREDS = {"email": "alice@example.com", "username": "alice", "password": "sunflower-desk-42"}


async def _register_and_login(client, signup):
    await signup(CREDS)
    r = await client.post(LOGIN, json={"email": CREDS["email"], "password": CREDS["password"]})
    return r


async def test_register_then_verify_then_me(client, signup):
    await signup(CREDS)
    login = await client.post(LOGIN, json={"email": CREDS["email"], "password": CREDS["password"]})
    token = login.json()["access_token"]
    me = await client.get(ME, headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["username"] == "alice"


async def test_register_response_is_identical_for_a_taken_email(client, outbox):
    """The anti-enumeration contract: a taken email gets byte-for-byte the same
    response as a new one. Only the inbox differs."""
    new = await client.post(REGISTER, json=CREDS)
    taken = await client.post(REGISTER, json={**CREDS, "username": "someone_else"})
    assert new.status_code == taken.status_code == 202
    assert new.json() == taken.json()

    assert [m.to for m in outbox] == [CREDS["email"], CREDS["email"]]
    assert "verify-email?token=" in outbox[0].body
    assert "already have one" in outbox[1].body
    assert "reset-password?token=" in outbox[1].body


async def test_register_with_a_taken_email_changes_nothing(client, signup):
    await signup(CREDS)
    await client.post(REGISTER, json={**CREDS, "username": "intruder", "password": "attacker-pw-99"})
    # The original password still works and the attacker's doesn't.
    ok = await client.post(LOGIN, json={"email": CREDS["email"], "password": CREDS["password"]})
    bad = await client.post(LOGIN, json={"email": CREDS["email"], "password": "attacker-pw-99"})
    assert ok.status_code == 200
    assert bad.status_code == 401


async def test_register_taken_username_conflicts_regardless_of_email(client):
    """A taken username is a 409 whether the email is new *or* taken — if a
    taken email skipped this check, pairing a known username with a guessed
    email would become an email oracle again."""
    await client.post(REGISTER, json=CREDS)
    with_new_email = await client.post(REGISTER, json={**CREDS, "email": "bob@example.com"})
    with_taken_email = await client.post(REGISTER, json=CREDS)
    assert with_new_email.status_code == with_taken_email.status_code == 409
    assert with_new_email.json() == with_taken_email.json()
    assert with_new_email.json()["code"] == "USERNAME_TAKEN"


async def test_unverified_account_cannot_log_in(client):
    """Closes the register→login oracle: with a new email, the password you just
    chose would work; with a taken one it wouldn't. So until verification, the
    right password gets exactly the wrong-password response."""
    await client.post(REGISTER, json=CREDS)
    unverified = await client.post(LOGIN, json={"email": CREDS["email"], "password": CREDS["password"]})
    wrong_pw = await client.post(LOGIN, json={"email": CREDS["email"], "password": "nope-nope-1"})
    assert unverified.status_code == wrong_pw.status_code == 401
    assert unverified.json() == wrong_pw.json()


async def test_unknown_email_login_still_runs_bcrypt(client, monkeypatch):
    """Otherwise an unknown email answers measurably faster than a wrong password."""
    from app.services import auth_service
    calls = []
    real = auth_service.verify_password
    monkeypatch.setattr(auth_service, "verify_password",
                        lambda pw, h: calls.append(h) or real(pw, h))
    r = await client.post(LOGIN, json={"email": "ghost@example.com", "password": "nope-nope-1"})
    assert r.status_code == 401
    assert calls == [auth_service._DUMMY_HASH]


async def test_register_rejects_short_password(client):
    r = await client.post(REGISTER, json={**CREDS, "password": "short"})
    assert r.status_code == 422


async def test_login_failures_are_uniform(client, signup):
    await signup(CREDS)
    wrong_pw = await client.post(LOGIN, json={"email": CREDS["email"], "password": "nope!"})
    no_user = await client.post(LOGIN, json={"email": "ghost@example.com", "password": "nope!"})
    assert wrong_pw.status_code == no_user.status_code == 401
    assert wrong_pw.json()["detail"] == no_user.json()["detail"]  # no user enumeration


async def test_me_requires_auth(client):
    assert (await client.get(ME)).status_code == 401
    bad = await client.get(ME, headers={"Authorization": "Bearer garbage"})
    assert bad.status_code == 401


async def test_me_rejects_a_validly_signed_token_with_a_non_uuid_sub(client):
    """A validly-signed token whose `sub` isn't a UUID (only reachable if the
    jwt_secret leaked, or some other bug minted one) must be a clean 401, not
    an unhandled ValueError surfacing as a 500. Unlike the "garbage" case above (fails at JWT decode, a different
    code path entirely), this token decodes fine — it fails specifically at
    the UUID parse."""
    token = jwt.encode({"sub": "not-a-uuid", "typ": "access"}, _settings.jwt_secret,
                       algorithm="HS256")
    r = await client.get(ME, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert r.json()["code"] == "INVALID_TOKEN"


async def test_refresh_rotates_token(client, signup):
    await _register_and_login(client, signup)
    old = client.cookies.get("refresh_token")
    r = await client.post(REFRESH)
    assert r.status_code == 200
    assert "access_token" in r.json()
    assert client.cookies.get("refresh_token") != old


async def test_refresh_reuse_revokes_all_sessions(client, signup, session_factory):
    await _register_and_login(client, signup)
    old = client.cookies.get("refresh_token")
    await client.post(REFRESH)  # rotate: old is now revoked
    new = client.cookies.get("refresh_token")
    # Past the grace window (a replay *inside* it is a concurrent request, not theft:
    # see test_auth_service.py), so age the rotation.
    async with session_factory() as s:
        await s.execute(text("update refresh_tokens set rotated_at = now() - interval '1 hour' "
                             "where revoked_at is not null"))
        await s.commit()

    client.cookies.clear()
    client.cookies.set("refresh_token", old)
    reuse = await client.post(REFRESH)
    assert reuse.status_code == 401
    assert reuse.json()["code"] == "REFRESH_REUSE"

    # The reuse must have revoked the still-live token too.
    client.cookies.clear()
    client.cookies.set("refresh_token", new)
    assert (await client.post(REFRESH)).status_code == 401


async def test_refresh_without_cookie(client):
    r = await client.post(REFRESH)
    assert r.status_code == 401
    assert r.json()["code"] == "NO_REFRESH"


async def test_logout_invalidates_refresh(client, signup):
    await _register_and_login(client, signup)
    assert (await client.post(LOGOUT)).status_code == 204
    assert (await client.post(REFRESH)).status_code == 401


async def test_remember_true_sets_persistent_cookie(client, signup):
    await signup(CREDS)
    r = await client.post(LOGIN, json={"email": CREDS["email"], "password": CREDS["password"],
                                       "remember": True})
    assert "max-age" in r.headers.get("set-cookie", "").lower()


async def test_remember_false_sets_session_cookie(client, signup):
    await signup(CREDS)
    r = await client.post(LOGIN, json={"email": CREDS["email"], "password": CREDS["password"],
                                       "remember": False})
    cookie = r.headers.get("set-cookie", "").lower()
    assert "refresh_token=" in cookie
    assert "max-age" not in cookie  # session cookie — cleared on browser close


async def test_session_cookie_survives_refresh_rotation(client, signup):
    await signup(CREDS)
    await client.post(LOGIN, json={"email": CREDS["email"], "password": CREDS["password"],
                                   "remember": False})
    r = await client.post(REFRESH)
    assert r.status_code == 200
    assert "max-age" not in r.headers.get("set-cookie", "").lower()  # still session-only


async def test_login_is_rate_limited(client, signup):
    await signup(CREDS)
    codes = []
    for _ in range(11):
        r = await client.post(LOGIN, json={"email": CREDS["email"], "password": CREDS["password"]})
        codes.append(r.status_code)
    assert codes[-1] == 429
    assert 200 in codes


async def test_account_locks_after_repeated_failures(client, signup):
    await signup(CREDS)
    for _ in range(_settings.login_max_failures):
        bad = await client.post(LOGIN, json={"email": CREDS["email"], "password": "wrong"})
        assert bad.status_code == 401

    locked = await client.post(LOGIN, json={"email": CREDS["email"], "password": "wrong"})
    assert locked.status_code == 429
    assert locked.json()["code"] == "ACCOUNT_LOCKED"

    # The lock stands even against the *correct* password until it expires.
    correct = await client.post(LOGIN, json={"email": CREDS["email"], "password": CREDS["password"]})
    assert correct.status_code == 429
    assert correct.json()["code"] == "ACCOUNT_LOCKED"


async def test_lockout_does_not_reveal_account_existence(client):
    # Failing against an unregistered email locks that email too, so an attacker
    # can't tell registered from not from the lockout behavior (anti-enumeration).
    ghost = {"email": "ghost@example.com", "password": "wrong"}
    for _ in range(_settings.login_max_failures):
        assert (await client.post(LOGIN, json=ghost)).status_code == 401
    assert (await client.post(LOGIN, json=ghost)).json()["code"] == "ACCOUNT_LOCKED"


async def test_successful_login_resets_failure_counter(client, signup):
    await signup(CREDS)
    # A few failures, then a success, must clear the counter so it can't accumulate
    # across sessions toward a lock.
    for _ in range(_settings.login_max_failures - 1):
        await client.post(LOGIN, json={"email": CREDS["email"], "password": "wrong"})
    ok = await client.post(LOGIN, json={"email": CREDS["email"], "password": CREDS["password"]})
    assert ok.status_code == 200
    # One more failure should not lock (counter was reset by the success).
    again = await client.post(LOGIN, json={"email": CREDS["email"], "password": "wrong"})
    assert again.status_code == 401


def _miss_first_check(monkeypatch, name):
    """Make `auth_service.<name>`'s first call report "not taken", as if a
    concurrent signup committed between the pre-check and our insert."""
    from app.services import auth_service
    real = getattr(auth_service, name)
    calls = []

    async def racy(session, value):
        calls.append(value)
        return (False if name == "_username_taken" else None) if len(calls) == 1 \
            else await real(session, value)
    monkeypatch.setattr(auth_service, name, racy)


async def test_email_race_is_answered_like_a_taken_email(client, outbox, monkeypatch):
    await client.post(REGISTER, json=CREDS)
    _miss_first_check(monkeypatch, "_user_by_email")
    r = await client.post(REGISTER, json={**CREDS, "username": "racer"})
    assert r.status_code == 202  # the unique constraint caught it; still uniform
    assert "already have one" in outbox[-1].body


async def test_username_race_is_a_conflict(client, monkeypatch):
    await client.post(REGISTER, json=CREDS)
    _miss_first_check(monkeypatch, "_username_taken")
    r = await client.post(REGISTER, json={**CREDS, "email": "bob@example.com"})
    assert r.status_code == 409
    assert r.json()["code"] == "USERNAME_TAKEN"


async def test_verifying_clears_a_lockout_from_signing_in_too_early(client, outbox):
    """Pre-verification sign-ins fail and count toward the lockout (they must,
    see `verify_email`), so following the link has to clear it — otherwise a
    new user who tried to sign in first stays locked out after verifying."""
    await client.post(REGISTER, json=CREDS)
    login = {"email": CREDS["email"], "password": CREDS["password"]}
    for _ in range(_settings.login_max_failures):
        assert (await client.post(LOGIN, json=login)).status_code == 401
    assert (await client.post(LOGIN, json=login)).json()["code"] == "ACCOUNT_LOCKED"

    token = re.search(r"verify-email\?token=([A-Za-z0-9_-]+)", outbox[-1].body).group(1)
    assert (await client.post("/api/v1/auth/verify-email", json={"token": token})).status_code == 200
    assert (await client.post(LOGIN, json=login)).status_code == 200


def _cookie_attrs(response, name):
    """The lower-cased attribute list of the `Set-Cookie` header for cookie `name`."""
    header = next(h for h in response.headers.get_list("set-cookie")
                  if h.lower().startswith(f"{name}="))
    return {a.strip().lower() for a in header.split(";")[1:]}


async def test_prod_refresh_cookie_flags(client, signup, monkeypatch):
    """Guards the load-bearing `secure = env == "prod"` in `_set_refresh_cookie`."""
    monkeypatch.setattr(auth_router.settings, "env", "prod")
    await signup(CREDS)
    r = await client.post(LOGIN, json={"email": CREDS["email"], "password": CREDS["password"]})

    refresh = _cookie_attrs(r, "refresh_token")
    assert {"httponly", "secure", "samesite=lax", "path=/api/v1/auth"} <= refresh
    hint = _cookie_attrs(r, "has_session")  # readable by design, but still not sent cross-site
    assert {"secure", "samesite=lax", "path=/"} <= hint and "httponly" not in hint


async def test_dev_refresh_cookie_is_not_secure(client, signup):
    """Dev runs over plain http on localhost, where a Secure cookie would be dropped."""
    await signup(CREDS)
    r = await client.post(LOGIN, json={"email": CREDS["email"], "password": CREDS["password"]})

    refresh = _cookie_attrs(r, "refresh_token")
    assert {"httponly", "samesite=lax", "path=/api/v1/auth"} <= refresh
    assert "secure" not in refresh


def test_env_typo_fails_at_startup(monkeypatch):
    """`ENV=production` must not quietly mean "not prod" (no Secure cookie)."""
    monkeypatch.setenv("ENV", "production")
    with pytest.raises(ValidationError):
        Settings()


def _prod_env(monkeypatch):
    """ENV=prod with a valid, non-default TOTP key, so a test about the *JWT* secret rules
    isolates them: prod also refuses the public dev TOTP key, which would otherwise make
    every "refuses a weak JWT secret" test pass for the wrong reason."""
    from cryptography.fernet import Fernet
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode())


@pytest.mark.parametrize("secret", ["dev-secret-change-me", "too-short"])
def test_prod_refuses_a_weak_jwt_secret(monkeypatch, secret):
    """The dev default is public, so prod must not boot on it (or anything short)."""
    _prod_env(monkeypatch)
    monkeypatch.setenv("JWT_SECRET", secret)
    with pytest.raises(ValidationError):
        Settings()


def test_prod_accepts_a_strong_jwt_secret_and_dev_keeps_the_default(monkeypatch):
    _prod_env(monkeypatch)
    monkeypatch.setenv("JWT_SECRET", "x" * 64)
    assert Settings().env == "prod"
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.delenv("JWT_SECRET")
    assert Settings(_env_file=None).jwt_secret == "dev-secret-change-me"


@pytest.mark.parametrize("previous", ["dev-secret-change-me", "short", "x" * 40 + ",short"])
def test_prod_refuses_a_weak_previous_jwt_secret(monkeypatch, previous):
    """A retired key still verifies tokens, so it needs the same strength as the live one."""
    _prod_env(monkeypatch)
    monkeypatch.setenv("JWT_SECRET", "y" * 64)
    monkeypatch.setenv("JWT_PREVIOUS_SECRETS", previous)
    with pytest.raises(ValidationError):
        Settings()


def test_prod_accepts_strong_previous_jwt_secrets(monkeypatch):
    _prod_env(monkeypatch)
    monkeypatch.setenv("JWT_SECRET", "y" * 64)
    monkeypatch.setenv("JWT_PREVIOUS_SECRETS", f"{'x' * 40}, {'z' * 40}")
    assert Settings().env == "prod"


def test_email_backend_typo_fails_at_startup(monkeypatch):
    """`EMAIL_BACKEND=SMTP` must not silently fall back to console (links only in a log)."""
    monkeypatch.setenv("EMAIL_BACKEND", "SMTP")
    with pytest.raises(ValidationError):
        Settings()


def test_smtp_backend_requires_a_host(monkeypatch):
    monkeypatch.setenv("EMAIL_BACKEND", "smtp")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    assert Settings(_env_file=None).email_backend == "smtp"
