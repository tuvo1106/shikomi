"""Everything the API does with Redis, in one place (DESIGN.md §5.1, §4.3).

Redis serves three roles here, all cheap and atomic:

1. **Job queue** — the API `enqueue`s a judge job and returns immediately (202);
   a separate worker process picks it up. Judging runs a Docker sandbox for
   seconds, far too long to block an HTTP request, so it must be out-of-band.
2. **In-flight lock** — "one running submission per user per problem", so a user
   can't spam the judge by firing many submits at once.
3. **Rate-limit counters** — per-user fixed-window counters for submit/run.

It's all wrapped in the `Queue` class (rather than calling Redis inline) so tests
can inject a fake via the `get_queue` dependency and run with no Redis at all.
"""
import time

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.constants import default_queue_name, job_key_prefix, result_key_prefix

from app.config import get_settings

settings = get_settings()

JUDGE_TASK = "judge_submission"
EMAIL_TASK = "send_account_email"
# Account mail rides its own arq queue (served by `worker.main.AccountsWorkerSettings`),
# not arq's default one that the judge worker drains. A backlog of mail (a hung SMTP
# server, a burst of resets) then can't delay judging, and a judging backlog can't delay
# verify/reset links. The worker's `queue_name` must match this or jobs are never picked up.
ACCOUNTS_QUEUE = "arq:accounts"
INFLIGHT_TTL_SECONDS = 120
PERCENTILE_CACHE_TTL_SECONDS = 30


# Delete the key only if it still holds our token. GET-then-DEL from Python would let the key
# expire and be re-taken between the two calls; a script runs atomically inside Redis.
_RELEASE_IF_OWNER = """
local holder = redis.call('GET', KEYS[1])
if holder == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

# INCR and (re)arm the expiry in one atomic step. Two separate calls can be split by a crash,
# leaving a counter with no TTL that never ages out. `TTL == -1` (key exists, no expiry) also
# heals a key already leaked that way, instead of only arming on the first hit.
_INCR_WITH_TTL = """
local count = redis.call('INCR', KEYS[1])
if count == 1 or redis.call('TTL', KEYS[1]) == -1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


def inflight_key(user_id, problem_id) -> str:
    """Redis key for the per-(user, problem) in-flight submission lock."""
    return f"inflight:{user_id}:{problem_id}"


class Queue:
    """The API's view of Redis: enqueue judge jobs, hold locks, count requests.

    Everything that touches Redis goes through this class so a test can swap in a
    fake (see `conftest.FakeQueue`) via the `get_queue` dependency without a real
    Redis. The `redis` handle is an arq pool, which is also a normal Redis client.
    """

    def __init__(self, redis: ArqRedis):
        self.redis = redis

    async def enqueue_judge(self, submission_id: str, mode: str) -> bool:
        """Hand a submission to the worker pool to be judged out-of-band.

        We only pass the id (not the code) — the worker re-reads the row — so the
        job payload stays tiny and there's a single source of truth.

        The submission id is also the arq job id, which makes this idempotent: arq refuses to
        enqueue a job whose id already exists (queued, running, or finished with its result
        still kept) and returns None. That is what lets the sweeper safely re-enqueue a row
        whose job was never queued (the API died between the commit and the enqueue) without
        risking a second job for a row that does have one. The queue is named explicitly
        because the sweeper calls this from the accounts worker, whose pool defaults to
        `arq:accounts`.

        Returns:
            True if a job was enqueued, False if one already existed for this submission.
        """
        job = await self.redis.enqueue_job(
            JUDGE_TASK, submission_id, mode, _job_id=submission_id,
            _queue_name=default_queue_name)
        return job is not None

    async def missing_judge_jobs(self, submission_ids: list[str]) -> list[str]:
        """The ids among `submission_ids` that have no arq job (queued, running, or result kept).

        One pipelined round trip for the whole list, using the same two keys arq's own duplicate
        check reads (`enqueue_job` refuses an id when either exists). That lets the sweeper look
        at many pending rows cheaply and only call `enqueue_judge` for the few that are genuinely
        missing a job, instead of one enqueue attempt per row.
        """
        if not submission_ids:
            return []
        async with self.redis.pipeline(transaction=False) as pipe:
            for sid in submission_ids:
                pipe.exists(job_key_prefix + sid, result_key_prefix + sid)
            counts = await pipe.execute()
        return [sid for sid, count in zip(submission_ids, counts) if not count]

    async def enqueue_email(self, kind: str, email: str) -> None:
        """Hand an account email (verify / existing-account / reset) to the accounts worker.

        The payload is only `(kind, email)`: the worker looks the user up, mints
        the single-use token and mails the link itself, so the raw token — the
        only secret in the message — is never written to Redis. See
        `worker/accounts.py:send_account_email`. Goes to `ACCOUNTS_QUEUE`, not the judge queue.
        """
        await self.redis.enqueue_job(EMAIL_TASK, kind, email, _queue_name=ACCOUNTS_QUEUE)

    async def acquire_inflight(self, user_id, problem_id, owner: str) -> bool:
        """Try to take the "one submission at a time per problem" lock, as `owner`.

        `owner` is the submission's id, stored as the lock's value so only that submission can
        release it (`release_inflight`). Without it the lock was a constant `"1"` that anyone
        could delete: if a job outlived the TTL, a second submit took the lock, the first job's
        cleanup then deleted *the second's* lock, and a third submit slipped in.

        Implemented as an atomic SET-if-Not-eXists with an expiry (`nx=True,
        ex=TTL`): the first caller writes the key and wins; concurrent callers see
        it exists and lose. The TTL is a safety net so a crashed worker that never
        releases the lock can't wedge the user forever.

        Returns:
            True if the lock was acquired, False if one is already held.
        """
        return bool(await self.redis.set(
            inflight_key(user_id, problem_id), owner, nx=True, ex=INFLIGHT_TTL_SECONDS))

    async def release_inflight(self, user_id, problem_id, owner: str) -> bool:
        """Release the lock only if `owner` still holds it (compare-and-delete).

        Returns True if it was ours and is now gone; False if it had expired or been re-taken
        by another submission, in which case it is left alone.
        """
        return bool(await self.redis.eval(
            _RELEASE_IF_OWNER, 1, inflight_key(user_id, problem_id), owner))

    async def within_rate_limit(self, user_id, action: str, limit: int) -> bool:
        """Fixed-window per-user rate limit; True while under `limit` this minute.

        The key embeds the current 60s bucket, so counts reset each minute simply
        by moving to a new key. The increment and its expiry happen in one atomic script (no lost
        updates under concurrency, and no key left without a TTL if we crash in between). Fixed
        windows can allow a short burst across a boundary — fine here, and simpler than a
        sliding window.
        """
        bucket = int(time.time()) // 60
        key = f"ratelimit:{action}:{user_id}:{bucket}"
        count = await self.redis.eval(_INCR_WITH_TTL, 1, key, 60)
        return count <= limit

    # --- login lockout (per-account brute-force defense, §4.1) --------------
    # Failure counts and locks live in Redis (not process memory) so they survive
    # restarts and are shared across API replicas — an attacker can't reset the
    # counter by waiting for a redeploy or spreading attempts across instances.

    async def incr_login_failures(self, email: str, window_seconds: int) -> int:
        """Count one failed login for `email`; return the running total in the window.

        The counter self-expires after `window_seconds` (armed atomically with the increment),
        so a few scattered typos age out instead of accumulating forever.
        """
        key = f"authfail:{email.lower()}"
        return await self.redis.eval(_INCR_WITH_TTL, 1, key, window_seconds)

    async def set_login_lock(self, email: str, seconds: int) -> None:
        """Lock `email` out of login for `seconds` (a key that auto-expires)."""
        await self.redis.set(f"authlock:{email.lower()}", "1", ex=seconds)

    async def get_login_lock(self, email: str) -> int:
        """Seconds remaining on `email`'s lock, or 0 if not locked."""
        ttl = await self.redis.ttl(f"authlock:{email.lower()}")
        return ttl if ttl and ttl > 0 else 0

    async def clear_login_failures(self, email: str) -> None:
        """Reset the failure counter and lock (called on a successful login)."""
        await self.redis.delete(f"authfail:{email.lower()}", f"authlock:{email.lower()}")

    # --- runtime-percentile cache (§4.3 "beats X%") ---------------------------
    # `submission_service.runtime_percentile` runs two COUNT(*) scans; once a
    # submission is accepted its runtime never changes, so repeated polls/views
    # of it (or another submission with an identical runtime) reuse the result
    # for a short TTL instead of re-scanning every time.

    async def get_cached_percentile(self, problem_id, runtime_ms) -> float | None:
        """Cached percentile for (problem_id, runtime_ms), or None on a miss."""
        raw = await self.redis.get(f"percentile:{problem_id}:{runtime_ms}")
        return float(raw) if raw is not None else None

    async def set_cached_percentile(self, problem_id, runtime_ms, value: float) -> None:
        """Cache a freshly computed percentile (see `get_cached_percentile`)."""
        await self.redis.set(f"percentile:{problem_id}:{runtime_ms}", value,
                             ex=PERCENTILE_CACHE_TTL_SECONDS)

    async def ping(self) -> bool:
        """Liveness check — used by the health endpoint to confirm Redis is up."""
        return bool(await self.redis.ping())

    async def queue_depth(self) -> int:
        """Number of judge jobs not yet finished: waiting *plus* currently running.

        arq stores its queue as a Redis *sorted set* (`arq:queue`, scored by
        run-at time), and a job stays in it while it runs (a separate
        `arq:in-progress:` key marks the claim); it's only removed on finish. So
        the cardinality is unfinished work, not just unclaimed work — which suits
        autoscaling: a worker mid-judge still counts, so it isn't scaled to zero
        under a running job. Exposed via a
        metrics endpoint so KEDA can autoscale the worker on backlog (DESIGN.md
        §5.6) — the natural signal for a queue consumer, and what lets it scale to
        zero when idle.
        """
        return int(await self.redis.zcard(default_queue_name))

    async def accounts_queue_stats(self) -> tuple[int, float | None]:
        """`(depth, oldest_age_seconds)` of the account-email queue, from one snapshot.

        Age, not depth, is the signal that the accounts worker is stuck: a burst of resets
        makes depth spike and drain, but a job that has waited minutes means nothing is
        consuming. arq scores each job with its enqueue time in ms, so the lowest score is
        the oldest; a retry deferred into the future scores ahead of now, hence the clamp.
        `oldest_age_seconds` is None when the queue is empty. arq only removes a job from the
        set when it *finishes*, so a job stuck mid-run also ages here, which is what we want:
        a worker wedged on one job is exactly the outage to report (a hung SMTP call is
        killed at the 60s job timeout, far below the alert threshold).

        Both numbers come from one MULTI/EXEC, so a monitor can't see a depth that
        contradicts the age (e.g. depth 0 with an age) because the queue moved between two
        separate reads.
        """
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.zcard(ACCOUNTS_QUEUE)
            pipe.zrange(ACCOUNTS_QUEUE, 0, 0, withscores=True)
            depth, oldest = await pipe.execute()
        age = max(0.0, time.time() - oldest[0][1] / 1000) if oldest else None
        return int(depth), age

    async def claim_alert(self, name: str, cooldown_seconds: int) -> bool:
        """Take the right to send alert `name`, at most once per cooldown (SET NX EX).

        The watchdog runs every minute, so without this a stuck queue would page every
        minute. True means this caller should send; the key expires on its own, so a
        problem that outlasts the cooldown re-alerts.
        """
        return bool(await self.redis.set(f"alert:{name}", "1", nx=True, ex=cooldown_seconds))

    async def release_alert(self, name: str) -> None:
        """Drop alert `name`'s cooldown so the next problem alerts immediately.

        Called when the condition clears (so a second outage inside the cooldown window is
        reported, not swallowed), and when an alert could not actually be delivered (so the
        next check tries again instead of staying silent for the whole cooldown).
        """
        await self.redis.delete(f"alert:{name}")



# One shared arq pool for the process, created lazily on first use.
_pool: ArqRedis | None = None


async def get_pool() -> ArqRedis:
    """Return the process-wide arq Redis pool, opening it on first call."""
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _pool


async def get_queue() -> Queue:
    """FastAPI dependency yielding a `Queue`. Overridden in tests with a fake."""
    return Queue(await get_pool())
