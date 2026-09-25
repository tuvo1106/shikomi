"""Two-factor auth primitives: TOTP codes (RFC 6238), secret encryption, recovery codes.

The mental model: a TOTP secret is a shared key between us and the user's authenticator
app. Both sides derive a 6-digit code from `HMAC(secret, floor(unix_time / 30))`, so
the code changes every 30 seconds and proves the user holds the secret *right now*.

Why hand-rolled (like the rest of `app/security.py`): RFC 6238 on top of RFC 4226's
HOTP is ~20 lines of stdlib `hmac`, so a dependency buys nothing here and this stays
readable as a learning artifact. It is checked against the RFC's own test vectors.

Three things live here and nowhere else, so the rules stay in one place:

* **Codes** — generating a secret, computing/verifying a code with a small clock-skew
  window and single-use enforcement (`verify` returns the accepted time step so the
  caller can refuse to accept the same step twice: an eavesdropped code is otherwise
  good for its whole ~90s window).
* **Sealing** — TOTP secrets must be stored *recoverably* (we need the secret to verify
  a code), unlike passwords, so they're Fernet-encrypted at rest. `MultiFernet` lets the
  key rotate: encrypt with the newest key, decrypt with any (`TOTP_PREVIOUS_KEYS`).
* **Recovery codes** — one-time backup codes for a lost phone. Random and high-entropy,
  so like refresh tokens they're stored only as a SHA-256 hash.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

from cryptography.fernet import Fernet, InvalidToken, MultiFernet  # noqa: F401 (InvalidToken re-exported)

from app.config import get_settings
from app.security import hash_token

settings = get_settings()

STEP_SECONDS = 30
DIGITS = 6
# Accept the code for the previous, current and next 30s step. Phone clocks drift and a
# user needs a moment to type; ±1 step (90s total) is the RFC's usual recommendation.
WINDOW = 1
RECOVERY_CODE_COUNT = 10


def generate_secret() -> str:
    """A fresh 160-bit secret, base32 without padding (the form authenticator apps take).

    160 bits is RFC 4226's recommended length. `secrets` (not `random`) because this is a
    cryptographic key.
    """
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(key: bytes, counter: int) -> str:
    """RFC 4226 HOTP: HMAC-SHA1 of the counter, dynamically truncated to `DIGITS` digits.

    "Dynamic truncation" takes 4 bytes starting at an offset given by the low nibble of the
    last byte, masks the top bit (to stay a positive int) and reduces mod 10^DIGITS.
    """
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def _key(secret: str) -> bytes:
    return base64.b32decode(secret + "=" * (-len(secret) % 8))


def current_step(now: float | None = None) -> int:
    """The 30-second time step the given unix time (default: now) falls in."""
    return int((time.time() if now is None else now) // STEP_SECONDS)


def code_at(secret: str, step: int) -> str:
    """The code an authenticator app shows for `secret` during time step `step`."""
    return _hotp(_key(secret), step)


def verify(secret: str, code: str, *, now: float | None = None,
           last_step: int | None = None) -> int | None:
    """Check `code` against `secret`; return the time step it matched, else None.

    `last_step` is the newest step already accepted for this user: any step at or before
    it is refused, so a code can be used **once** (replay protection). The caller must
    persist the returned step (ideally with a conditional update, so two concurrent
    requests can't both claim it).

    All window steps are compared even after a match, and each comparison is constant-time,
    so timing doesn't reveal which step (or how much of the code) matched.
    """
    code = code.replace(" ", "")
    # isascii(): str.isdigit() also accepts Unicode digits ('١٢٣٤٥٦'), and compare_digest
    # raises TypeError on non-ASCII str, which would surface as a 500.
    if len(code) != DIGITS or not (code.isascii() and code.isdigit()):
        return None
    key = _key(secret)
    now_step = current_step(now)
    matched: int | None = None
    for step in range(now_step - WINDOW, now_step + WINDOW + 1):
        if hmac.compare_digest(_hotp(key, step), code) and (last_step is None or step > last_step):
            matched = step
    return matched


def provisioning_uri(secret: str, account: str, issuer: str | None = None) -> str:
    """The `otpauth://` URI authenticator apps import (usually by scanning it as a QR code).

    The issuer appears both in the label and as a parameter, which is what Google
    Authenticator and most others expect. SHA1/6/30 are the defaults every app supports.
    """
    issuer = issuer or settings.totp_issuer
    label = quote(f"{issuer}:{account}")
    query = urlencode({"secret": secret, "issuer": issuer, "algorithm": "SHA1",
                       "digits": DIGITS, "period": STEP_SECONDS}, quote_via=quote)
    return f"otpauth://totp/{label}?{query}"


# --- encryption at rest ------------------------------------------------------------------

def _fernet() -> MultiFernet:
    keys = [settings.totp_encryption_key]
    keys += [k.strip() for k in settings.totp_previous_keys.split(",") if k.strip()]
    return MultiFernet([Fernet(k) for k in keys])


def seal(secret: str) -> str:
    """Encrypt a TOTP secret for storage (newest key)."""
    return _fernet().encrypt(secret.encode()).decode()


def unseal(sealed: str) -> str:
    """Decrypt a stored TOTP secret (any configured key).

    Raises:
        cryptography.fernet.InvalidToken: no configured key decrypts it (key lost or
            rotated out too early) — surfaced, not swallowed, because silently treating
            it as "no 2FA" would be a downgrade.
    """
    return _fernet().decrypt(sealed.encode()).decode()


# --- recovery codes ----------------------------------------------------------------------

def generate_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    """`count` one-time codes shaped `xxxx-xxxx-xxxx-xxxx` (64 random bits each).

    Grouped so they're easy to read off a printout; the dashes are cosmetic and stripped
    when checking (`normalize_recovery_code`).
    """
    codes = []
    for _ in range(count):
        raw = secrets.token_hex(8)
        codes.append("-".join(raw[i:i + 4] for i in range(0, 16, 4)))
    return codes


def normalize_recovery_code(code: str) -> str:
    """Lower-case and drop dashes/spaces, so `ABCD-1234 …` and `abcd1234…` are one code."""
    return code.replace("-", "").replace(" ", "").lower()


def hash_recovery_code(code: str) -> str:
    """The stored form of a recovery code (SHA-256 of the normalized code)."""
    return hash_token(normalize_recovery_code(code))
