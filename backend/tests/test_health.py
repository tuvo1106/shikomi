"""Health check tests, including the DB-failure branch (DESIGN.md §9)."""
from app.db import get_session
from app.main import app

HEALTHZ = "/api/v1/healthz"


async def test_healthz_ok(client):
    r = await client.get(HEALTHZ)
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_healthz_reports_db_failure(client):
    class BrokenSession:
        async def execute(self, *_args, **_kwargs):
            raise RuntimeError("database is down")

    async def broken_session():
        yield BrokenSession()

    app.dependency_overrides[get_session] = broken_session
    r = await client.get(HEALTHZ)
    assert r.status_code == 503
    assert r.json()["code"] == "DB_UNAVAILABLE"


async def test_healthz_reports_redis_failure(client, queue):
    queue.fail_ping = True
    r = await client.get(HEALTHZ)
    assert r.status_code == 503
    assert r.json()["code"] == "REDIS_UNAVAILABLE"


async def test_queue_depth_reports_backlog(client, queue):
    # The metric KEDA autoscales the worker on (DESIGN.md §5.6). Unauthenticated.
    queue.depth = 7
    r = await client.get("/api/v1/internal/queue-depth")
    assert r.status_code == 200
    assert r.json() == {"depth": 7}


async def test_accounts_queue_depth_reports_mail_backlog_separately(client, queue):
    # A monitoring metric, distinct from the KEDA one: mail backlog must not scale judging.
    queue.depth, queue.accounts_depth, queue.accounts_oldest_age = 7, 3, 12.34
    r = await client.get("/api/v1/internal/accounts-queue-depth")
    assert r.status_code == 200
    assert r.json() == {"depth": 3, "oldest_age_seconds": 12.3}
    assert (await client.get("/api/v1/internal/queue-depth")).json() == {"depth": 7}


async def test_the_real_queue_counts_each_backlog_from_its_own_key_and_age_from_the_oldest_job():
    """The real `Queue` reads `arq:queue` for judging and `arq:accounts` for mail, and takes
    depth and age from one MULTI/EXEC. arq scores a job with its enqueue time in ms, so the
    lowest score is the oldest; a retry deferred into the future clamps to 0, never negative."""
    import time

    from app.queue import ACCOUNTS_QUEUE, Queue

    class Pipe:
        def __init__(self, oldest):
            self.oldest, self.calls = oldest, []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def zcard(self, key):
            self.calls.append(("zcard", key))

        def zrange(self, key, start, stop, withscores=False):
            self.calls.append(("zrange", key, start, stop, withscores))

        async def execute(self):
            return [len(self.oldest), self.oldest]

    class Redis:
        def __init__(self):
            self.oldest = []
            self.last = None

        async def zcard(self, key):
            return {"arq:queue": 5}[key]

        def pipeline(self, transaction=True):
            assert transaction is True  # one MULTI/EXEC, so depth and age agree
            self.last = Pipe(self.oldest)
            return self.last

    r = Redis()
    q = Queue(r)
    assert await q.queue_depth() == 5

    assert await q.accounts_queue_stats() == (0, None)
    assert r.last.calls == [("zcard", ACCOUNTS_QUEUE), ("zrange", ACCOUNTS_QUEUE, 0, 0, True)]

    r.oldest = [(b"job", (time.time() - 90) * 1000)]
    depth, age = await q.accounts_queue_stats()
    assert depth == 1 and 89 < age < 92

    r.oldest = [(b"job", (time.time() + 60) * 1000)]  # a deferred retry
    assert (await q.accounts_queue_stats())[1] == 0.0


async def test_livez_is_shallow_and_stays_up_when_dependencies_are_down(client, queue):
    """Liveness must answer "would a restart help?": a Redis (or Postgres) outage must not
    make Kubernetes restart healthy api pods, which is what a deep liveness check would do."""
    queue.fail_ping = True
    assert (await client.get("/api/v1/healthz")).status_code == 503   # readiness: out of rotation
    r = await client.get("/api/v1/livez")
    assert r.status_code == 200 and r.json() == {"status": "alive"}   # liveness: keep the pod


def test_the_helm_chart_uses_livez_for_liveness_and_healthz_for_readiness():
    """The chart wiring is the whole point of /livez, so pin it."""
    import pathlib
    chart = (pathlib.Path(__file__).resolve().parents[2]
             / "deploy/helm/shikomi/templates/api.yaml").read_text()
    live = chart[chart.index("livenessProbe"):]
    ready = chart[chart.index("readinessProbe"):chart.index("livenessProbe")]
    assert "/api/v1/livez" in live and "/api/v1/healthz" not in live
    assert "/api/v1/healthz" in ready
