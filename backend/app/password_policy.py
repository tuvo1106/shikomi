"""Password strength screening (NIST 800-63B style).

Modern guidance favors *length* and *screening against known-bad passwords* over
the old "must have an uppercase, a digit, and a symbol" composition rules — which
mostly push users toward predictable patterns (`Password1!`) without adding real
entropy. So we check three things: a length floor, a common-password denylist,
and that the password doesn't embed the user's own email/username.

The denylist here is intentionally tiny: it catches the famous few instantly and
offline. The large breached-password corpus lives in `app/breached_passwords.py`
(Have I Been Pwned's k-anonymity range API), and `enforce_password_policy` runs
both — this local check first, so an obviously weak password never costs a
network round-trip.
"""
from app.breached_passwords import check_password_not_breached
from app.errors import APIError

MIN_LENGTH = 8

# A small sample of the most-guessed passwords. Compared case-insensitively and
# by exact match (substring matching would wrongly reject fine passwords that
# merely contain "password").
_COMMON = frozenset({
    "password", "password1", "password123", "passw0rd", "12345678", "123456789",
    "1234567890", "qwerty", "qwertyuiop", "letmein", "iloveyou", "admin", "welcome",
    "monkey", "abc12345", "changeme", "football", "baseball", "dragon", "sunshine",
})


def _reject(reason: str) -> None:
    raise APIError(422, "WEAK_PASSWORD", reason)


def check_password_strength(password: str, *, email: str | None = None,
                            username: str | None = None) -> None:
    """Raise APIError(422, WEAK_PASSWORD) if `password` is unacceptable; else return.

    Called from the service layer (register, password reset) where the user's
    email/username are known, so we can forbid a password that's trivially derived
    from identifying info.

    Args:
        password: the plaintext candidate.
        email/username: the account's identifiers, to screen against (optional).
    """
    if len(password) < MIN_LENGTH:
        _reject(f"Password must be at least {MIN_LENGTH} characters.")

    lowered = password.lower()
    if lowered in _COMMON:
        _reject("That password is too common — choose something less guessable.")

    # Only screen identifiers long enough to be meaningful (avoid matching a
    # 1–2 char username as an accidental substring of a good password).
    if username and len(username) >= 3 and username.lower() in lowered:
        _reject("Password must not contain your username.")
    local = (email or "").split("@")[0].lower()
    if local and len(local) >= 3 and local in lowered:
        _reject("Password must not contain your email address.")


async def enforce_password_policy(password: str, *, email: str | None = None,
                                  username: str | None = None) -> None:
    """The full policy for a new password: local strength rules, then breach screening.

    Every place a password is *set* (register, reset, change) calls this rather
    than the two checks separately, so none of them can forget one. Order
    matters: the local check is instant and offline, so it runs first and a
    password it rejects never reaches the network.

    Raises:
        APIError(422, WEAK_PASSWORD): fails the local strength rules.
        APIError(422, BREACHED_PASSWORD): appears in a known breach.
    """
    check_password_strength(password, email=email, username=username)
    await check_password_not_breached(password)
