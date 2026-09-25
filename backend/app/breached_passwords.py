"""Breached-password screening against Have I Been Pwned (NIST 800-63B §5.1.1.2).

NIST asks verifiers to reject passwords "obtained from previous breach corpuses".
The local denylist in `password_policy` only knows ~20 famous ones; HIBP's Pwned
Passwords corpus knows hundreds of millions. This module asks it — without ever
revealing the password.

**The k-anonymity range API.** We SHA-1 the password and send only the first 5 hex
characters of the digest (`GET /range/{prefix}`). HIBP answers with every breached
hash suffix sharing that prefix (~1,000–2,000 of them) plus how often each was
seen, and we look for our own suffix locally. HIBP learns a prefix that hundreds of
unrelated passwords share; the password and its full hash never leave the server.
SHA-1 is fine here: it's the corpus's lookup key, not how we *store* passwords
(that's bcrypt, `app/security.py`).

**Padding.** We send `Add-Padding: true`, so HIBP pads every response with decoy
suffixes whose count is `0`. Without it, the response *size* varies by prefix and
an observer of the (encrypted) traffic could narrow down which prefix was asked
for. The flip side: a matching line only means "breached" when its count is > 0.

**Fail-open.** This is an external dependency on the signup path, so it must not
become a single point of failure: on a timeout, connection error, or non-2xx
response we log a warning and allow the password, falling back to the local policy
alone. The alternative, fail-closed, would let an HIBP outage (or our own egress
misconfiguration) block every signup and password reset. The cost is a window
where a breached password can slip through; the local policy still applies.
"""
import asyncio
import hashlib
import logging

import httpx

from app.config import get_settings
from app.errors import APIError

logger = logging.getLogger(__name__)
settings = get_settings()

# Test seam: tests set this to an `httpx.MockTransport` so the suite never makes a
# real network call. `None` means httpx's normal network transport.
_transport: httpx.AsyncBaseTransport | None = None


def _split_digest(password: str) -> tuple[str, str]:
    """SHA-1 `password` and split the uppercase hex digest into (prefix, suffix).

    The 5-character prefix is the only part that's ever sent; the 35-character
    suffix is what we look for in the response.
    """
    digest = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    return digest[:5], digest[5:]


def _count_for_suffix(body: str, suffix: str) -> int:
    """Return how many times `suffix` appears in a range-API response body.

    Each line is `SUFFIX:COUNT`. Padding lines carry a count of `0`, so a match
    with count 0 is correctly reported as 0 (not breached). No match at all also
    means 0.
    """
    for line in body.splitlines():
        candidate, _, count = line.strip().partition(":")
        if candidate == suffix:
            try:
                return int(count)
            except ValueError:
                return 0  # a malformed line is treated like no match (fail-open)
    return 0


async def breach_count(password: str) -> int | None:
    """Ask HIBP how many times `password` appears in known breaches.

    Returns:
        The breach count (0 if never seen), or `None` if the lookup failed — a
        timeout, network error, or error response. Callers treat `None` as
        "unknown" and fail open.
    """
    prefix, suffix = _split_digest(password)
    url = settings.breached_password_api_url.rstrip("/") + "/" + prefix
    headers = {"Add-Padding": "true", "User-Agent": "shikomi-password-screen"}
    try:
        # httpx's own timeout applies per phase (connect, each read), so a server
        # trickling bytes could keep resetting it; `asyncio.timeout` is the hard
        # ceiling on the whole lookup.
        async with asyncio.timeout(settings.breached_password_timeout_seconds):
            async with httpx.AsyncClient(
                    transport=_transport,
                    timeout=settings.breached_password_timeout_seconds) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
    except (httpx.HTTPError, TimeoutError) as exc:
        # Never log the password, hash, or even the prefix: the exception type is
        # enough to diagnose an outage.
        logger.warning("breached-password check unavailable (%s); "
                       "falling back to the local password policy",
                       type(exc).__name__)
        return None
    return _count_for_suffix(response.text, suffix)


async def check_password_not_breached(password: str) -> None:
    """Raise APIError(422, BREACHED_PASSWORD) if `password` appears in a breach.

    A no-op when `BREACHED_PASSWORD_CHECK` is off, and when the lookup fails
    (fail-open, see the module docstring). Any appearance at all rejects the
    password: the count is how popular it is with attackers, not how safe it is.
    """
    if not settings.breached_password_check:
        return
    count = await breach_count(password)
    if count:
        raise APIError(422, "BREACHED_PASSWORD",
                       "This password has appeared in a known data breach, so attackers "
                       "try it first. Please choose a different one.")
