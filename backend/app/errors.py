"""One error type + one handler, so every failure leaves the API the same shape.

Every 4xx/5xx we raise is an `APIError`, and a single registered handler turns it
into `{"detail": <human message>, "code": <STABLE_CODE>}`. The split matters:
`detail` is for humans (may change wording freely), while `code` is a stable
machine identifier the frontend switches on (e.g. `SUBMISSION_IN_FLIGHT`,
`EMAIL_NOT_VERIFIED`) — so translating or rewording a message never breaks
client logic. (DESIGN.md §4)
"""
from fastapi import Request
from fastapi.responses import JSONResponse


class APIError(Exception):
    """A request failure with an HTTP status, a stable `code`, and a message.

    Raise this anywhere in a request (services included) instead of returning
    error responses by hand; the handler below renders it uniformly.

    Args:
        status_code: HTTP status (e.g. 401, 403, 409, 429).
        code: stable SCREAMING_SNAKE identifier the frontend can branch on.
        detail: human-readable message (safe to reword/localize).
        headers: optional extra response headers, e.g. ``{"Retry-After": "30"}``
            on a 429.
    """

    def __init__(self, status_code: int, code: str, detail: str, headers: dict | None = None):
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.headers = headers


async def api_error_handler(_request: Request, exc: APIError) -> JSONResponse:
    """Render any raised `APIError` as the canonical JSON error body.

    Registered on the app via `add_exception_handler` (see `app/main.py`), so a
    raised `APIError` unwinds the stack and lands here regardless of where it came
    from — routers, dependencies, or deep in a service.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code},
        headers=exc.headers,
    )
