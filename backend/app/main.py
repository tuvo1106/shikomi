"""Where the API is assembled: the FastAPI app factory (DESIGN.md §7).

`create_app()` wires the pieces together — the uniform error handler, dev-only
CORS, and every router under `/api/v1`. Using a factory (rather than mutating a
module-level `app`) keeps construction in one testable place. The module-level
`app = create_app()` at the bottom is what `uvicorn app.main:app` serves.
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.errors import APIError, api_error_handler
from app.routers import auth, health, problems, submissions

settings = get_settings()
API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    """Build and return the configured FastAPI application."""
    app = FastAPI(title="Shikomi API", version="0.1.0")
    # Make every raised APIError render as our canonical {detail, code} body.
    app.add_exception_handler(APIError, api_error_handler)

    # In prod the SPA is served same-origin behind Caddy, so the browser never
    # makes a cross-origin call and CORS is unnecessary. In dev the SPA runs on
    # :5173 and the API on :8000 (cross-origin), so we allow those origins —
    # with credentials, since auth rides on the refresh cookie. (DESIGN.md §8)
    if settings.env != "prod":
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    for module in (health, auth, problems, submissions):
        app.include_router(module.router, prefix=API_PREFIX)
    return app


# The ASGI app object uvicorn imports and serves.
app = create_app()
