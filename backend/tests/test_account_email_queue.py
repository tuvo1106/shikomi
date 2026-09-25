"""Account email moved off the request path: the API enqueues, the worker sends.

(DESIGN.md §4.1 anti-enumeration; `worker/accounts.py:send_account_email`.)
"""
import smtplib

import pytest

from worker import accounts as worker_accounts

REGISTER = "/api/v1/auth/register"
RESEND = "/api/v1/auth/resend-verification"
RESET = "/api/v1/auth/password-reset/request"
CREDS = {"email": "alice@example.com", "username": "alice", "password": "sunflower-desk-42"}


@pytest.fixture
def held(queue):
    """Hold jobs in the queue instead of running them, to see what the API did."""
    queue.deliver_emails = False
    return queue


async def test_register_only_enqueues(client, held, outbox):
    r = await client.post(REGISTER, json=CREDS)

    assert r.status_code == 202
    assert held.emails == [("verify", CREDS["email"])]
    assert outbox == []  # nothing was mailed in the request


async def test_register_taken_email_enqueues_the_existing_account_notice(
        client, held, outbox, make_user):
    await make_user(email=CREDS["email"], username="someone")

    r = await client.post(REGISTER, json=CREDS)

    assert r.status_code == 202
    assert held.emails == [("existing", CREDS["email"])]
    assert outbox == []


@pytest.mark.parametrize("path,kind", [(RESEND, "verify"), (RESET, "reset")])
async def test_known_and_unknown_addresses_do_identical_work(
        client, held, make_user, path, kind):
    """The request enqueues the same job either way, so neither its response nor
    its timing depends on whether the address is registered."""
    await make_user(email="known@example.com", username="known", verified=False)

    known = await client.post(path, json={"email": "known@example.com"})
    unknown = await client.post(path, json={"email": "nobody@example.com"})

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert held.emails == [(kind, "known@example.com"), (kind, "nobody@example.com")]


@pytest.mark.parametrize("path,body", [
    (RESEND, {"email": "known@example.com"}),
    (RESET, {"email": "known@example.com"}),
])
async def test_a_redis_failure_never_fails_the_request(client, queue, make_user, path, body):
    await make_user(email="known@example.com", username="known", verified=False)
    queue.fail_email_enqueue = True

    assert (await client.post(path, json=body)).status_code == 202


async def test_register_survives_an_enqueue_failure(client, queue, session_factory):
    """The account is committed before the enqueue, so a Redis blip must not 500 it."""
    from sqlalchemy import select

    from app.models import User
    queue.fail_email_enqueue = True

    assert (await client.post(REGISTER, json=CREDS)).status_code == 202
    async with session_factory() as s:
        assert await s.scalar(select(User).where(User.email == CREDS["email"])) is not None


async def test_worker_sends_verify_reset_and_existing(session_factory, make_user, outbox,
                                                      monkeypatch):
    monkeypatch.setattr(worker_accounts, "SessionLocal", session_factory)
    await make_user(email="u@example.com", username="u", verified=False)

    for kind in ("verify", "reset", "existing"):
        await worker_accounts.send_account_email({}, kind, "u@example.com")

    assert [m.subject for m in outbox] == [
        "Verify your shikomi email",
        "Reset your shikomi password",
        "You already have a shikomi account",
    ]
    assert "verify-email?token=" in outbox[0].body
    assert "reset-password?token=" in outbox[1].body


async def test_worker_ignores_unknown_addresses_kinds_and_verified_accounts(
        session_factory, make_user, outbox, monkeypatch):
    monkeypatch.setattr(worker_accounts, "SessionLocal", session_factory)
    await make_user(email="done@example.com", username="done", verified=True)

    await worker_accounts.send_account_email({}, "verify", "nobody@example.com")
    await worker_accounts.send_account_email({}, "reset", "nobody@example.com")
    await worker_accounts.send_account_email({}, "verify", "done@example.com")  # already verified
    await worker_accounts.send_account_email({}, "bogus", "done@example.com")

    assert outbox == []


def test_email_job_has_its_own_short_timeout():
    """A hung SMTP server mustn't hold a slot for the judge-sized 300s backstop."""
    from worker.main import EMAIL_JOB_TIMEOUT_SECONDS, AccountsWorkerSettings, WorkerSettings

    job = next(f for f in AccountsWorkerSettings.functions
               if getattr(f, "name", None) == "send_account_email")
    assert job.timeout_s == EMAIL_JOB_TIMEOUT_SECONDS < WorkerSettings.job_timeout


def test_purge_cron_keeps_a_generous_timeout():
    """The accounts worker's class-level job_timeout is sized for mail; the hourly purge
    (one big DELETE) must not inherit it."""
    from worker.main import PURGE_JOB_TIMEOUT_SECONDS, AccountsWorkerSettings

    purge = next(c for c in AccountsWorkerSettings.cron_jobs
                 if c.name == "cron:purge_unverified_users")
    assert purge.timeout_s == PURGE_JOB_TIMEOUT_SECONDS > AccountsWorkerSettings.job_timeout


def test_mail_and_judging_run_on_separate_queues():
    """The point of the split: a mail backlog can't delay judging, or vice versa. Each
    worker only registers its own jobs, and the enqueue side targets the mail queue the
    accounts worker listens on."""
    from arq.constants import default_queue_name

    from app import queue as queue_mod
    from worker.main import AccountsWorkerSettings, WorkerSettings

    def names(w):
        return {getattr(f, "name", getattr(f, "__name__", None)) for f in w.functions}

    assert AccountsWorkerSettings.queue_name == queue_mod.ACCOUNTS_QUEUE != default_queue_name
    assert not hasattr(WorkerSettings, "queue_name")  # judge worker stays on arq's default
    assert "send_account_email" in names(AccountsWorkerSettings)
    assert "send_account_email" not in names(WorkerSettings)
    assert "judge_submission" in names(WorkerSettings)
    assert "judge_submission" not in names(AccountsWorkerSettings)


async def test_enqueue_email_targets_the_accounts_queue():
    """`Queue.enqueue_email` passes `_queue_name`, or the accounts worker would never see it."""
    from app import queue as queue_mod

    class Pool:
        calls = []

        async def enqueue_job(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    pool = Pool()
    await queue_mod.Queue(pool).enqueue_email("verify", "a@example.com")

    assert pool.calls == [(("send_account_email", "verify", "a@example.com"),
                           {"_queue_name": queue_mod.ACCOUNTS_QUEUE})]


# --- retry: the SMTP hop retries in-process; the token is minted once ------------------

def _flaky_send(monkeypatch, failures, error=ConnectionError("smtp down")):
    """Make `app.email.send_email` fail `failures` times with `error`, then succeed via
    the real console backend. Returns the list of attempts and the recorded backoff
    sleeps."""
    from app import email as email_mod

    real, attempts, sleeps = email_mod.send_email, [], []

    def flaky(to, subject, body):
        attempts.append(to)
        if len(attempts) <= failures:
            raise error
        real(to, subject, body)

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(email_mod, "send_email", flaky)
    monkeypatch.setattr(email_mod, "_sleep", fake_sleep)
    return attempts, sleeps


async def test_smtp_failure_is_retried_with_backoff_then_succeeds(monkeypatch, outbox):
    from app import email as email_mod
    attempts, sleeps = _flaky_send(monkeypatch, failures=2)

    await email_mod.send_best_effort("a@example.com", "s", "b")

    assert len(attempts) == 3 and len(outbox) == 1
    delay = email_mod.settings.email_retry_delay_seconds
    assert sleeps == [delay, delay * 2]


async def test_smtp_gives_up_quietly_after_the_last_attempt(monkeypatch, outbox):
    from app import email as email_mod
    attempts, _ = _flaky_send(monkeypatch, failures=99)

    await email_mod.send_best_effort("a@example.com", "s", "b")  # must not raise

    assert len(attempts) == email_mod.settings.email_send_attempts
    assert outbox == []


async def test_retrying_the_send_mints_the_token_only_once(
        session_factory, make_user, monkeypatch, outbox):
    """A flaky mail server must not leave a trail of extra live links."""
    from sqlalchemy import func, select

    from app.models import EmailToken
    monkeypatch.setattr(worker_accounts, "SessionLocal", session_factory)
    await make_user(email="u@example.com", username="u", verified=False)
    attempts, _ = _flaky_send(monkeypatch, failures=2)

    await worker_accounts.send_account_email({}, "reset", "u@example.com")

    assert len(attempts) == 3 and len(outbox) == 1
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(EmailToken)) == 1


def _db_error():
    from sqlalchemy.exc import OperationalError
    return OperationalError("stmt", {}, Exception("connection lost"))


@pytest.mark.parametrize("error", [_db_error(), ConnectionRefusedError("postgres down")])
async def test_a_transient_db_error_is_retried_via_arq_then_dropped(monkeypatch, error):
    """Both a wrapped DB error and asyncpg's raw connect failure (an `OSError` that
    SQLAlchemy doesn't wrap) must be retried."""
    from arq import Retry

    from app.services import account_service

    async def boom(*_a):
        raise error

    monkeypatch.setattr(account_service, "deliver_account_email", boom)
    monkeypatch.setattr(worker_accounts, "SessionLocal", lambda: _NullSession())

    with pytest.raises(Retry):
        await worker_accounts.send_account_email({"job_try": 1}, "verify", "u@example.com")
    # On the last try it gives up (logged) instead of raising forever.
    await worker_accounts.send_account_email(
        {"job_try": worker_accounts.EMAIL_MAX_TRIES}, "verify", "u@example.com")


class _NullSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


def test_worst_case_retry_time_fits_inside_the_job_timeout():
    """attempts x SMTP timeout + backoff must fit the email job's own timeout, or arq
    would kill the job mid-retry."""
    from app.config import get_settings
    from worker.main import EMAIL_JOB_TIMEOUT_SECONDS
    cfg = get_settings()
    backoff = sum(cfg.email_retry_delay_seconds * 2 ** i for i in range(cfg.email_send_attempts - 1))
    assert cfg.email_send_attempts * cfg.smtp_timeout_seconds + backoff < EMAIL_JOB_TIMEOUT_SECONDS


def test_email_job_arq_retries_are_wired():
    from worker.main import AccountsWorkerSettings
    job = next(f for f in AccountsWorkerSettings.functions
               if getattr(f, "name", None) == "send_account_email")
    assert job.max_tries == worker_accounts.EMAIL_MAX_TRIES


@pytest.mark.parametrize("error", [
    smtplib.SMTPRecipientsRefused({"a@example.com": (550, b"no such user")}),
    smtplib.SMTPAuthenticationError(535, b"bad credentials"),
    smtplib.SMTPSenderRefused(553, b"sender rejected", "no-reply@x"),
    ValueError("a bug, not a network problem"),
])
async def test_permanent_failures_are_not_retried(monkeypatch, outbox, error):
    """A bad recipient or credentials will fail every time; retrying only holds a
    worker slot (and a bad-address flood could burn slots that way)."""
    from app import email as email_mod
    attempts, sleeps = _flaky_send(monkeypatch, failures=99, error=error)

    await email_mod.send_best_effort("a@example.com", "s", "b")

    assert len(attempts) == 1 and sleeps == []


@pytest.mark.parametrize("error", [
    smtplib.SMTPResponseException(421, b"try again later"),
    smtplib.SMTPRecipientsRefused({"a@example.com": (450, b"greylisted")}),
    smtplib.SMTPServerDisconnected("connection lost"),
    TimeoutError("timed out"),
])
async def test_transient_failures_are_retried(monkeypatch, outbox, error):
    from app import email as email_mod
    attempts, _ = _flaky_send(monkeypatch, failures=1, error=error)

    await email_mod.send_best_effort("a@example.com", "s", "b")

    assert len(attempts) == 2 and len(outbox) == 1


async def test_a_broken_inline_job_fails_the_test_instead_of_hiding(queue, monkeypatch):
    """Guards the FakeQueue itself: the API swallows enqueue errors, so a bug in the job
    must be recorded and surfaced at teardown, not silently pass."""
    async def boom(*_a):
        raise RuntimeError("job bug")

    monkeypatch.setattr(worker_accounts, "send_account_email", boom)
    await queue.enqueue_email("verify", "a@example.com")

    assert [str(e) for e in queue.delivery_errors] == ["job bug"]
    queue.delivery_errors.clear()  # expected here; keep the teardown check quiet
