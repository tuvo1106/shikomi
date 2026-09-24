"""Watchdog for the accounts queue: alert when account mail stops flowing.

Account email runs on its own arq queue and worker (`worker.main.AccountsWorkerSettings`),
so if that worker dies or wedges on SMTP, register / resend / reset keep answering 202 while
verify and reset links silently never arrive, and nothing else in the system notices. This
cron is the "notice".

It runs on the *judge* worker on purpose. A watchdog living on the accounts worker would
die with the thing it watches. The tradeoff, accepted: it shares fate with the judge worker
(which KEDA can scale to zero, and which is down if the whole stack is), so it is a
best-effort in-app alert, not a substitute for an external monitor on
`GET /internal/accounts-queue-depth` (its `oldest_age_seconds` is the same signal).
"""
import asyncio
import logging

from app import email
from app.config import get_settings
from app.queue import Queue

logger = logging.getLogger("watchdog")
settings = get_settings()

ACCOUNTS_ALERT = "accounts-queue-backlog"


async def check_accounts_queue(ctx) -> None:
    """Cron (every minute): alert if the oldest queued mail job is older than the threshold.

    Age, not depth: a burst of resets spikes depth and drains in a second, but a job that
    has been queued (or stuck mid-run) for `accounts_queue_stale_seconds` (normally it waits
    well under one) means nothing is consuming. Logs at ERROR (the alert if you ship logs
    anywhere) and mails `alert_email` when it's set, at most once per
    `alert_cooldown_seconds` (a Redis key), so a queue that stays stuck reminds hourly, not
    every minute.

    Two details that keep the alert honest:

    * The cooldown is **re-armed** when the queue is healthy again, so a second outage
      inside the cooldown window is reported rather than swallowed.
    * The cooldown is **released** if the alert email can't be sent. SMTP being down is the
      likeliest reason mail is stuck, so the alert path must not go quiet for an hour just
      because its one delivery attempt failed; it tries again next minute.

    The email goes straight out over SMTP in a worker thread, one attempt bounded by
    `smtp_timeout_seconds`, never onto the accounts queue (the thing that's stuck) and never
    through `send_best_effort`'s retry-with-backoff (which would hold one of this worker's
    job slots open and swallow the failure this function needs to see).
    """
    queue = Queue(ctx["redis"])
    depth, age = await queue.accounts_queue_stats()
    if age is None or age < settings.accounts_queue_stale_seconds:
        await queue.release_alert(ACCOUNTS_ALERT)  # healthy: re-arm for the next outage
        return
    if not await queue.claim_alert(ACCOUNTS_ALERT, settings.alert_cooldown_seconds):
        return
    summary = (f"Account email is backed up: {depth} job(s) on arq:accounts, the oldest queued "
               f"(or stuck) for {age / 60:.1f} min. The accounts worker "
               "(`arq worker.main.AccountsWorkerSettings`) is down or wedged; verification and "
               "password-reset links are not being sent.")
    logger.error(summary)
    if not settings.alert_email:
        return
    try:
        await asyncio.to_thread(
            email.send_email, settings.alert_email, "[shikomi] account email is backed up",
            summary)
    except Exception:  # noqa: BLE001 - any delivery failure must re-arm, whatever its type
        logger.warning("alert email to %s failed; will retry next minute", settings.alert_email,
                       exc_info=True)
        await queue.release_alert(ACCOUNTS_ALERT)
