"""JWT signing-key rotation (`app/security.py`, DESIGN.md §3.6)."""
import jwt
import pytest

from app import security
from app.models import User

OLD, NEW = "old-secret-" + "a" * 40, "new-secret-" + "b" * 40


@pytest.fixture
def user():
    return User(id="00000000-0000-0000-0000-000000000001")


def _use(monkeypatch, current, previous=""):
    monkeypatch.setattr(security.settings, "jwt_secret", current)
    monkeypatch.setattr(security.settings, "jwt_previous_secrets", previous)


def test_token_names_its_key(monkeypatch, user):
    _use(monkeypatch, OLD)
    token = security.create_access_token(user)
    assert jwt.get_unverified_header(token)["kid"] == security._kid(OLD)
    assert security.decode_access_token(token)["sub"] == str(user.id)


def test_rotation_keeps_old_tokens_valid_and_signs_with_new(monkeypatch, user):
    _use(monkeypatch, OLD)
    old_token = security.create_access_token(user)

    _use(monkeypatch, NEW, previous=OLD)  # the rotation: old key moves to verify-only
    assert security.decode_access_token(old_token)["sub"] == str(user.id)
    new_token = security.create_access_token(user)
    assert jwt.get_unverified_header(new_token)["kid"] == security._kid(NEW)
    assert security.decode_access_token(new_token)["sub"] == str(user.id)


def test_retired_key_stops_verifying(monkeypatch, user):
    _use(monkeypatch, OLD)
    old_token = security.create_access_token(user)

    _use(monkeypatch, NEW)  # old key dropped once the access TTL has passed
    with pytest.raises(jwt.PyJWTError):
        security.decode_access_token(old_token)


def test_swapped_kid_still_fails_the_signature(monkeypatch, user):
    """`kid` only picks a key; it can't make a token signed with another key valid."""
    _use(monkeypatch, NEW, previous=OLD)
    forged = jwt.encode({"sub": str(user.id), "typ": "access", "exp": 4102444800},
                        "attacker-key", algorithm="HS256",
                        headers={"kid": security._kid(NEW)})
    with pytest.raises(jwt.PyJWTError):
        security.decode_access_token(forged)


def test_unknown_kid_is_rejected(monkeypatch, user):
    _use(monkeypatch, NEW)
    token = jwt.encode({"sub": str(user.id), "exp": 4102444800}, NEW, algorithm="HS256",
                       headers={"kid": "deadbeef"})
    with pytest.raises(jwt.PyJWTError):
        security.decode_access_token(token)


def test_token_without_kid_is_tried_against_every_known_key(monkeypatch, user):
    """A token with no `kid` header still verifies against the current or a previous key,
    and stops verifying once its key is retired."""
    legacy = jwt.encode({"sub": str(user.id), "exp": 4102444800}, OLD, algorithm="HS256")
    _use(monkeypatch, OLD)
    assert security.decode_access_token(legacy)["sub"] == str(user.id)
    _use(monkeypatch, NEW, previous=OLD)
    assert security.decode_access_token(legacy)["sub"] == str(user.id)
    _use(monkeypatch, NEW)
    with pytest.raises(jwt.PyJWTError):
        security.decode_access_token(legacy)


def test_blank_previous_entries_are_ignored(monkeypatch):
    _use(monkeypatch, NEW, previous=" , ,")
    assert list(security._verification_keys()) == [security._kid(NEW)]


async def test_api_accepts_a_pre_rotation_token(client, make_user, monkeypatch):
    """End to end: a session minted under the old key survives the rotation."""
    _use(monkeypatch, OLD)
    _, headers = await make_user()
    _use(monkeypatch, NEW, previous=OLD)
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200
    _use(monkeypatch, NEW)
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401
