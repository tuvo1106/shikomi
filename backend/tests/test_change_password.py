"""Authenticated password change from the Settings page (POST /auth/change-password)."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models import RefreshToken, User
from app.security import hash_token

pytestmark = pytest.mark.asyncio

STRONG = "Chinchilla-Ladder-92"


async def test_change_password_success(client, make_user):
    user, headers = await make_user(password="OldPassw0rd-77")
    r = await client.post("/api/v1/auth/change-password", headers=headers,
                          json={"current_password": "OldPassw0rd-77", "new_password": STRONG})
    assert r.status_code == 200
    # New password works, old one is rejected.
    ok = await client.post("/api/v1/auth/login",
                           json={"email": user["email"], "password": STRONG})
    assert ok.status_code == 200
    bad = await client.post("/api/v1/auth/login",
                            json={"email": user["email"], "password": "OldPassw0rd-77"})
    assert bad.status_code == 401


async def test_change_password_wrong_current(client, user_headers):
    r = await client.post("/api/v1/auth/change-password", headers=user_headers,
                          json={"current_password": "not-my-password", "new_password": STRONG})
    assert r.status_code == 400
    assert r.json()["code"] == "INVALID_CURRENT_PASSWORD"


async def test_change_password_weak_new(client, make_user):
    _, headers = await make_user(password="OldPassw0rd-77")
    r = await client.post("/api/v1/auth/change-password", headers=headers,
                          json={"current_password": "OldPassw0rd-77", "new_password": "password"})
    assert r.status_code == 422
    assert r.json()["code"] == "WEAK_PASSWORD"


async def test_change_password_revokes_other_sessions(session_factory, make_user):
    """The service keeps the caller's session and revokes every other one."""
    from app.services import account_service

    user, _ = await make_user(password="OldPassw0rd-77")
    uid = uuid.UUID(user["id"])
    exp = datetime.now(timezone.utc) + timedelta(days=1)
    async with session_factory() as s:
        s.add(RefreshToken(user_id=uid, token_hash=hash_token("current"), expires_at=exp))
        s.add(RefreshToken(user_id=uid, token_hash=hash_token("other"), expires_at=exp))
        await s.commit()
        row = await s.get(User, uid)
        await account_service.change_password(
            s, row, "OldPassw0rd-77", STRONG, keep_token_hash=hash_token("current"))

    async with session_factory() as s:
        by_hash = {rt.token_hash: rt.revoked_at
                   for rt in (await s.execute(
                       select(RefreshToken).where(RefreshToken.user_id == uid))).scalars()}
    assert by_hash[hash_token("current")] is None       # kept — caller stays logged in
    assert by_hash[hash_token("other")] is not None     # revoked
