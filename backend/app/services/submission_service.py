"""Submission lifecycle + the stats derived from submissions (§4.3, §5.7).

Creating a submission is a careful little dance because judging is asynchronous:
we rate-limit, take the in-flight lock, write the row, *then* enqueue — and if
either the write or the enqueue fails we release the lock (the write failure also
has no row to mark `judge_error`, since it never committed), so the system never
has a "pending forever" row or a stuck lock (the ordering is the whole point of
§5.7). The rest of the module is reads: ownership-checked fetches, plus the
runtime percentile/distribution that power the "you beat X%" feedback.
"""
import time
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import APIError
from app.models import Problem, Submission, User
from app.queue import Queue

settings = get_settings()

_RATE_LIMITS = {
    "submit": settings.submit_rate_limit_per_minute,
    "run": settings.run_rate_limit_per_minute,
}


async def create_submission(session: AsyncSession, queue: Queue, user: User,
                            problem_id: uuid.UUID, code: str, *, mode: str) -> Submission:
    """Accept a Run/Submit attempt and enqueue it for async judging.

    `mode` is "submit" (all cases, rate-limited, one-in-flight, saved to history)
    or "run" (sample cases only, no in-flight lock). Guards run cheapest-first so
    abuse is rejected before doing real work: size cap → rate limit → problem
    exists → in-flight lock → write row → enqueue.

    The row is written *before* the enqueue (§5.7): if enqueue then fails we mark
    the row `judge_error` and release the lock, so we never accept work we can't
    queue and never leave a stuck lock. If the write itself fails (the commit
    raises), the lock is released the same way — otherwise the user would be
    wedged out of resubmitting for the full in-flight TTL over a transient DB
    error, with no row for them to even see went wrong.

    Returns:
        The created `Submission` (status "pending"); the client polls it.

    Raises:
        APIError: 413 CODE_TOO_LARGE, 429 RATE_LIMITED, 404 NOT_FOUND (unpublished/
            missing), 429 SUBMISSION_IN_FLIGHT, or 503 QUEUE_UNAVAILABLE.
    """
    if len(code.encode()) > settings.max_code_bytes:
        raise APIError(413, "CODE_TOO_LARGE", "Submission code exceeds the size limit.")

    # Per-user rate limit (checked before any DB/lock work so hammering is cheap to reject).
    try:
        within = await queue.within_rate_limit(user.id, mode, _RATE_LIMITS[mode])
    except Exception:
        raise APIError(503, "QUEUE_UNAVAILABLE", "Judging is temporarily unavailable.")
    if not within:
        retry_after = 60 - int(time.time()) % 60
        raise APIError(429, "RATE_LIMITED", "Too many submissions; slow down.",
                       headers={"Retry-After": str(retry_after)})

    problem = await session.get(Problem, problem_id)
    if problem is None or not problem.is_published:
        raise APIError(404, "NOT_FOUND", "Problem not found.")

    # The id is chosen up front so it can be the lock's owner token: the lock is taken before
    # the row exists, and every later release (here, the worker, the sweeper) knows this id.
    sub_id = uuid.uuid4()
    # Read now: a failed commit below rolls the session back and expires `user`, and a lazy
    # load of `user.id` in the error path would raise inside the lock-release guard.
    user_id = user.id

    # One in-flight submission per user per problem (submit only; run is unthrottled).
    if mode == "submit":
        try:
            acquired = await queue.acquire_inflight(user_id, problem_id, str(sub_id))
        except Exception:
            raise APIError(503, "QUEUE_UNAVAILABLE", "Judging is temporarily unavailable.")
        if not acquired:
            raise APIError(429, "SUBMISSION_IN_FLIGHT",
                           "You already have a submission running for this problem.")

    sub = Submission(id=sub_id, user_id=user_id, problem_id=problem_id, code=code,
                     status="pending", is_run=(mode == "run"))
    session.add(sub)
    try:
        await session.commit()
    except Exception:
        # The row never made it in, so there's nothing to mark judge_error — just
        # release the lock we took above so the write failure doesn't also wedge
        # the user out for the full in-flight TTL.
        if mode == "submit":
            try:
                await queue.release_inflight(user_id, problem_id, str(sub_id))
            except Exception:
                pass
        raise
    await session.refresh(sub)

    # Row first, then enqueue (§5.7): never accept a submission we can't queue.
    try:
        await queue.enqueue_judge(str(sub.id), mode)
    except Exception:
        # Best-effort: if Postgres is failing too, the row stays `pending` and the
        # sweeper re-enqueues it once Redis is back, so the code is judged late
        # rather than lost. Either way the lock is released and the caller gets the
        # 503 — not a 500 that also leaves them locked out for the in-flight TTL.
        try:
            sub.status = "judge_error"
            await session.commit()
        except Exception:
            await session.rollback()
        if mode == "submit":
            try:
                await queue.release_inflight(user_id, problem_id, str(sub_id))
            except Exception:
                pass
        raise APIError(503, "QUEUE_UNAVAILABLE", "Judging is temporarily unavailable.")
    return sub


async def get_submission(session: AsyncSession, user: User, submission_id: uuid.UUID) -> Submission:
    """Fetch a submission, enforcing that the caller owns it.

    Raises:
        APIError(404): no such submission. APIError(403): not the owner.
    """
    sub = await session.get(Submission, submission_id)
    if sub is None:
        raise APIError(404, "NOT_FOUND", "Submission not found.")
    if sub.user_id != user.id:
        raise APIError(403, "FORBIDDEN", "You cannot view this submission.")
    return sub


async def delete_submission(session: AsyncSession, user: User, submission_id: uuid.UUID) -> None:
    """Delete a submission after the same owner check as reading it."""
    sub = await get_submission(session, user, submission_id)  # enforces ownership
    await session.delete(sub)
    await session.commit()


def _accepted_filter(problem_id):
    """The WHERE clauses for "a real, accepted submission on this problem".

    Reused by the stats queries so they consistently exclude Runs (`is_run`) and
    non-accepted attempts. Returns a tuple splatted into `.where(*_accepted_filter(...))`.
    """
    return (Submission.problem_id == problem_id,
            Submission.status == "accepted",
            Submission.is_run.is_(False))


async def runtime_percentile(session: AsyncSession, queue: Queue, problem_id, runtime_ms) -> float | None:
    """What percent of *other* accepted submissions this runtime is at least as
    fast as. Counts accepted submissions with `runtime_ms >= ours`, excluding
    ourselves from both that count and the total, so faster runtimes score
    higher (100 = faster than every other accepted submission). Powers
    "Beats X%".

    Returns None when this is the only accepted submission — with nobody else
    to compare against, any number here (including the old self-inclusive
    formula's trivial 100%) would be a hollow stat rather than a real
    comparison; the frontend shows "you're the first!" instead. Excluding self
    matters: naively counting `runtime_ms >= ours` including our own row would
    always credit us with beating *ourselves*, so e.g. the sole slowest of two
    submissions would score 50% ("beats" itself) instead of the true 0% (it
    beats zero *other* submissions).

    Cached briefly per (problem_id, runtime_ms): this is two full COUNT(*) scans,
    and once accepted a submission's runtime never changes, so repeated polls/
    views of the same (or another identically-timed) submission would otherwise
    re-run both scans every time for the same answer. The sole-submission case
    isn't cached — it's a single count query, and caching "None" isn't
    distinguishable from a cache miss in `get_cached_percentile`'s contract.
    """
    cached = await queue.get_cached_percentile(problem_id, runtime_ms)
    if cached is not None:
        return cached
    total = await session.scalar(
        select(func.count()).select_from(Submission).where(*_accepted_filter(problem_id)))
    if total <= 1:
        return None
    at_least_as_slow = await session.scalar(
        select(func.count()).select_from(Submission).where(
            *_accepted_filter(problem_id), Submission.runtime_ms >= runtime_ms))
    percentile = round(100.0 * (at_least_as_slow - 1) / (total - 1), 1)
    await queue.set_cached_percentile(problem_id, runtime_ms, percentile)
    return percentile


async def runtime_distribution(session: AsyncSession, problem_id, num_buckets: int = 20) -> dict:
    """Bucket accepted-submission runtimes into a histogram for the success modal.

    Returns `{buckets, lo, hi, total}`: `buckets` are equal-width bin counts from
    `lo`..`hi` (min/max runtime). Edge cases return early — no data → empty, and
    all-equal runtimes → a single bucket (avoids a divide-by-zero on zero width).
    The last bin is inclusive of `hi` via the `min(..., num_buckets - 1)` clamp.
    """
    rows = list((await session.execute(
        select(Submission.runtime_ms).where(
            *_accepted_filter(problem_id), Submission.runtime_ms.is_not(None)))).scalars().all())
    if not rows:
        return {"buckets": [], "lo": 0.0, "hi": 0.0, "total": 0}
    lo, hi = float(min(rows)), float(max(rows))
    if hi <= lo:
        return {"buckets": [len(rows)], "lo": lo, "hi": lo, "total": len(rows)}
    width = (hi - lo) / num_buckets
    counts = [0] * num_buckets
    for r in rows:
        counts[min(int((r - lo) / width), num_buckets - 1)] += 1
    return {"buckets": counts, "lo": lo, "hi": hi, "total": len(rows)}


async def list_user_submissions(session: AsyncSession, user: User, slug: str,
                                limit: int = 20) -> list[Submission]:
    """The caller's Submit history for a problem, newest first (Runs excluded).

    Backs the Submissions tab. Ordered by `created_at DESC` and capped at `limit`
    — a query the `(user_id, problem_id, created_at DESC)` index serves directly.
    """
    problem = await session.scalar(
        select(Problem).where(Problem.slug == slug, Problem.is_published.is_(True)))
    if problem is None:
        raise APIError(404, "NOT_FOUND", "Problem not found.")
    rows = await session.execute(
        select(Submission)
        .where(Submission.user_id == user.id, Submission.problem_id == problem.id,
               Submission.is_run.is_(False))
        .order_by(Submission.created_at.desc())
        .limit(limit))
    return list(rows.scalars().all())
