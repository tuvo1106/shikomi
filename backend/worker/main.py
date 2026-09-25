"""arq worker entrypoints (DESIGN.md §5.6, §5.7).

Two worker classes, two queues, so unrelated work can't starve each other:

* `WorkerSettings` — the **judge** worker (default arq queue): judging, reference
  validation and orphan-sandbox reaping. Needs the sandbox runner (Docker socket or
  k8s RBAC) and is what KEDA autoscales on queue depth.
* `AccountsWorkerSettings` — the **accounts** worker (`ACCOUNTS_QUEUE`): account email, the
  unverified-signup purge and the stale-submission sweep (it needs only Postgres/Redis, so it
  keeps running while the judge worker is scaled to zero). No sandbox access, so it runs from
  the lean api image.

Run with:  uv run arq worker.main.WorkerSettings
           uv run arq worker.main.AccountsWorkerSettings
"""
import logging

from arq import cron, func
from arq.connections import RedisSettings

from app.config import EMAIL_JOB_TIMEOUT_SECONDS, get_settings
from app.judge_budget import JUDGE_JOB_TIMEOUT_SECONDS
from app.queue import ACCOUNTS_QUEUE
from worker import runner
from worker.accounts import EMAIL_MAX_TRIES, purge_unverified_users, send_account_email
from worker.judge import judge_submission, reap_orphans
from worker.sweeper import sweep_stale
from worker.watchdog import check_accounts_queue

settings = get_settings()
PURGE_JOB_TIMEOUT_SECONDS = 300
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


async def on_startup(ctx) -> None:
    # Reap any judge sandboxes orphaned by a previous crash (§5.5).
    await runner.sweep_orphans()


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    functions = [judge_submission]
    cron_jobs = [
        cron(reap_orphans, second=0, run_at_startup=False),  # top of every minute
        # Watches the *accounts* queue from here so it still fires when that worker is the
        # thing that died (worker/watchdog.py).
        cron(check_accounts_queue, second=30, run_at_startup=False),
    ]
    on_startup = on_startup
    max_jobs = settings.judge_max_concurrency
    max_tries = 1          # no automatic retries; a failed job becomes judge_error (§5.7)
    # Backstop above `run_judgement`'s own per-problem wall clock. Authoring refuses any problem
    # whose wall clock would not fit inside it (app/judge_budget.py), so arq never cancels a
    # judge job that was still within its own budget.
    job_timeout = JUDGE_JOB_TIMEOUT_SECONDS


class AccountsWorkerSettings:
    """Account email + signup purge, on their own queue (see the module docstring).

    Kept off the judge worker so a mail backlog can't delay judging and vice versa, and
    so it never needs sandbox privileges. `max_jobs` is small: mail is I/O-bound and
    ~100ms a job, and the per-IP auth rate limits cap the inflow. Not KEDA-scaled — one
    replica is plenty, and it must keep running at zero judge load (a verify link
    shouldn't wait for a scale-up).
    """
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    queue_name = ACCOUNTS_QUEUE
    functions = [
        func(send_account_email, timeout=EMAIL_JOB_TIMEOUT_SECONDS, max_tries=EMAIL_MAX_TRIES),
    ]
    cron_jobs = [
        # Its own generous timeout: the class-level job_timeout below is sized for mail,
        # and one big DELETE (e.g. after a bot-signup flood) shouldn't be cancelled hourly.
        cron(purge_unverified_users, minute=17, second=0, run_at_startup=False,
             timeout=PURGE_JOB_TIMEOUT_SECONDS),  # hourly
        # Fails stuck submissions, re-enqueues lost judge jobs, prunes tokens. Here rather than
        # on the judge worker so it still runs when KEDA has scaled that to zero (worker/sweeper.py).
        cron(sweep_stale, second=0, run_at_startup=False),  # top of every minute
    ]
    max_jobs = 10
    max_tries = 1
    job_timeout = 120      # backstop above the per-job EMAIL_JOB_TIMEOUT_SECONDS
