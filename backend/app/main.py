"""Where the API is assembled: the FastAPI app factory (DESIGN.md §7).

`create_app()` wires the pieces together — the uniform error handler, dev-only
CORS, and every router under `/api/v1`. Using a factory (rather than mutating a
module-level `app`) keeps construction in one testable place. The module-level
`app = create_app()` at the bottom is what `uvicorn app.main:app` serves.
"""
import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import telemetry
from app.config import get_settings
from app.errors import APIError, api_error_handler
from app.queue import get_queue
from app.routers import auth, health, problems, submissions

settings = get_settings()
API_PREFIX = "/api/v1"
# How often the API reports queue depth. KEDA polls `/internal/queue-depth` for scaling; this
# is the same number for the dashboard, so it only needs to be as fresh as one chart bucket.
QUEUE_DEPTH_INTERVAL_SECONDS = 10

logger = logging.getLogger("app")


async def _report_queue_depth() -> None:
    """Gauge the judge and accounts queue depths forever (cancelled at shutdown).

    A telemetry failure — Redis down, a bad reply — is logged and skipped; the loop must
    outlive any single bad read, and it must never take the API down with it.
    """
    while True:
        try:
            queue = await get_queue()
            telemetry.gauge("arq.queue.depth", await queue.queue_depth(), queue="judge")
            accounts_depth, accounts_age = await queue.accounts_queue_stats()
            telemetry.gauge("arq.queue.depth", accounts_depth, queue="accounts")
            if accounts_age is not None:
                telemetry.gauge("arq.queue.oldest_age_seconds", accounts_age, queue="accounts")
        except Exception:
            logger.debug("queue depth report failed", exc_info=True)
        await asyncio.sleep(QUEUE_DEPTH_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the queue-depth reporter when telemetry is on; stop it cleanly on shutdown."""
    task = asyncio.create_task(_report_queue_depth()) if telemetry.enabled() else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def create_app() -> FastAPI:
    """Build and return the configured FastAPI application."""
    telemetry.init_telemetry("shikomi-api")
    app = FastAPI(title="Shikomi API", version="0.1.0", lifespan=lifespan)
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

    # Added last, so it is the outermost middleware added here. The probes are polled by the
    # orchestrator and the queue-depth endpoints by KEDA; none says anything about users.
    app.add_middleware(telemetry.MetricsMiddleware, exclude_paths=[
        f"{API_PREFIX}{path}" for path in (
            "/livez", "/healthz", "/internal/queue-depth", "/internal/accounts-queue-depth")])
    return app


# The ASGI app object uvicorn imports and serves.
app = create_app()
