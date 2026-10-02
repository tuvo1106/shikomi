"""The one module that imports the ozymandias SDK (`ozy`); everything else imports from here.

Mental model: telemetry is a *side channel*. It must never change what the app does, so
every helper here is fire-and-forget and inert until `OZY_AGENT_HOST` is set — the SDK
creates no socket, no thread and no hook while disabled, and its calls are no-ops. That is
what lets the same code run under pytest, in CI, and on a laptop with no agent.

Keeping the import in one file is the same seam discipline the rest of the repo follows for
Redis (`app/queue.py`) and the sandbox (`worker/runner.py`): swapping the telemetry vendor
touches one module, not forty call sites.

Metric naming follows the ozymandias catalog: `lower.dotted.names`, durations are
distributions in **milliseconds**, counts are counters, and tags come from bounded sets only.
Never put a user id, an email, a submission id or a raw path in a tag — those belong in logs.
"""
import asyncio
import functools
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from typing import Any

import ozy
from ozy import statsd
from ozy.integrations.asgi import MetricsMiddleware

from app.config import get_settings

__all__ = ["MetricsMiddleware", "count", "enabled", "gauge", "init_telemetry", "job", "observe"]


def init_telemetry(service: str) -> None:
    """Enable the SDK for this process, tagging everything as `service`.

    Called once from the API's `create_app()` and from each worker's `on_startup`. Reads
    `OZY_AGENT_HOST` etc. through `Settings` (so a local `.env` works too), then hands the
    values to `ozy.init`, which never raises.
    """
    settings = get_settings()
    ozy.init(service=service, env=settings.ozy_env, version=settings.ozy_version,
             agent_host=settings.ozy_agent_host or "")


def enabled() -> bool:
    """True when metrics are actually being sent (an agent host is configured)."""
    return statsd.enabled


def _tags(tags: dict[str, Any]) -> list[str]:
    return [f"{k}:{v}" for k, v in tags.items() if v is not None]


def count(name: str, value: int = 1, **tags: Any) -> None:
    """Add `value` (default one) to counter `name`. Tags are keyword arguments; `None` values
    are dropped."""
    statsd.increment(name, value, tags=_tags(tags))


def gauge(name: str, value: float, **tags: Any) -> None:
    """Set gauge `name` to `value`."""
    statsd.gauge(name, value, tags=_tags(tags))


@contextmanager
def observe(prefix: str, **tags: Any):
    """Time a block and record `<prefix>.duration` (ms, distribution) and `<prefix>.count`.

    Yields a mutable object whose `outcome` the caller may set (`"ok"` by default). If the
    block raises and the caller has not set one, it becomes `"error"`, or `"cancelled"` for
    a cancellation, because arq cancels a job at its timeout and that is not a failure of the
    job's own logic. The exception always propagates; observing never swallows anything.
    """
    obs = _Observation()
    started = time.perf_counter()
    try:
        yield obs
    except BaseException as exc:
        if obs.outcome is None:
            obs.outcome = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
        raise
    finally:
        all_tags = _tags({**tags, "outcome": obs.outcome or "ok"})
        statsd.distribution(f"{prefix}.duration", (time.perf_counter() - started) * 1000.0,
                            tags=all_tags)
        statsd.increment(f"{prefix}.count", tags=all_tags)


class _Observation:
    """What `observe` yields: set `.outcome` to override the default."""

    outcome: str | None = None


def job(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Wrap an arq job function so each run records `arq.job.duration` / `arq.job.count`.

    Tagged with the function name and the outcome. `functools.wraps` keeps `__name__`, which
    arq uses as the job's registered name, so enqueueing by name still finds it. Only the
    registration in `WorkerSettings.functions` is wrapped, so tests calling the job function
    directly never touch telemetry.
    """
    @functools.wraps(fn)
    async def wrapper(ctx, *args, **kwargs):
        with observe("arq.job", function=fn.__name__):
            return await fn(ctx, *args, **kwargs)

    return wrapper
