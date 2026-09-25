"""Two-factor auth end to end (DESIGN.md §4.1): enrollment, the two-step login, replay and
lockout protection, recovery codes, disabling.

A TOTP code is single-use per 30s time step, so realistic multi-login flows can't lean on
the wall clock: the `clock` fixture pins `app.totp`'s notion of "now" and lets a test
advance it a step at a time.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import cli, totp
from app.config import get_settings
from app.models import RecoveryCode, User

LOGIN = "/api/v1/auth/login"
LOGIN_2FA = "/api/v1/auth/login/2fa"
ME = "/api/v1/auth/me"
SETUP = "/api/v1/auth/2fa/setup"
ENABLE = "/api/v1/auth/2fa/enable"
DISABLE = "/api/v1/auth/2fa/disable"
RECOVERY = "/api/v1/auth/2fa/recovery-codes"

CREDS = {"email": "alice@example.com", "username": "alice", "password": "sunflower-desk-42"}
MAX_FAILURES = get_settings().login_max_failures


@pytest.fixture(autouse=True)
def roomy_ip_rate_limit(monkeypatch):
    """These flows make dozens of auth calls from one client. The per-IP cap is a different
    defense (tested in test_rate_limit.py); raising it here keeps *account lockout* — the
    behavior under test — from being pre-empted by `RATE_LIMITED`."""
    monkeypatch.setattr(get_settings(), "auth_rate_limit_per_minute", 10_000)


@pytest.fixture
def clock(monkeypatch):
    """A controllable clock for `app.totp`; `advance()` moves it one 30s step (or `n`)."""
    state = SimpleNamespace(t=1_700_000_000.0)
    state.advance = lambda steps=1: setattr(state, "t", state.t + steps * totp.STEP_SECONDS)
    monkeypatch.setattr(totp, "time", SimpleNamespace(time=lambda: state.t))
    return state


def code_now(secret):
    return totp.code_at(secret, totp.current_step())


async def bearer(client):
    r = await client.post(LOGIN, json={"email": CREDS["email"], "password": CREDS["password"]})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
async def enrolled(client, signup, clock):
    """A verified user with 2FA switched on. Returns the secret and the recovery codes.

    The enabling code consumed the current time step, so the clock has moved one step on:
    the next code is fresh, exactly as it would be for a real user logging in later.
    """
    await signup(CREDS)
    headers = await bearer(client)
    secret = (await client.post(SETUP, headers=headers)).json()["secret"]
    r = await client.post(ENABLE, headers=headers, json={"code": code_now(secret)})
    assert r.status_code == 200, r.text
    clock.advance()
    return SimpleNamespace(secret=secret, codes=r.json()["recovery_codes"], headers=headers)


async def password_step(client):
    return await client.post(LOGIN, json={"email": CREDS["email"], "password": CREDS["password"]})


async def second_step(client, mfa_token, code, **extra):
    return await client.post(LOGIN_2FA, json={"mfa_token": mfa_token, "code": code, **extra})


# --- enrollment -------------------------------------------------------------------------

async def test_setup_then_enable_turns_2fa_on_and_returns_recovery_codes(
        client, signup, clock, session_factory):
    await signup(CREDS)
    headers = await bearer(client)
    assert (await client.get(ME, headers=headers)).json()["totp_enabled"] is False

    setup = (await client.post(SETUP, headers=headers)).json()
    assert setup["otpauth_uri"].startswith("otpauth://totp/") and setup["secret"] in setup["otpauth_uri"]
    # Pending only: nothing is enforced until a code proves the authenticator works.
    assert (await client.get(ME, headers=headers)).json()["totp_enabled"] is False

    r = await client.post(ENABLE, headers=headers, json={"code": code_now(setup["secret"])})
    assert r.status_code == 200 and len(r.json()["recovery_codes"]) == totp.RECOVERY_CODE_COUNT
    assert (await client.get(ME, headers=headers)).json()["totp_enabled"] is True

    async with session_factory() as s:
        user = await s.scalar(select(User).where(User.email == CREDS["email"]))
        assert setup["secret"] not in user.totp_secret_enc          # encrypted at rest
        assert totp.unseal(user.totp_secret_enc) == setup["secret"]
        hashes = (await s.scalars(select(RecoveryCode.code_hash))).all()
        assert not set(r.json()["recovery_codes"]) & set(hashes)    # only hashes are stored
        assert len(hashes) == totp.RECOVERY_CODE_COUNT


async def test_enable_rejects_a_wrong_code_and_leaves_2fa_off(client, signup, clock):
    await signup(CREDS)
    headers = await bearer(client)
    await client.post(SETUP, headers=headers)

    r = await client.post(ENABLE, headers=headers, json={"code": "000000"})

    assert r.status_code == 400 and r.json()["code"] == "INVALID_CODE"
    assert (await client.get(ME, headers=headers)).json()["totp_enabled"] is False


async def test_enable_needs_setup_first_and_setup_refuses_when_already_on(
        client, signup, clock, enrolled):
    r = await client.post(SETUP, headers=enrolled.headers)
    assert r.status_code == 409 and r.json()["code"] == "TOTP_ALREADY_ENABLED"
    r = await client.post(ENABLE, headers=enrolled.headers, json={"code": code_now(enrolled.secret)})
    assert r.status_code == 409


async def test_enable_without_setup_is_a_clear_error(client, signup, clock):
    await signup(CREDS)
    r = await client.post(ENABLE, headers=await bearer(client), json={"code": "123456"})
    assert r.status_code == 400 and r.json()["code"] == "TOTP_NOT_STARTED"


async def test_management_endpoints_need_a_session(client):
    for path, body in ((SETUP, {}), (ENABLE, {"code": "123456"}), (RECOVERY, {"code": "123456"}),
                       (DISABLE, {"password": "x", "code": "123456"})):
        assert (await client.post(path, json=body)).status_code == 401


# --- the two-step login -----------------------------------------------------------------

async def test_login_with_2fa_returns_a_challenge_and_no_session(client, enrolled):
    r = await password_step(client)

    assert r.status_code == 200
    assert r.json()["mfa_required"] is True and "access_token" not in r.json()
    assert "refresh_token" not in r.headers.get("set-cookie", "")  # no session yet


async def test_second_step_with_a_valid_code_completes_the_login(client, enrolled):
    challenge = (await password_step(client)).json()

    r = await second_step(client, challenge["mfa_token"], code_now(enrolled.secret))

    assert r.status_code == 200
    assert "refresh_token=" in r.headers["set-cookie"]
    me = await client.get(ME, headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert me.status_code == 200 and me.json()["totp_enabled"] is True


async def test_a_wrong_password_looks_the_same_with_or_without_2fa(client, enrolled):
    """The 2FA prompt only follows a *correct* password, so it can't reveal which emails
    are registered or have 2FA."""
    r = await client.post(LOGIN, json={"email": CREDS["email"], "password": "wrong-password-1"})
    assert r.status_code == 401 and r.json()["code"] == "INVALID_CREDENTIALS"


async def test_the_mfa_token_cannot_be_used_as_a_bearer_token(client, enrolled):
    """The password alone must never open the API."""
    challenge = (await password_step(client)).json()
    r = await client.get(ME, headers={"Authorization": f"Bearer {challenge['mfa_token']}"})
    assert r.status_code == 401


async def test_an_access_token_cannot_stand_in_for_the_challenge(client, enrolled):
    r = await second_step(client, enrolled.headers["Authorization"].split()[1],
                          code_now(enrolled.secret))
    assert r.status_code == 401 and r.json()["code"] == "INVALID_MFA_TOKEN"


async def test_an_expired_challenge_is_refused(client, enrolled, monkeypatch):
    monkeypatch.setattr(get_settings(), "mfa_token_ttl_seconds", -1)
    challenge = (await password_step(client)).json()
    r = await second_step(client, challenge["mfa_token"], code_now(enrolled.secret))
    assert r.status_code == 401 and r.json()["code"] == "INVALID_MFA_TOKEN"


async def test_a_garbage_challenge_is_refused(client, enrolled):
    r = await second_step(client, "not-a-token", "123456")
    assert r.status_code == 401 and r.json()["code"] == "INVALID_MFA_TOKEN"


# --- replay and lockout -----------------------------------------------------------------

async def test_a_code_cannot_be_used_twice(client, enrolled, clock):
    """An intercepted code is useless once the real user has spent it (within its window)."""
    code = code_now(enrolled.secret)
    first = await second_step(client, (await password_step(client)).json()["mfa_token"], code)
    replay = await second_step(client, (await password_step(client)).json()["mfa_token"], code)

    assert first.status_code == 200
    assert replay.status_code == 401 and replay.json()["code"] == "INVALID_CODE"


async def test_wrong_codes_lock_the_account_like_wrong_passwords(client, enrolled):
    token = (await password_step(client)).json()["mfa_token"]
    for _ in range(MAX_FAILURES):
        r = await second_step(client, token, "000000")
        assert r.status_code == 401
    r = await second_step(client, token, code_now(enrolled.secret))  # even the right one now
    assert r.status_code == 429 and r.json()["code"] == "ACCOUNT_LOCKED"


async def test_a_correct_password_does_not_reset_the_code_failure_count(client, enrolled, queue):
    """Otherwise someone who knows the password could alternate password logins with code
    guesses and never reach the lockout."""
    token = (await password_step(client)).json()["mfa_token"]
    for _ in range(MAX_FAILURES - 1):
        await second_step(client, token, "000000")

    await password_step(client)                         # correct password again...
    r = await second_step(client, token, "000000")      # ...one more wrong code

    assert r.status_code == 401
    assert await queue.get_login_lock(CREDS["email"]) > 0
    assert (await password_step(client)).status_code == 429


async def test_a_successful_login_clears_the_failure_count(client, enrolled, queue):
    token = (await password_step(client)).json()["mfa_token"]
    await second_step(client, token, "000000")

    await second_step(client, token, code_now(enrolled.secret))

    assert queue.login_failures.get(CREDS["email"]) is None


# --- recovery codes ---------------------------------------------------------------------

async def test_a_recovery_code_logs_in_once(client, enrolled):
    code = enrolled.codes[0]
    first = await second_step(client, (await password_step(client)).json()["mfa_token"], code)
    again = await second_step(client, (await password_step(client)).json()["mfa_token"], code)

    assert first.status_code == 200
    assert again.status_code == 401 and again.json()["code"] == "INVALID_CODE"


async def test_recovery_codes_ignore_case_and_dashes(client, enrolled):
    messy = enrolled.codes[1].upper().replace("-", " ")
    r = await second_step(client, (await password_step(client)).json()["mfa_token"], messy)
    assert r.status_code == 200


async def test_regenerating_recovery_codes_kills_the_old_ones(client, enrolled, clock):
    r = await client.post(RECOVERY, headers=enrolled.headers, json={"code": code_now(enrolled.secret)})
    new_codes = r.json()["recovery_codes"]
    assert r.status_code == 200 and not set(new_codes) & set(enrolled.codes)

    old = await second_step(client, (await password_step(client)).json()["mfa_token"], enrolled.codes[0])
    fresh = await second_step(client, (await password_step(client)).json()["mfa_token"], new_codes[0])
    assert old.status_code == 401 and fresh.status_code == 200


async def test_regenerating_needs_a_valid_code(client, enrolled):
    r = await client.post(RECOVERY, headers=enrolled.headers, json={"code": "000000"})
    assert r.status_code == 400 and r.json()["code"] == "INVALID_CODE"


# --- disabling --------------------------------------------------------------------------

async def test_disable_needs_the_password_and_a_code(client, enrolled, clock):
    wrong_pw = await client.post(DISABLE, headers=enrolled.headers,
                                 json={"password": "nope-nope-1", "code": code_now(enrolled.secret)})
    assert wrong_pw.status_code == 400 and wrong_pw.json()["code"] == "INVALID_CURRENT_PASSWORD"

    wrong_code = await client.post(DISABLE, headers=enrolled.headers,
                                   json={"password": CREDS["password"], "code": "000000"})
    assert wrong_code.status_code == 400 and wrong_code.json()["code"] == "INVALID_CODE"
    assert (await password_step(client)).json().get("mfa_required") is True  # still on


async def test_disable_removes_all_2fa_state_and_login_is_password_only_again(
        client, enrolled, session_factory):
    r = await client.post(DISABLE, headers=enrolled.headers,
                          json={"password": CREDS["password"], "code": code_now(enrolled.secret)})
    assert r.status_code == 200

    login = await password_step(client)
    assert "access_token" in login.json() and "mfa_required" not in login.json()
    async with session_factory() as s:
        user = await s.scalar(select(User).where(User.email == CREDS["email"]))
        assert user.totp_enabled is False and user.totp_secret_enc is None
        assert (await s.scalars(select(RecoveryCode))).all() == []


async def test_a_challenge_is_dead_if_2fa_was_turned_off_meanwhile(client, enrolled, clock):
    challenge = (await password_step(client)).json()
    await client.post(DISABLE, headers=enrolled.headers,
                      json={"password": CREDS["password"], "code": code_now(enrolled.secret)})
    r = await second_step(client, challenge["mfa_token"], "123456")
    assert r.status_code == 401 and r.json()["code"] == "INVALID_MFA_TOKEN"


async def test_disable_2fa_cli_recovers_a_locked_out_user(client, enrolled, session_factory,
                                                          monkeypatch):
    monkeypatch.setattr(cli, "SessionLocal", session_factory)

    assert await cli.disable_2fa(CREDS["email"]) == 0
    assert "access_token" in (await password_step(client)).json()
    assert await cli.disable_2fa("nobody@example.com") == 1


# --- review follow-ups ------------------------------------------------------------------

async def test_non_ascii_digits_are_a_wrong_code_not_a_server_error(client, enrolled):
    """str.isdigit() accepts Unicode digits; compare_digest would then raise on them."""
    token = (await password_step(client)).json()["mfa_token"]
    for code in ("١٢٣٤٥٦", "²²²²²²"):
        r = await second_step(client, token, code)
        assert r.status_code == 401 and r.json()["code"] == "INVALID_CODE"


async def test_wrong_answers_on_disable_count_toward_the_account_lockout(client, enrolled):
    """A stolen access token must not get an unlimited code/password guessing oracle."""
    for _ in range(MAX_FAILURES):
        r = await client.post(DISABLE, headers=enrolled.headers,
                              json={"password": CREDS["password"], "code": "000000"})
        assert r.status_code == 400
    r = await client.post(DISABLE, headers=enrolled.headers,
                          json={"password": CREDS["password"], "code": code_now(enrolled.secret)})
    assert r.status_code == 429 and r.json()["code"] == "ACCOUNT_LOCKED"


async def test_wrong_codes_on_regenerate_count_toward_the_account_lockout(client, enrolled):
    for _ in range(MAX_FAILURES):
        await client.post(RECOVERY, headers=enrolled.headers, json={"code": "000000"})
    r = await client.post(RECOVERY, headers=enrolled.headers,
                          json={"code": code_now(enrolled.secret)})
    assert r.status_code == 429 and r.json()["code"] == "ACCOUNT_LOCKED"


async def test_a_password_change_kills_an_outstanding_challenge(client, enrolled, clock):
    challenge = (await password_step(client)).json()["mfa_token"]
    r = await client.post("/api/v1/auth/change-password", headers=enrolled.headers,
                          json={"current_password": CREDS["password"],
                                "new_password": "a-brand-new-password-9"})
    assert r.status_code == 200, r.text

    r = await second_step(client, challenge, code_now(enrolled.secret))
    assert r.status_code == 401 and r.json()["code"] == "INVALID_MFA_TOKEN"
