"""TOTP primitives (`app/totp.py`, DESIGN.md §4.1) — checked against the RFC's own vectors."""
import base64

import pytest
from pydantic import ValidationError

from app import totp
from app.config import DEV_TOTP_KEY, Settings

# RFC 6238 Appendix B: SHA-1, secret = ASCII "12345678901234567890". The RFC lists 8-digit
# codes; ours are 6 digits, i.e. the last six of each.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
RFC_VECTORS = [(59, "287082"), (1111111109, "081804"), (1111111111, "050471"),
               (1234567890, "005924"), (2000000000, "279037"), (20000000000, "353130")]


@pytest.mark.parametrize("unix_time,expected", RFC_VECTORS)
def test_codes_match_the_rfc_6238_test_vectors(unix_time, expected):
    assert totp.code_at(RFC_SECRET, totp.current_step(unix_time)) == expected


def test_verify_accepts_the_current_code_and_returns_its_step():
    step = totp.current_step(1111111109)
    assert totp.verify(RFC_SECRET, "081804", now=1111111109) == step


def test_verify_tolerates_one_step_of_clock_skew_but_not_two():
    t = 1111111109
    prev = totp.code_at(RFC_SECRET, totp.current_step(t) - 1)
    nxt = totp.code_at(RFC_SECRET, totp.current_step(t) + 1)
    far = totp.code_at(RFC_SECRET, totp.current_step(t) + 2)
    assert totp.verify(RFC_SECRET, prev, now=t) is not None
    assert totp.verify(RFC_SECRET, nxt, now=t) is not None
    assert totp.verify(RFC_SECRET, far, now=t) is None


def test_a_code_cannot_be_used_twice():
    """`last_step` is the newest step already accepted; that step and older are refused."""
    t = 1111111109
    step = totp.verify(RFC_SECRET, "081804", now=t)
    assert totp.verify(RFC_SECRET, "081804", now=t, last_step=step) is None
    # An older in-window code is refused too, once a newer step was used.
    older = totp.code_at(RFC_SECRET, totp.current_step(t) - 1)
    assert totp.verify(RFC_SECRET, older, now=t, last_step=step) is None


@pytest.mark.parametrize("bad", ["", "12345", "1234567", "abcdef", "08 18 04x", "081805"])
def test_verify_rejects_malformed_and_wrong_codes(bad):
    assert totp.verify(RFC_SECRET, bad, now=1111111109) is None


def test_verify_ignores_spaces_authenticator_apps_group_digits():
    assert totp.verify(RFC_SECRET, "081 804", now=1111111109) is not None


def test_generated_secrets_are_160_bit_base32_and_unique():
    a, b = totp.generate_secret(), totp.generate_secret()
    assert a != b and len(base64.b32decode(a + "=" * (-len(a) % 8))) == 20


def test_provisioning_uri_is_what_authenticator_apps_import():
    uri = totp.provisioning_uri("ABC234", "alice@example.com", "shikomi")
    assert uri.startswith("otpauth://totp/shikomi%3Aalice%40example.com?")
    for part in ("secret=ABC234", "issuer=shikomi", "algorithm=SHA1", "digits=6", "period=30"):
        assert part in uri


def test_a_sealed_secret_round_trips_and_is_not_plaintext():
    secret = totp.generate_secret()
    sealed = totp.seal(secret)
    assert secret not in sealed and totp.unseal(sealed) == secret


def test_key_rotation_keeps_old_secrets_readable(monkeypatch):
    from cryptography.fernet import Fernet
    old, new = totp.settings.totp_encryption_key, Fernet.generate_key().decode()
    sealed = totp.seal("SECRETVALUE")

    monkeypatch.setattr(totp.settings, "totp_encryption_key", new)
    monkeypatch.setattr(totp.settings, "totp_previous_keys", old)
    assert totp.unseal(sealed) == "SECRETVALUE"            # rotated key still reads old data
    assert totp.unseal(totp.seal("X")) == "X"              # and new data uses the new key

    monkeypatch.setattr(totp.settings, "totp_previous_keys", "")
    with pytest.raises(totp.InvalidToken):                  # dropped too early: loud, not silent
        totp.unseal(sealed)


def test_recovery_codes_are_unique_high_entropy_and_normalize():
    codes = totp.generate_recovery_codes()
    assert len(codes) == totp.RECOVERY_CODE_COUNT == len(set(codes))
    code = codes[0]
    assert len(code.replace("-", "")) == 16 and code.count("-") == 3
    assert totp.hash_recovery_code(code) == totp.hash_recovery_code(code.upper().replace("-", " "))
    assert totp.hash_recovery_code(code) != totp.hash_recovery_code(codes[1])


def test_startup_rejects_a_malformed_totp_key(monkeypatch):
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", "not-a-fernet-key")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_prod_refuses_the_public_dev_totp_key(monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("JWT_SECRET", "y" * 64)
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", DEV_TOTP_KEY)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    # ...and a rotated-out dev key is just as public, so it can't linger in the previous list.
    from cryptography.fernet import Fernet
    monkeypatch.setenv("TOTP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("TOTP_PREVIOUS_KEYS", DEV_TOTP_KEY)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    monkeypatch.delenv("TOTP_PREVIOUS_KEYS")
    assert Settings(_env_file=None).env == "prod"
