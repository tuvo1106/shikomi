"""The worker's jobs: judge a submission and sweep up afterwards (§5.2, §5.7).

`judge_submission` is the async task the API enqueued: load the row, run the
sandbox, write the verdict. Its structure is all about *invariants under failure* —
whatever happens, a crashed judgement must land as `judge_error` (never stuck
`pending`), and the in-flight lock must be released so the user isn't wedged. Hence
the try/except (→ judge_error) / finally (→ release lock) shape.

The once-a-minute safety net for anything the happy path missed is split by what it needs:
`reap_orphans` (here) kills orphaned sandboxes and needs the sandbox runner, so it stays on
the judge worker; `worker/sweeper.py::sweep_stale` fails stuck submissions and prunes old
tokens using only the database and Redis, so it runs on the always-on accounts worker and
still fires when KEDA has scaled the judge worker to zero.
"""
import asyncio
import logging
import uuid

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.models import Problem, Submission
from app.queue import Queue
from worker import runner
from worker.judging import build_verdict_results, run_judgement

logger = logging.getLogger("judge")


async def _fail_submission(sub_uuid: uuid.UUID) -> None:
    """Flip a still-unfinished submission to `judge_error` using a *fresh* session.

    Used when the job is cancelled: the job's own session may be mid-query (a cancelled
    asyncpg call leaves it unusable), so the verdict write can't reuse it. The status guard
    means it never overwrites a verdict that had already landed.
    """
    async with SessionLocal() as session:
        await session.execute(
            update(Submission)
            .where(Submission.id == sub_uuid, Submission.status.in_(("pending", "running")))
            .values(status="judge_error"))
        await session.commit()


async def judge_submission(ctx, submission_id: str, mode: str) -> None:
    """Load the submission, judge it in a sandbox, persist the verdict (§5.2).

    Two failure paths, and the difference matters: an `Exception` (a sandbox fault, a missing
    problem) is caught and recorded as `judge_error`; a **cancellation** is a different
    exception class (`CancelledError` is a `BaseException`, so `except Exception` never sees it).
    arq cancels the job at its `job_timeout`, and on worker shutdown. Without an explicit
    handler the verdict write was skipped and the row spun in `running` until the ~5 minute
    sweeper failed it. Authoring refuses any problem that could hit the timeout in normal
    operation (app/judge_budget.py), so this is the belt to that brace.
    """
    redis = ctx["redis"]
    sub_uuid = uuid.UUID(submission_id)

    async with SessionLocal() as session:
        sub = await session.get(Submission, sub_uuid)
        if sub is None:
            logger.warning("submission %s not found; skipping", submission_id)
            return
        # Read once, before anything can expire the ORM object (a rollback does), so the
        # `finally` can always release the lock without a lazy load that would itself raise.
        user_id, problem_id = sub.user_id, sub.problem_id
        try:
            problem = (await session.execute(
                select(Problem).where(Problem.id == problem_id)
                .options(selectinload(Problem.test_cases)))).scalar_one_or_none()
            if problem is None:
                raise RuntimeError(f"problem {sub.problem_id} missing")

            # Claim the job: flip pending -> running only if the row is still pending. The
            # stale sweep runs on another worker and can fail a row (and free its lock) while
            # its job is still queued, e.g. the judge worker was scaled to zero or backlogged
            # for over five minutes. Running that job later would overwrite `judge_error` with a
            # real verdict the user was already told never came, and with the lock gone a second
            # submit could be judging at the same time. Losing the claim means the row is settled.
            claimed = await session.execute(
                update(Submission)
                .where(Submission.id == sub_uuid, Submission.status == "pending")
                .values(status="running"))
            await session.commit()
            if claimed.rowcount != 1:
                logger.warning("submission %s is no longer pending (already settled); skipping",
                               submission_id)
                return
            sub.status = "running"  # keep the loaded object in step with the row

            cases = sorted(
                (tc for tc in problem.test_cases if mode == "submit" or tc.is_sample),
                key=lambda t: t.ordinal)
            verdict = await run_judgement(
                code=sub.code, function_name=problem.function_name,
                comparison=problem.comparison, time_limit_ms=problem.time_limit_ms,
                memory_limit_mb=problem.memory_limit_mb,
                params=problem.params, return_type=problem.return_type,
                kind=problem.kind, class_name=problem.class_name, language=problem.language,
                test_cases=[{"id": tc.ordinal, "input": tc.input, "expected": tc.expected}
                            for tc in cases],
                container_name=f"judge-{submission_id}")

            sub.status = verdict.status
            sub.runtime_ms = verdict.runtime_ms
            sub.verdict_detail = {
                "results": build_verdict_results(verdict.results, cases),
                "passed": verdict.passed,
                "total": verdict.total,
            }
            await session.commit()
            logger.info("submission %s -> %s", submission_id, verdict.status)
        except Exception:
            logger.exception("judge error for submission %s", submission_id)
            await session.rollback()
            stuck = await session.get(Submission, sub_uuid)
            if stuck is not None:
                stuck.status = "judge_error"
                await session.commit()
        except asyncio.CancelledError:
            # The sandbox itself is killed by the runner's own cancel handler; here we settle
            # the row (shielded, so a second cancel can't leave it half-written), then re-raise
            # so arq still sees the job as cancelled. The `finally` below releases the lock.
            logger.error("judge job for submission %s cancelled (job timeout or shutdown)",
                         submission_id)
            await asyncio.shield(_fail_submission(sub_uuid))
            raise
        finally:
            if mode == "submit":
                # Only if the lock is still ours: after a TTL expiry it may belong to a newer
                # submission, and deleting that would let a third submit slip in.
                await Queue(redis).release_inflight(user_id, problem_id, submission_id)


async def reap_orphans(ctx) -> None:
    """Cron (every minute): kill sandboxes no live job can own (§5.5, §5.7).

    Stays on the judge worker because only it has the Docker socket / judge-Pod RBAC. Age-guarded
    so it is safe across replicas (`docker_runner.sweep_orphans`). There is nothing to reap at
    zero replicas: with no judge worker there are no sandboxes.
    """
    await runner.sweep_orphans()
