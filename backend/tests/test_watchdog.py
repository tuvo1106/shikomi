"""The accounts-queue watchdog (`worker/watchdog.py`): alert when account mail stops flowing."""
import logging

import pytest

from app.config import get_settings
from worker import watchdog

STALE = get_settings().accounts_queue_stale_seconds


@pytest.fixture
def stuck(queue, monkeypatch):
    """A watchdog wired to the fake queue, with an operator address configured."""
    monkeypatch.setattr(watchdog, "Queue", lambda redis: queue)
    monkeypatch.setattr(get_settings(), "alert_email", "ops@example.com")
    queue.accounts_depth = 4
    return queue


async def check():
    await watchdog.check_accounts_queue({"redis": None})


async def test_an_empty_queue_is_quiet(stuck, outbox, caplog):
    stuck.accounts_oldest_age = None
    await check()
    assert outbox == [] and not caplog.records


async def test_a_backlog_that_is_still_moving_is_quiet(stuck, outbox, caplog):
    """A burst of resets spikes depth but drains; a young oldest-job is not an outage."""
    stuck.accounts_depth, stuck.accounts_oldest_age = 500, STALE - 30
    await check()
    assert outbox == [] and not caplog.records


async def test_a_stuck_queue_logs_an_error_and_emails_the_operator(stuck, outbox, caplog):
    stuck.accounts_oldest_age = STALE + 120
    with caplog.at_level(logging.ERROR, logger="watchdog"):
        await check()

    assert any("Account email is backed up" in r.getMessage() and r.levelno == logging.ERROR
               for r in caplog.records)
    assert [m.to for m in outbox] == ["ops@example.com"]
    assert "4 job(s)" in outbox[0].body and "7.0 min" in outbox[0].body


async def test_it_only_alerts_once_per_cooldown(stuck, outbox):
    """The cron runs every minute; a queue that stays stuck must not page every minute."""
    stuck.accounts_oldest_age = STALE + 1
    await check()
    await check()
    await check()
    assert len(outbox) == 1


async def test_without_an_alert_address_it_still_logs(stuck, monkeypatch, outbox, caplog):
    monkeypatch.setattr(get_settings(), "alert_email", "")
    stuck.accounts_oldest_age = STALE + 1
    with caplog.at_level(logging.ERROR, logger="watchdog"):
        await check()
    assert outbox == [] and any(r.levelno == logging.ERROR for r in caplog.records)


def test_the_watchdog_runs_on_the_judge_worker_not_the_one_it_watches():
    """A watchdog on the accounts worker would die with the thing it watches."""
    from worker.main import AccountsWorkerSettings, WorkerSettings

    def cron_names(w):
        return {c.name for c in w.cron_jobs}

    assert "cron:check_accounts_queue" in cron_names(WorkerSettings)
    assert "cron:check_accounts_queue" not in cron_names(AccountsWorkerSettings)


async def test_a_second_outage_inside_the_cooldown_still_alerts(stuck, outbox):
    """Once the queue is healthy again the alert re-arms; otherwise an outage that recurs
    within the hour would be swallowed by the first outage's cooldown."""
    stuck.accounts_oldest_age = STALE + 1
    await check()                       # outage 1: alert
    stuck.accounts_oldest_age = None
    await check()                       # recovered: re-arms
    stuck.accounts_oldest_age = STALE + 1
    await check()                       # outage 2, well inside the cooldown
    assert len(outbox) == 2


async def test_a_failed_alert_email_is_retried_not_silenced_for_the_cooldown(
        stuck, monkeypatch, outbox, caplog):
    """SMTP being down is the likeliest reason mail is stuck, so the alert path mustn't go
    quiet for an hour because its one send failed."""
    from app import email as email_mod

    def boom(*args, **kwargs):
        raise ConnectionError("smtp down")

    real_send = email_mod.send_email
    monkeypatch.setattr(email_mod, "send_email", boom)
    stuck.accounts_oldest_age = STALE + 1
    with caplog.at_level(logging.WARNING, logger="watchdog"):
        await check()
    assert outbox == []
    assert any("failed; will retry" in r.getMessage() for r in caplog.records)

    monkeypatch.setattr(email_mod, "send_email", real_send)   # SMTP comes back
    await check()
    assert [m.to for m in outbox] == ["ops@example.com"]      # delivered on the next check


async def test_the_alert_email_does_not_touch_the_stuck_queue(stuck, outbox, monkeypatch):
    """It goes out over SMTP directly, not through `send_best_effort`'s retry/backoff."""
    from app import email as email_mod

    async def forbidden(*args, **kwargs):
        raise AssertionError("alert must not use send_best_effort")

    monkeypatch.setattr(email_mod, "send_best_effort", forbidden)
    stuck.accounts_oldest_age = STALE + 1
    await check()
    assert len(outbox) == 1


def test_the_alert_threshold_must_outlast_a_mail_job():
    """arq keeps a running job in the queue set, so a threshold at or below the job timeout
    would fire on a healthy worker that is just slow on one send. Refuse it at startup."""
    from pydantic import ValidationError

    from app.config import EMAIL_JOB_TIMEOUT_SECONDS, Settings

    for bad in (EMAIL_JOB_TIMEOUT_SECONDS, EMAIL_JOB_TIMEOUT_SECONDS - 1, 1):
        with pytest.raises(ValidationError, match="ACCOUNTS_QUEUE_STALE_SECONDS"):
            Settings(_env_file=None, accounts_queue_stale_seconds=bad)
    assert Settings(_env_file=None,
                    accounts_queue_stale_seconds=EMAIL_JOB_TIMEOUT_SECONDS + 1)
