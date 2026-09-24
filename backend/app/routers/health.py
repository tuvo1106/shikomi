"""Liveness/readiness probe for load balancers and uptime checks (DESIGN.md §9)."""
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import APIError
from app.queue import Queue, get_queue

router = APIRouter(tags=["health"])


@router.get("/livez")
async def livez():
    """Liveness: 200 whenever the process can answer at all, with no dependency check.

    Kubernetes *restarts* a pod whose liveness probe fails, so this must only answer "would a
    restart help?" A Postgres or Redis outage does not: restarting healthy api pods just drops
    in-flight requests and puts the whole fleet into a crash loop for as long as the
    dependency is down. That is what the deep `/healthz` is for, on the *readiness* probe,
    which only stops routing traffic to the pod.
    """
    return {"status": "alive"}


@router.get("/healthz")
async def healthz(session: AsyncSession = Depends(get_session),
                  queue: Queue = Depends(get_queue)):
    """Readiness: return 200 only if both critical dependencies (Postgres + Redis) respond.

    A *deep* health check (actually pings each backing service) rather than a
    bare "process is up", so an orchestrator pulls the instance from rotation when
    a dependency is down instead of routing traffic into 500s.
    """
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        raise APIError(503, "DB_UNAVAILABLE", "Database is unavailable.")
    try:
        await queue.ping()
    except Exception:
        raise APIError(503, "REDIS_UNAVAILABLE", "Queue is unavailable.")
    return {"status": "ok"}


@router.get("/internal/queue-depth")
async def queue_depth(queue: Queue = Depends(get_queue)):
    """Judge-queue backlog (pending arq jobs). Unauthenticated and non-sensitive —
    just a count — so KEDA's metrics-api scaler can poll it to autoscale the worker
    (DESIGN.md §5.6). Not reached through Caddy in practice; KEDA hits the api
    Service directly in-cluster.
    """
    return {"depth": await queue.queue_depth()}


@router.get("/internal/accounts-queue-depth")
async def accounts_queue_depth(queue: Queue = Depends(get_queue)):
    """Account-email backlog (pending jobs on `arq:accounts`), for monitoring/alerting.

    Same shape and trust model as `/internal/queue-depth`, but a *separate* metric: it is
    not wired to KEDA (mail must never scale the judge worker), and a depth that keeps
    growing means the accounts worker is down or stuck (a hung SMTP server), which the
    API's own health check can't see. `oldest_age_seconds` (null when empty) is the better
    alarm signal for an external monitor: a burst spikes depth, but a job that has waited
    minutes means nothing is consuming. The in-app watchdog alerts on the same number.
    """
    depth, age = await queue.accounts_queue_stats()
    return {"depth": depth, "oldest_age_seconds": None if age is None else round(age, 1)}
