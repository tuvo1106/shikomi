"""The database/Redis half of the once-a-minute sweep (DESIGN.md §5.7).

Three jobs: fail submissions stuck in `pending`/`running` past their budget, re-enqueue
`pending` submissions whose judge job was lost (so a lost job is retried, not failed), and
prune expired tokens. Deliberately free of
any sandbox-runner import, so the lean accounts worker (api image, no Docker/k8s client) can
run it and it keeps running when the judge worker is scaled to zero.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, or_, select, update

from app.db import SessionLocal
from app.models import EmailToken, RefreshToken, Submission
from app.queue import Queue

logger = logging.getLogger("sweeper")

STALE_AFTER = timedelta(minutes=5)
TOKEN_RETENTION = timedelta(days=60)

# A `pending` row this old is worth checking for a missing job. Well past the microseconds
# between the API's commit and its enqueue, and short enough that a lost job is recovered long
# before the STALE_AFTER budget fails the row.
REENQUEUE_AFTER = timedelta(seconds=30)
# Rows checked per sweep, oldest first. The existence check is one pipelined Redis round trip for
# the whole batch (`Queue.missing_judge_jobs`), so this can be generous: a backlog of legitimately
# queued rows must not crowd a genuinely lost job out of the window.
REENQUEUE_BATCH = 1000
# Concurrent enqueues. Each is its own WATCH/MULTI exchange on a pooled connection, so an
# uncapped burst after a Redis wipe could open a connection per missing job.
REENQUEUE_CONCURRENCY = 20


async def sweep_stale(ctx) -> None:
    """Cron (every minute): fail stuck submissions (Runs included), re-enqueue lost jobs,
    prune refresh tokens past retention and expired email tokens (§5.7).

    Runs on the **accounts worker**, not the judge worker. It needs only Postgres and Redis,
    and KEDA can scale the judge worker to zero; if it ran there, a submission lost at the
    wrong moment (job never enqueued, or the worker gone) would sit `pending` with nothing
    running to fail it. Orphan-sandbox reaping needs the sandbox runner, so it stays with the
    judge worker (`worker/judge.py::reap_orphans`). With more than one accounts replica the sweep is
    still safe: every step is an idempotent conditional write.

    Keys off `updated_at`, not `created_at`: `judge_submission` bumps
    `updated_at` when it flips a row to "running", so the 5-minute budget
    below effectively restarts from when the worker actually picked the job
    up — decoupling it from how long the row sat queued first. A row still
    "pending" has never been updated since creation, so `updated_at ==
    created_at` there and a genuinely wedged pending submission is still
    caught; keying off `created_at` instead would count queue-wait time
    against the same budget as run time, so a submission that waited even a
    couple of minutes in a backlog before starting could be killed mid-judge
    despite barely having run.

    Runs (`is_run`) aren't excluded here (unlike the history/stats queries,
    which exclude them because they're not part of a user's submission
    record) — a Run stuck `pending`/`running` forever (the worker crashed, the
    job was lost) would otherwise poll forever client-side with nothing to
    ever resolve it, since Runs have no history row a user could otherwise
    notice and retry from.
    """
    redis = ctx["redis"]
    now = datetime.now(timezone.utc)

    async with SessionLocal() as session:
        # One conditional UPDATE ... RETURNING: no full rows (the code column) are loaded,
        # and the status guard makes a race with a verdict landing a no-op, not an overwrite.
        # Served by the partial index ix_submissions_unfinished_updated.
        stale = (await session.execute(
            update(Submission)
            .where(Submission.status.in_(("pending", "running")),
                   Submission.updated_at < func.now() - STALE_AFTER)
            .values(status="judge_error")
            .returning(Submission.id, Submission.user_id, Submission.problem_id))).all()
        await session.commit()
        if stale:
            logger.warning("swept %d stale submission(s) to judge_error", len(stale))
        # After the commit, so the verdict is durable even if Redis is down. A Run never took
        # the inflight lock (only "submit" does), and a lock that now belongs to a newer
        # submission isn't ours to free: the release only deletes it when this stale
        # submission is still the owner, so both cases are no-ops. A Redis error just leaves
        # the lock to its TTL; it must not abort the rest of the sweep.
        for sid, user_id, problem_id in stale:
            try:
                await Queue(redis).release_inflight(user_id, problem_id, str(sid))
            except Exception:
                logger.exception("could not release the in-flight lock for %s", sid)

        await _requeue_lost_jobs(session, redis)

        cutoff = now - TOKEN_RETENTION
        await session.execute(
            delete(RefreshToken).where(
                or_(RefreshToken.expires_at < cutoff, RefreshToken.revoked_at < cutoff)))
        # Email tokens have no reuse-detection value once expired (unlike refresh
        # tokens, which are kept past expiry for a retention window to catch reuse
        # of a stolen/rotated token) — every verify/reset issues one, so without
        # this they'd accumulate forever.
        await session.execute(delete(EmailToken).where(EmailToken.expires_at < now))
        await session.commit()


async def _requeue_lost_jobs(session, redis) -> None:
    """Re-enqueue `pending` rows whose judge job was never queued.

    `create_submission` commits the row and *then* enqueues, so a crash in between leaves a
    `pending` row with no job: it would only ever be failed as `judge_error`, for a submission
    that was never attempted. Enqueueing again is safe for any pending row, because the job id
    is the submission id and arq treats a duplicate id as a no-op (`Queue.enqueue_judge`): a row
    whose job exists (still queued behind a backlog, or running) is left untouched, and only a
    genuinely missing job is created. Runs after the stale pass, so rows past their budget are
    already `judge_error` and are never resurrected.
    """
    # Only the two columns needed, and the read transaction is ended before any Redis call
    # so no pooled connection sits idle-in-transaction during the round trips.
    stmt = (select(Submission.id, Submission.is_run)
            .where(Submission.status == "pending",
                   Submission.updated_at < func.now() - REENQUEUE_AFTER)
            .order_by(Submission.created_at).limit(REENQUEUE_BATCH))
    rows = (await session.execute(stmt)).all()
    await session.commit()
    if not rows:
        return
    queue = Queue(redis)
    is_run = {str(sid): run for sid, run in rows}
    try:
        missing = await queue.missing_judge_jobs(list(is_run))
    except Exception:
        # A Redis hiccup must not abort the rest of the sweep (token pruning, the final commit):
        # the rows stay pending and are checked again next minute.
        logger.exception("could not check pending submissions for lost jobs; will retry next pass")
        return

    # Normally zero or a handful; after a Redis restart without persistence it can be the whole
    # batch, so the enqueues run concurrently, capped so they can't exhaust Redis connections.
    limit = asyncio.Semaphore(REENQUEUE_CONCURRENCY)

    async def enqueue(sid):
        async with limit:
            return await queue.enqueue_judge(sid, "run" if is_run[sid] else "submit")

    # return_exceptions: one failed enqueue mustn't hide the ones that succeeded; the failures
    # stay pending and are retried next pass.
    results = await asyncio.gather(*(enqueue(sid) for sid in missing), return_exceptions=True)
    failed = [r for r in results if isinstance(r, BaseException)]
    requeued = sum(1 for r in results if r is True)
    if requeued:
        logger.warning("re-enqueued %d pending submission(s) that had no job", requeued)
    if failed:
        logger.error("could not re-enqueue %d pending submission(s); will retry next pass: %r",
                     len(failed), failed[0])
