"""`app/telemetry.py` and the instrumentation points that call it.

Telemetry is a side channel, so the properties worth pinning are: it is inert without an
agent, it reports what it should with bounded tags, and it never changes an outcome.
"""
import asyncio
from types import SimpleNamespace

import ozy
import pytest

from app import telemetry
from app.main import app, create_app
from app.queue import Queue


@pytest.fixture(autouse=True)
def _statsd_left_disabled():
    yield
    ozy.statsd.close()


def test_telemetry_is_inert_without_an_agent_host(monkeypatch):
    monkeypatch.setattr(telemetry, "get_settings", lambda: SimpleNamespace(
        ozy_agent_host=None, ozy_env=None, ozy_version=None))
    telemetry.init_telemetry("leetercode-api")
    assert telemetry.enabled() is False
    telemetry.count("x")  # a no-op, not an error
    telemetry.gauge("y", 1)


def test_init_enables_the_sdk_when_a_host_is_set(monkeypatch):
    monkeypatch.setattr(telemetry, "get_settings", lambda: SimpleNamespace(
        ozy_agent_host="127.0.0.1", ozy_env="dev", ozy_version="1"))
    telemetry.init_telemetry("leetercode-worker")
    assert telemetry.enabled() is True


def test_count_and_gauge_drop_none_tags(statsd_calls):
    telemetry.count("a.b", 3, language=None, mode="submit")
    telemetry.gauge("c.d", 7, queue="judge")
    assert statsd_calls.calls == [
        ("count", "a.b", 3, ["mode:submit"]),
        ("gauge", "c.d", 7, ["queue:judge"]),
    ]


def test_observe_records_duration_and_count_with_the_default_outcome(statsd_calls):
    with telemetry.observe("job.thing", function="f"):
        pass
    (dur,) = statsd_calls.named("job.thing.duration")
    (cnt,) = statsd_calls.named("job.thing.count")
    assert dur[3] == cnt[3] == ["function:f", "outcome:ok"]
    assert dur[2] >= 0


def test_observe_lets_the_caller_set_the_outcome(statsd_calls):
    with telemetry.observe("job.thing") as obs:
        obs.outcome = "wrong_answer"
    assert statsd_calls.named("job.thing.count")[0][3] == ["outcome:wrong_answer"]


def test_observe_marks_errors_and_reraises(statsd_calls):
    with pytest.raises(ValueError), telemetry.observe("job.thing"):
        raise ValueError("boom")
    assert statsd_calls.named("job.thing.count")[0][3] == ["outcome:error"]


def test_observe_distinguishes_a_cancellation_from_a_failure(statsd_calls):
    with pytest.raises(asyncio.CancelledError), telemetry.observe("job.thing"):
        raise asyncio.CancelledError
    assert statsd_calls.named("job.thing.count")[0][3] == ["outcome:cancelled"]


def test_an_explicit_outcome_survives_an_exception(statsd_calls):
    with pytest.raises(RuntimeError), telemetry.observe("job.thing") as obs:
        obs.outcome = "judge_error"
        raise RuntimeError
    assert statsd_calls.named("job.thing.count")[0][3] == ["outcome:judge_error"]


async def test_job_wrapper_keeps_the_name_and_tags_the_function(statsd_calls):
    async def my_job(ctx, a, b=2):
        return (ctx, a, b)

    wrapped = telemetry.job(my_job)
    assert wrapped.__name__ == "my_job"  # arq registers jobs by __name__
    assert await wrapped("ctx", 1, b=3) == ("ctx", 1, 3)
    assert statsd_calls.named("arq.job.count")[0][3] == ["function:my_job", "outcome:ok"]


async def test_job_wrapper_never_swallows_the_jobs_exception(statsd_calls):
    async def failing(ctx):
        raise KeyError("x")

    with pytest.raises(KeyError):
        await telemetry.job(failing)({})
    assert statsd_calls.named("arq.job.count")[0][3] == ["function:failing", "outcome:error"]


def test_the_api_stack_has_the_metrics_middleware_outermost_and_skips_probes():
    middleware = create_app().user_middleware
    assert middleware[0].cls is telemetry.MetricsMiddleware  # added last => first in the list
    skipped = set(middleware[0].kwargs["exclude_paths"])
    assert {"/api/v1/healthz", "/api/v1/livez", "/api/v1/internal/queue-depth"} <= skipped
    assert app is not None


class _Redis:
    def __init__(self, *, set_result=True, eval_result=1):
        self.set_result, self.eval_result, self.jobs = set_result, eval_result, []

    async def set(self, *args, **kwargs):
        return self.set_result

    async def eval(self, *args):
        return self.eval_result

    async def enqueue_job(self, function, *args, **kwargs):
        self.jobs.append(function)
        return object()


async def test_queue_counts_enqueues_by_function(statsd_calls):
    queue = Queue(_Redis())
    await queue.enqueue_judge("sid", "submit")
    await queue.enqueue_email("verify", "a@example.com")
    assert [c[3] for c in statsd_calls.named("arq.jobs.enqueued")] == [
        ["function:judge_submission"], ["function:send_account_email"]]


async def test_a_refused_duplicate_enqueue_is_not_counted(statsd_calls):
    class Duplicate(_Redis):
        async def enqueue_job(self, *args, **kwargs):
            return None  # arq's answer when the job id already exists

    assert await Queue(Duplicate()).enqueue_judge("sid", "submit") is False
    assert statsd_calls.named("arq.jobs.enqueued") == []


async def test_contended_inflight_lock_is_counted_only_when_refused(statsd_calls):
    assert await Queue(_Redis(set_result=True)).acquire_inflight("u", "p", "s") is True
    assert statsd_calls.named("inflight.lock.contended") == []
    assert await Queue(_Redis(set_result=False)).acquire_inflight("u", "p", "s") is False
    assert len(statsd_calls.named("inflight.lock.contended")) == 1


async def test_rate_limit_rejections_are_counted_by_action(statsd_calls):
    queue = Queue(_Redis(eval_result=11))
    assert await queue.within_rate_limit("u", "submit", 10) is False
    assert statsd_calls.named("ratelimit.rejected")[0][3] == ["action:submit"]
    assert await Queue(_Redis(eval_result=10)).within_rate_limit("u", "submit", 10) is True
    assert len(statsd_calls.named("ratelimit.rejected")) == 1


async def test_login_lock_is_counted(statsd_calls):
    await Queue(_Redis()).set_login_lock("a@example.com", 900)
    assert len(statsd_calls.named("auth.login.locked")) == 1


class _DepthQueue:
    def __init__(self, age=12.5, fail=False):
        self.age, self.fail = age, fail

    async def queue_depth(self):
        if self.fail:
            raise ConnectionError("redis down")
        return 4

    async def accounts_queue_stats(self):
        return 2, self.age


async def _one_pass(monkeypatch, queue):
    """Run `_report_queue_depth` for exactly one iteration (the sleep ends the loop)."""
    from app import main

    async def get_queue():
        return queue

    async def stop(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(main, "get_queue", get_queue)
    monkeypatch.setattr(main.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await main._report_queue_depth()


async def test_queue_depth_reporter_gauges_both_queues(monkeypatch, statsd_calls):
    await _one_pass(monkeypatch, _DepthQueue())
    assert [(c[2], c[3]) for c in statsd_calls.named("arq.queue.depth")] == [
        (4, ["queue:judge"]), (2, ["queue:accounts"])]
    assert statsd_calls.named("arq.queue.oldest_age_seconds")[0][2] == 12.5


async def test_an_empty_accounts_queue_reports_no_age(monkeypatch, statsd_calls):
    await _one_pass(monkeypatch, _DepthQueue(age=None))
    assert statsd_calls.named("arq.queue.oldest_age_seconds") == []


async def test_queue_depth_reporter_survives_a_redis_failure(monkeypatch, statsd_calls):
    await _one_pass(monkeypatch, _DepthQueue(fail=True))  # reaches the sleep, i.e. kept looping
    assert statsd_calls.named("arq.queue.depth") == []


async def test_lifespan_runs_the_reporter_only_when_telemetry_is_on(monkeypatch, statsd_calls):
    from app import main

    started, cancelled = [], []

    async def reporter():
        started.append(True)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(main, "_report_queue_depth", reporter)
    # Bounded below: if shutdown stopped cancelling the reporter it would wait out the
    # reporter's hour-long sleep, and a test that hangs reports nothing.
    async def enter_and_leave():
        async with main.lifespan(app):
            await asyncio.sleep(0)

    # `wait` does not cancel on timeout (wait_for does, and cancelling the lifespan would itself
    # cancel the reporter, hiding exactly the bug this test is for).
    lifespan_run = asyncio.create_task(enter_and_leave())
    done, _ = await asyncio.wait({lifespan_run}, timeout=2)
    if not done:
        lifespan_run.cancel()
    assert done, "shutdown did not finish: the reporter was never cancelled"
    assert started == [True]
    assert cancelled == [True]  # shutdown cancelled it rather than leaving it to leak

    started.clear()
    monkeypatch.setattr(main.telemetry, "enabled", lambda: False)
    async with main.lifespan(app):
        await asyncio.sleep(0)
    assert started == []


async def test_heartbeat_gauges_liveness(statsd_calls):
    from worker.main import heartbeat

    await heartbeat({})
    assert statsd_calls.named("worker.heartbeat")[0][2] == 1


def test_both_workers_register_wrapped_jobs_and_a_heartbeat():
    from worker.main import AccountsWorkerSettings, WorkerSettings

    names = [f.name if hasattr(f, "name") else f.__name__ for f in WorkerSettings.functions]
    assert names == ["judge_submission"]
    # `telemetry.job` wraps with functools.wraps, which leaves `__wrapped__` behind.
    assert all(hasattr(f, "__wrapped__") for f in WorkerSettings.functions)
    assert hasattr(AccountsWorkerSettings.functions[0].coroutine, "__wrapped__")
    judge = [c.name for c in WorkerSettings.cron_jobs]
    accounts = [c.name for c in AccountsWorkerSettings.cron_jobs]
    assert "cron:heartbeat:judge" in judge
    assert "cron:heartbeat:accounts" in accounts
    # arq runs a cron job once per scheduled time across ALL workers, keyed by its name, so two
    # workers sharing a name means one of them never fires. Every cron name must be unique across
    # both classes (found live: the judge worker's heartbeat never ran).
    assert len(set(judge + accounts)) == len(judge + accounts)
    assert AccountsWorkerSettings.functions[0].name == "send_account_email"


# --- business metrics -------------------------------------------------------------------------


def test_every_audit_event_is_counted_by_name_and_carries_no_identifiers(statsd_calls):
    from app.audit import audit

    audit("auth.login.failure", email="a@example.com", ip="1.2.3.4", user_id="u1")
    assert statsd_calls.named("audit.event") == [("count", "audit.event", 1, ["event:auth.login.failure"])]


async def test_a_new_signup_is_counted_and_an_existing_email_is_not(client, outbox, statsd_calls):
    creds = {"email": "alice@example.com", "username": "alice", "password": "sunflower-desk-42"}
    assert (await client.post("/api/v1/auth/register", json=creds)).status_code == 202
    assert len(statsd_calls.named("auth.signup")) == 1
    again = dict(creds, username="alice2")  # same email: the response is identical, no account made
    assert (await client.post("/api/v1/auth/register", json=again)).status_code == 202
    assert len(statsd_calls.named("auth.signup")) == 1


async def test_account_email_is_counted_by_kind_only_for_a_real_user(
        client, outbox, session_factory, statsd_calls):
    from app.services import account_service

    creds = {"email": "bob@example.com", "username": "bob", "password": "sunflower-desk-42"}
    await client.post("/api/v1/auth/register", json=creds)
    statsd_calls.calls.clear()  # the fake queue ran the verify job inline; count only what follows
    async with session_factory() as session:
        await account_service.deliver_account_email(session, account_service.EMAIL_RESET, creds["email"])
        await account_service.deliver_account_email(session, account_service.EMAIL_RESET, "nobody@example.com")
        await account_service.deliver_account_email(session, "bogus-kind", creds["email"])
    assert [c[3] for c in statsd_calls.named("account.email.handled")] == [["kind:reset"]]
