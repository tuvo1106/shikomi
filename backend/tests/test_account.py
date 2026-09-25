"""Email verification + password reset flows (token via the console outbox)."""
import re
import uuid

import pytest
from sqlalchemy import select, text

from app.errors import APIError
from app.models import User
from app.models.email_token import PURPOSE_RESET, PURPOSE_VERIFY
from app.services import account_service

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"
VERIFY = "/api/v1/auth/verify-email"
RESEND = "/api/v1/auth/resend-verification"
RESET_REQ = "/api/v1/auth/password-reset/request"
RESET_CONFIRM = "/api/v1/auth/password-reset/confirm"

CREDS = {"email": "alice@example.com", "username": "alice", "password": "sunflower-desk-42"}


def _token_from(sent) -> str:
    m = re.search(r"token=([A-Za-z0-9_-]+)", sent.body)
    assert m, sent.body
    return m.group(1)


async def test_register_sends_verification_email(client, outbox):
    r = await client.post(REGISTER, json=CREDS)
    assert r.status_code == 202
    assert len(outbox) == 1
    assert outbox[0].to == CREDS["email"]
    assert "verify-email?token=" in outbox[0].body


async def test_verify_email_flow(client, outbox):
    await client.post(REGISTER, json=CREDS)
    token = _token_from(outbox[-1])

    r = await client.post(VERIFY, json={"token": token})
    assert r.status_code == 200
    assert r.json()["email_verified"] is True

    login = await client.post(LOGIN, json={"email": CREDS["email"], "password": CREDS["password"]})
    me = await client.get(ME, headers={"Authorization": f"Bearer {login.json()['access_token']}"})
    assert me.json()["email_verified"] is True


async def test_verify_email_rejects_bad_token(client, outbox):
    await client.post(REGISTER, json=CREDS)
    r = await client.post(VERIFY, json={"token": "not-a-real-token"})
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_TOKEN"


async def test_verify_email_token_is_single_use(client, outbox):
    await client.post(REGISTER, json=CREDS)
    token = _token_from(outbox[-1])
    assert (await client.post(VERIFY, json={"token": token})).status_code == 200
    replay = await client.post(VERIFY, json={"token": token})
    assert replay.status_code == 400
    assert replay.json()["code"] == "TOKEN_USED"


async def test_resend_verification_by_email(client, outbox):
    """Unauthenticated — an unverified account can't log in to ask for it."""
    await client.post(REGISTER, json=CREDS)
    r = await client.post(RESEND, json={"email": CREDS["email"]})
    assert r.status_code == 202
    assert len(outbox) == 2  # original + resend
    assert "verify-email?token=" in outbox[-1].body


async def test_resend_verification_is_uniform(client, outbox, signup):
    """Unknown and already-verified addresses get the same 202 and no email."""
    await signup(CREDS)
    sent_before = len(outbox)
    verified = await client.post(RESEND, json={"email": CREDS["email"]})
    unknown = await client.post(RESEND, json={"email": "ghost@example.com"})
    assert verified.status_code == unknown.status_code == 202
    assert verified.json() == unknown.json()
    assert len(outbox) == sent_before


async def test_password_reset_flow(client, outbox):
    await client.post(REGISTER, json=CREDS)
    assert (await client.post(RESET_REQ, json={"email": CREDS["email"]})).status_code == 202
    token = _token_from(outbox[-1])

    confirm = await client.post(RESET_CONFIRM, json={"token": token, "password": "newpassword1"})
    assert confirm.status_code == 200

    old = await client.post(LOGIN, json={"email": CREDS["email"], "password": CREDS["password"]})
    new = await client.post(LOGIN, json={"email": CREDS["email"], "password": "newpassword1"})
    assert old.status_code == 401
    assert new.status_code == 200


async def test_password_reset_request_is_uniform_for_unknown_email(client, outbox):
    r = await client.post(RESET_REQ, json={"email": "ghost@example.com"})
    assert r.status_code == 202  # same response as a real address
    assert outbox == []  # but nothing is actually sent


async def test_password_reset_confirm_rejects_bad_token(client):
    r = await client.post(RESET_CONFIRM, json={"token": "nope", "password": "newpassword1"})
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_TOKEN"


# --- email sending is best-effort --------------------------------------------
# A mail hiccup must never break the flow that triggered it, and must not become
# an enumeration oracle in password reset. We simulate an SMTP failure by making
# the underlying dispatcher (which send_best_effort wraps) raise.

def _make_send_fail(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("smtp down")

    monkeypatch.setattr("app.email.send_email", boom)


async def test_register_succeeds_when_email_send_fails(client, monkeypatch, session_factory):
    """A failing send during registration still returns 202 with the row created
    (the user can ask for the link again via resend-verification)."""
    _make_send_fail(monkeypatch)
    r = await client.post(REGISTER, json=CREDS)
    assert r.status_code == 202
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.email == CREDS["email"]))
    assert user is not None and user.email_verified is False


async def test_password_reset_request_uniform_when_email_send_fails(client, make_user, monkeypatch):
    """A failing send on the real-email path stays 202 — same as the unknown path,
    so a mail error can't leak which addresses are registered."""
    await make_user(email=CREDS["email"], username="alice")
    _make_send_fail(monkeypatch)
    registered = await client.post(RESET_REQ, json={"email": CREDS["email"]})
    unknown = await client.post(RESET_REQ, json={"email": "ghost@example.com"})
    assert registered.status_code == 202
    assert unknown.status_code == 202


async def test_send_best_effort_does_not_block_the_event_loop(monkeypatch):
    """`send_email` blocks, so calling it directly would stall every other
    in-flight request on the process for as long as a slow (or hung) SMTP
    conversation took. `send_best_effort` must run `send_email` in a thread
    (`asyncio.to_thread`) so a concurrent task keeps making progress while a
    send is in flight.

    Simulated by making `send_email` a slow synchronous call and racing it
    against a coroutine that records how promptly its own sleeps resolve: if
    `send_best_effort` were blocking the loop, none of those sleeps could fire
    until the slow send returns, so the first one would land near the full
    delay instead of shortly after it was scheduled.
    """
    import asyncio
    import time

    from app import email as email_module

    def slow_send(*_a, **_kw):
        time.sleep(0.3)

    monkeypatch.setattr(email_module, "send_email", slow_send)

    first_heartbeat_at = None

    async def heartbeat():
        nonlocal first_heartbeat_at
        await asyncio.sleep(0.05)
        first_heartbeat_at = time.monotonic()

    start = time.monotonic()
    await asyncio.gather(email_module.send_best_effort("a@example.com", "s", "b"), heartbeat())
    assert first_heartbeat_at is not None
    assert first_heartbeat_at - start < 0.2  # well under the 0.3s send; not blocked


def test_send_smtp_passes_a_socket_timeout(monkeypatch):
    """Regression: `smtplib.SMTP` was constructed with no `timeout`, so a mail
    server that never responds (a dropped connection with no RST, a firewall
    silently eating packets) would hang the send forever instead of failing
    after a bounded wait."""
    from unittest.mock import MagicMock, patch

    from app import email as email_module

    monkeypatch.setattr(email_module.settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(email_module.settings, "smtp_port", 587)
    monkeypatch.setattr(email_module.settings, "smtp_user", "")
    monkeypatch.setattr(email_module.settings, "smtp_timeout_seconds", 7.5)

    fake_smtp = MagicMock()
    with patch("smtplib.SMTP", return_value=fake_smtp) as smtp_cls:
        fake_smtp.__enter__.return_value = fake_smtp
        email_module._send_smtp(email_module.SentEmail("a@example.com", "s", "b"))

    smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=7.5)


# --- password strength policy -----------------------------------------------

async def test_register_rejects_common_password(client, outbox):
    r = await client.post(REGISTER, json={**CREDS, "password": "password123"})
    assert r.status_code == 422
    assert r.json()["code"] == "WEAK_PASSWORD"
    assert outbox == []  # no account created → no verification email


async def test_register_rejects_password_containing_username(client):
    r = await client.post(REGISTER, json={
        "email": "bob@example.com", "username": "bobby", "password": "my-bobby-pw-9"})
    assert r.status_code == 422
    assert r.json()["code"] == "WEAK_PASSWORD"


async def test_reset_rejects_weak_password_without_burning_token(client, outbox):
    await client.post(REGISTER, json=CREDS)
    await client.post(RESET_REQ, json={"email": CREDS["email"]})
    token = _token_from(outbox[-1])

    # A weak new password is rejected...
    weak = await client.post(RESET_CONFIRM, json={"token": token, "password": "password123"})
    assert weak.status_code == 422
    assert weak.json()["code"] == "WEAK_PASSWORD"

    # ...and the token survives, so the user can retry with a strong one.
    good = await client.post(RESET_CONFIRM, json={"token": token, "password": "orbit-lantern-5"})
    assert good.status_code == 200


# --- a token outliving its user -----------------------------------------------
# There's no user-deletion path in the app today (both token tables cascade on
# delete), so we simulate the future/manual-op case a raw DB delete can't
# normally produce: bypass the FK cascade to leave the token orphaned, then
# assert the service raises a clean 400 instead of an AttributeError.

async def _delete_user_bypassing_cascade(session_factory, user_id):
    async with session_factory() as s:
        await s.execute(text("SET session_replication_role = replica"))
        await s.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        await s.execute(text("SET session_replication_role = DEFAULT"))
        await s.commit()


async def test_verify_email_rejects_token_whose_user_was_deleted(session_factory, make_user):
    user, _ = await make_user(verified=False)
    user_id = uuid.UUID(user["id"])
    async with session_factory() as s:
        raw = await account_service._issue_token(
            s, await s.get(User, user_id), PURPOSE_VERIFY, 3600)

    await _delete_user_bypassing_cascade(session_factory, user_id)

    async with session_factory() as s:
        with pytest.raises(APIError) as exc:
            await account_service.verify_email(s, raw)
    assert exc.value.code == "INVALID_TOKEN"


async def test_reset_password_rejects_token_whose_user_was_deleted(session_factory, make_user):
    user, _ = await make_user()
    user_id = uuid.UUID(user["id"])
    async with session_factory() as s:
        raw = await account_service._issue_token(
            s, await s.get(User, user_id), PURPOSE_RESET, 3600)

    await _delete_user_bypassing_cascade(session_factory, user_id)

    async with session_factory() as s:
        with pytest.raises(APIError) as exc:
            await account_service.reset_password(s, raw, "newpassword1")
    assert exc.value.code == "INVALID_TOKEN"
