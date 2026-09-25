"""Per-IP rate limiting for auth endpoints (DESIGN.md §4.1).

The first line of brute-force defense: cap how many auth requests a single IP can
make per minute. It's a Redis fixed-window counter (via `Queue.within_rate_limit`)
rather than in-memory, so the limit **survives restarts and is shared across API
replicas** — an attacker can't reset it with a redeploy or dodge it by spreading
requests over instances. The second line (per-account lockout on *failed* logins)
lives in the login route; this cap counts every attempt regardless of outcome.
"""
import time

from fastapi import Depends, Request

from app.config import get_settings
from app.errors import APIError
from app.queue import Queue, get_queue

settings = get_settings()


def _client_ip(request: Request) -> str:
    """Best-effort client IP — the rate-limit key.

    Behind a proxy the socket peer is the proxy, so the real client is the first
    hop of `X-Forwarded-For` (the client Caddy saw). But that header is
    client-supplied and trivially forged, so we read it **only** when
    `trust_proxy` is set — i.e. the operator asserts the API is reachable strictly
    through a proxy that overwrites it. Otherwise we fall back to the socket peer:
    a direct client can't then spoof its way into a fresh rate-limit bucket per
    forged IP. Fail-closed default keeps a proxy-less deploy safe.
    """
    if settings.trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def rate_limit_auth(request: Request, queue: Queue = Depends(get_queue)) -> None:
    """FastAPI dependency: allow the request, or raise 429 if the IP is over budget.

    Attached to the sensitive auth routes. Reuses the shared Redis fixed-window
    counter keyed on the client IP under the "auth" action.

    Raises:
        APIError(429, RATE_LIMITED): with a `Retry-After` header, when over the limit.
    """
    ip = _client_ip(request)
    if not await queue.within_rate_limit(ip, "auth", settings.auth_rate_limit_per_minute):
        retry_after = 60 - int(time.time()) % 60
        raise APIError(429, "RATE_LIMITED", "Too many attempts. Try again later.",
                       headers={"Retry-After": str(retry_after)})
