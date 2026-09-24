"""Audit logging of security-relevant auth events.

These assert that the right events land on the `app.audit` logger — and, just as
importantly, that no secret (password, token, hash) ever appears in the record.
`caplog` captures log output; we set the level on the `app.audit` logger so the
INFO-level audit records propagate to it.
"""
import logging

import pytest

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
VERIFY = "/api/v1/auth/verify-email"
RESET_REQ = "/api/v1/auth/password-reset/request"
RESET_CONFIRM = "/api/v1/auth/password-reset/confirm"

CREDS = {"email": "alice@example.com", "username": "alice", "password": "sunflower-desk-42"}


@pytest.fixture
def audit_logs(caplog):
    caplog.set_level(logging.INFO, logger="app.audit")
    return caplog


def _events(caplog):
    return [r.audit_event for r in caplog.records if hasattr(r, "audit_event")]


async def test_register_and_login_emit_audit_events(client, audit_logs, signup):
    await signup(CREDS)
    assert "auth.register" in _events(audit_logs)

    await client.post(LOGIN, json={"email": CREDS["email"], "password": CREDS["password"]})
    assert "auth.login.success" in _events(audit_logs)


async def test_failed_login_and_lockout_are_audited(client, audit_logs, signup):
    from app.config import get_settings

    await signup(CREDS)
    for _ in range(get_settings().login_max_failures):
        await client.post(LOGIN, json={"email": CREDS["email"], "password": "wrong"})

    events = _events(audit_logs)
    assert "auth.login.failure" in events
    assert "auth.login.locked" in events

    # A subsequent attempt while locked is recorded as blocked.
    await client.post(LOGIN, json={"email": CREDS["email"], "password": "wrong"})
    assert "auth.login.blocked" in _events(audit_logs)


async def test_verify_and_reset_are_audited(client, outbox, audit_logs, signup):
    import re

    await signup(CREDS)
    token = re.search(r"token=([A-Za-z0-9_-]+)", outbox[-1].body).group(1)
    await client.post(VERIFY, json={"token": token})
    assert "auth.email.verified" in _events(audit_logs)

    await client.post(RESET_REQ, json={"email": CREDS["email"]})
    assert "auth.password_reset.requested" in _events(audit_logs)
    reset_token = re.search(r"token=([A-Za-z0-9_-]+)", outbox[-1].body).group(1)
    await client.post(RESET_CONFIRM, json={"token": reset_token, "password": "orbit-lantern-5"})
    assert "auth.password_reset.completed" in _events(audit_logs)


async def test_audit_never_logs_secrets(client, audit_logs, signup):
    await signup(CREDS)
    await client.post(LOGIN, json={"email": CREDS["email"], "password": CREDS["password"]})
    # The refresh token cookie and the password must not appear in any audit record.
    blob = "\n".join(r.getMessage() for r in audit_logs.records)
    assert CREDS["password"] not in blob
    refresh = client.cookies.get("refresh_token")
    assert refresh and refresh not in blob
