"""Breached-password screening (app/breached_passwords.py) against a mocked HIBP.

conftest's autouse `no_breach_network` turns screening off and refuses real
network calls; the `hibp` fixture here turns it back on behind an
`httpx.MockTransport` that serves canned range-API responses and records every
request, so each test can assert exactly what would have left the server.
"""
import asyncio
import hashlib
import logging
import re

import httpx
import pytest

from app import breached_passwords

REGISTER = "/api/v1/auth/register"
RESET_REQ = "/api/v1/auth/password-reset/request"
RESET_CONFIRM = "/api/v1/auth/password-reset/confirm"
CHANGE = "/api/v1/auth/change-password"

BREACHED = "correct-horse-battery"   # passes the local policy; the mock marks it breached
CLEAN = "orbit-lantern-5"


def _sha1(password):
    return hashlib.sha1(password.encode()).hexdigest().upper()


class FakeHIBP:
    """Serves `/range/{prefix}` from `self.breached` (password -> count).

    Every response also carries a zero-count padding line for each password in
    `self.padded`, mimicking `Add-Padding: true`.
    """

    def __init__(self):
        self.breached = {BREACHED: 42}
        self.padded = set()
        self.requests = []
        self.fail_with = None      # an exception to raise instead of answering
        self.status = 200
        self.delay = 0.0

    async def handler(self, request):
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_with:
            raise self.fail_with
        prefix = request.url.path.rsplit("/", 1)[-1]
        lines = ["0000000000000000000000000000000000A:3"]  # an unrelated suffix
        for pw, count in self.breached.items():
            if _sha1(pw).startswith(prefix):
                lines.append(f"{_sha1(pw)[5:]}:{count}")
        for pw in self.padded:
            if _sha1(pw).startswith(prefix):
                lines.append(f"{_sha1(pw)[5:]}:0")
        return httpx.Response(self.status, text="\r\n".join(lines))


@pytest.fixture
def hibp(monkeypatch):
    fake = FakeHIBP()
    monkeypatch.setattr(breached_passwords.settings, "breached_password_check", True)
    monkeypatch.setattr(breached_passwords, "_transport", httpx.MockTransport(fake.handler))
    return fake


# --- the range-API protocol ----------------------------------------------------

async def test_only_the_five_char_prefix_leaves_the_server(hibp):
    await breached_passwords.breach_count(CLEAN)
    (request,) = hibp.requests
    digest = _sha1(CLEAN)
    assert request.url.path == f"/range/{digest[:5]}"
    sent = str(request.url) + str(request.headers) + request.content.decode()
    assert digest not in sent and digest[5:] not in sent and CLEAN not in sent


async def test_requests_padding(hibp):
    await breached_passwords.breach_count(CLEAN)
    assert hibp.requests[0].headers["Add-Padding"] == "true"


async def test_breached_password_reports_its_count(hibp):
    assert await breached_passwords.breach_count(BREACHED) == 42


async def test_unseen_password_counts_zero(hibp):
    assert await breached_passwords.breach_count(CLEAN) == 0


async def test_zero_count_padding_line_is_not_a_breach(hibp):
    """With padding on, our own suffix can come back as a decoy with count 0."""
    hibp.padded.add(CLEAN)
    assert await breached_passwords.breach_count(CLEAN) == 0
    await breached_passwords.check_password_not_breached(CLEAN)  # must not raise


async def test_breached_password_is_rejected(hibp):
    with pytest.raises(breached_passwords.APIError) as exc:
        await breached_passwords.check_password_not_breached(BREACHED)
    assert exc.value.code == "BREACHED_PASSWORD"


async def test_disabled_check_makes_no_request(hibp, monkeypatch):
    monkeypatch.setattr(breached_passwords.settings, "breached_password_check", False)
    await breached_passwords.check_password_not_breached(BREACHED)
    assert hibp.requests == []


# --- fail-open ---------------------------------------------------------------

@pytest.mark.parametrize("failure", [httpx.ConnectError("down"), httpx.ReadTimeout("slow")])
async def test_network_failure_fails_open_and_logs_nothing_secret(hibp, caplog, failure):
    hibp.fail_with = failure
    with caplog.at_level(logging.WARNING, logger="app.breached_passwords"):
        assert await breached_passwords.breach_count(BREACHED) is None
        await breached_passwords.check_password_not_breached(BREACHED)  # allowed
    assert "breached-password check unavailable" in caplog.text
    digest = _sha1(BREACHED)
    assert BREACHED not in caplog.text and digest[:5] not in caplog.text


async def test_error_status_fails_open(hibp):
    hibp.status = 503
    assert await breached_passwords.breach_count(BREACHED) is None


async def test_hard_deadline_bounds_a_slow_response(hibp, monkeypatch):
    """httpx's timeout is per phase; `asyncio.timeout` caps the whole lookup."""
    monkeypatch.setattr(breached_passwords.settings, "breached_password_timeout_seconds", 0.05)
    hibp.delay = 1.0
    loop = asyncio.get_running_loop()
    start = loop.time()
    assert await breached_passwords.breach_count(BREACHED) is None
    assert loop.time() - start < 0.5


# --- wired into every place a password is set --------------------------------

async def test_register_rejects_breached_password(client, hibp, outbox):
    r = await client.post(REGISTER, json={
        "email": "alice@example.com", "username": "alice", "password": BREACHED})
    assert r.status_code == 422
    assert r.json()["code"] == "BREACHED_PASSWORD"
    assert outbox == []  # no account created → no verification email


async def test_register_checks_local_policy_before_the_network(client, hibp):
    r = await client.post(REGISTER, json={
        "email": "alice@example.com", "username": "alice", "password": "password123"})
    assert r.json()["code"] == "WEAK_PASSWORD"
    assert hibp.requests == []


async def test_register_succeeds_when_hibp_is_down(client, hibp):
    hibp.fail_with = httpx.ConnectError("down")
    r = await client.post(REGISTER, json={
        "email": "alice@example.com", "username": "alice", "password": BREACHED})
    assert r.status_code == 202


async def test_reset_rejects_breached_password_without_burning_token(client, hibp, outbox):
    await client.post(REGISTER, json={
        "email": "alice@example.com", "username": "alice", "password": CLEAN})
    await client.post(RESET_REQ, json={"email": "alice@example.com"})
    token = re.search(r"token=([\w-]+)", outbox[-1].body).group(1)

    bad = await client.post(RESET_CONFIRM, json={"token": token, "password": BREACHED})
    assert bad.status_code == 422
    assert bad.json()["code"] == "BREACHED_PASSWORD"

    good = await client.post(RESET_CONFIRM, json={"token": token, "password": "violet-harbor-8"})
    assert good.status_code == 200


async def test_change_password_rejects_breached_password(client, hibp, make_user):
    _, headers = await make_user(password="OldPassw0rd-77")
    r = await client.post(CHANGE, headers=headers, json={
        "current_password": "OldPassw0rd-77", "new_password": BREACHED})
    assert r.status_code == 422
    assert r.json()["code"] == "BREACHED_PASSWORD"


def test_malformed_count_is_treated_as_no_match():
    suffix = _sha1(CLEAN)[5:]
    assert breached_passwords._count_for_suffix(f"{suffix}:not-a-number", suffix) == 0
