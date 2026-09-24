"""Worker job + sweeper tests (DESIGN.md §5.2, §5.3, §5.7).

Unit tests mock run_judgement so they need no Docker; the docker-marked test at
the bottom exercises the whole job against a real container.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.models import EmailToken, Problem, Submission, TestCase
from app.models.email_token import PURPOSE_VERIFY
from worker import judge as judge_mod
from worker import sweeper
from fake_redis import ScriptedRedis
from app.queue import inflight_key
from worker.aggregate import Verdict
from worker.docker_runner import ContainerResult
from worker.judging import _capped, build_verdict_results


class FakeRedis(ScriptedRedis):
    def __init__(self):
        self.deleted = []
        self.store = {}
        self.jobs = set()        # arq job ids that already exist (queued, running or kept result)
        self.enqueued = []       # (function, args, kwargs) of jobs actually created
        self.fail_pipeline = False

    def pipeline(self, transaction=False):
        return _FakePipeline(self)

    async def enqueue_job(self, function, *args, _job_id=None, **kwargs):
        """arq's contract: a job whose id already exists is refused (returns None)."""
        if _job_id in self.jobs:
            return None
        self.jobs.add(_job_id)
        self.enqueued.append((function, args, {"_job_id": _job_id, **kwargs}))
        return object()

    async def delete(self, *keys):
        self.deleted.extend(keys)


class _FakePipeline:
    """Just enough of a redis pipeline for `Queue.missing_judge_jobs`: `exists` calls queue up and
    `execute` answers each from the fake's set of existing job ids."""
    def __init__(self, redis):
        self.redis, self.queued = redis, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def exists(self, *keys):
        self.queued.append(keys)

    async def execute(self):
        if self.redis.fail_pipeline:
            raise ConnectionError("redis unavailable")
        return [sum(key.rsplit(":", 1)[1] in self.redis.jobs for key in keys)
                for keys in self.queued]


class _Case:
    def __init__(self, ordinal, is_sample, inp, expected):
        self.ordinal = ordinal
        self.is_sample = is_sample
        self.input = inp
        self.expected = expected


def test_build_verdict_results_reveals_only_first_failure():
    cases = [_Case(0, True, [1], 1), _Case(1, False, [2], 2),
             _Case(2, False, [3], 3), _Case(3, False, [4], 4)]
    results = [
        {"test_case_id": 0, "status": "passed", "runtime_ms": 1, "output": "x"},
        {"test_case_id": 1, "status": "wrong_answer", "runtime_ms": 1, "output": "y", "stdout": "s"},
        {"test_case_id": 2, "status": "wrong_answer", "runtime_ms": 1, "output": "z"},
        {"test_case_id": 3, "status": "passed", "runtime_ms": 1, "output": "w"},
    ]
    stored = build_verdict_results(results, cases)

    assert stored[0]["output"] == "x"                       # sample kept in full
    # first failing (hidden) case revealed, with input/expected embedded
    assert stored[1]["output"] == "y"
    assert stored[1]["input"] == {"value": [2], "truncated": False}
    assert stored[1]["expected"] == {"value": 2, "truncated": False}
    # a later failing hidden case stays redacted (only one case is exposed)
    assert set(stored[2]) == {"test_case_id", "status", "runtime_ms"}
    # a passing hidden case stays redacted
    assert set(stored[3]) == {"test_case_id", "status", "runtime_ms"}


def test_build_verdict_results_caps_large_reveal():
    big = list(range(5000))  # serializes to ~25 KB
    cases = [_Case(0, False, big, [0, 1])]
    results = [{"test_case_id": 0, "status": "wrong_answer", "runtime_ms": 1, "output": "x"}]
    stored = build_verdict_results(results, cases)
    # Revealed but capped to a truncated string preview so verdict_detail stays
    # bounded; the small `expected` keeps its original shape either way.
    assert stored[0]["input"]["truncated"] is True
    assert isinstance(stored[0]["input"]["value"], str)
    assert len(stored[0]["input"]["value"]) < 2100
    assert stored[0]["expected"] == {"value": [0, 1], "truncated": False}


def test_capped_returns_consistent_shape_for_small_and_large_values():
    """`_capped` never returns the bare value for a small input and a bare
    truncated string for a large one — always `{"value", "truncated"}`."""
    assert _capped([1, 2, 3]) == {"value": [1, 2, 3], "truncated": False}

    big = _capped(list(range(5000)))
    assert big["truncated"] is True
    assert isinstance(big["value"], str)
    assert len(big["value"]) < 2100


async def test_run_judgement_dispatches_sql_language_to_its_image_and_tmpfs(monkeypatch):
    """language="mysql" must resolve to the SQL sandbox image and its larger
    tmpfs (DESIGN.md §13, docs/adr/0002-sql-judge-engine-mysql-vs-mariadb.md)
    — not silently fall through to the python defaults the way an unrecognized
    language would (`_image_for`'s documented fallback)."""
    from worker import judging as judging_mod

    captured = {}

    async def fake_run_in_container(payload, **kwargs):
        captured.update(kwargs)
        return ContainerResult(
            stdout='{"results": []}', stderr="", exit_code=0,
            timed_out=False, stdout_truncated=False)

    monkeypatch.setattr(judging_mod.runner, "run_in_container", fake_run_in_container)

    await judging_mod.run_judgement(
        code="SELECT 1", comparison={"mode": "unordered"}, time_limit_ms=2000,
        memory_limit_mb=192, test_cases=[{"id": 0, "input": ["seed"], "expected": [[1]]}],
        container_name="judge-test-sql", kind="sql", language="mysql",
    )

    assert captured["image"] == judging_mod.settings.judge_image_sql
    assert captured["tmpfs_size_mb"] == 32
    # WALL_CLOCK_SLACK_S (10) + STARTUP_SLACK_S_BY_LANGUAGE["mysql"] (2) + 1 case * 2s
    assert captured["wall_timeout_s"] == pytest.approx(2 + 10 + 2)


async def test_run_judgement_python_language_keeps_existing_defaults(monkeypatch):
    """Companion to the mysql-dispatch test above: an ordinary python problem's
    image/tmpfs/wall-clock math must be unaffected by the new per-language dicts."""
    from worker import judging as judging_mod

    captured = {}

    async def fake_run_in_container(payload, **kwargs):
        captured.update(kwargs)
        return ContainerResult(
            stdout='{"results": []}', stderr="", exit_code=0,
            timed_out=False, stdout_truncated=False)

    monkeypatch.setattr(judging_mod.runner, "run_in_container", fake_run_in_container)

    await judging_mod.run_judgement(
        code="def f(x): return x", comparison={"mode": "exact"}, time_limit_ms=2000,
        memory_limit_mb=256, test_cases=[{"id": 0, "input": [1], "expected": 1}],
        container_name="judge-test-py", function_name="f", language="python",
    )

    assert captured["image"] == judging_mod.settings.judge_image
    assert captured["tmpfs_size_mb"] == 16
    assert captured["wall_timeout_s"] == pytest.approx(2 + 10)


async def _make_pending(session_factory, user_id, problem_id, code="x", is_run=False):
    async with session_factory() as s:
        sub = Submission(user_id=uuid.UUID(user_id), problem_id=uuid.UUID(problem_id),
                         code=code, status="pending", is_run=is_run)
        s.add(sub)
        await s.commit()
        await s.refresh(sub)
        return str(sub.id)


async def test_judge_persists_verdict_and_redacts(session_factory, make_problem, make_user,
                                                  monkeypatch):
    pid, _ = await make_problem(slug="pair-sum")  # ordinal 0 sample, ordinal 1 hidden
    user, _ = await make_user()
    sid = await _make_pending(session_factory, user["id"], pid)

    async def fake_run(**_kwargs):
        return Verdict(status="wrong_answer", runtime_ms=5, passed=1, total=2, results=[
            {"test_case_id": 0, "status": "passed", "runtime_ms": 1, "output": "a",
             "stdout": "", "error": None},
            {"test_case_id": 1, "status": "wrong_answer", "runtime_ms": 5, "output": "b",
             "stdout": "secret", "error": None},
        ])

    monkeypatch.setattr(judge_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(judge_mod, "run_judgement", fake_run)
    redis = FakeRedis()
    redis.store[inflight_key(user["id"], pid)] = sid       # this submission holds the lock

    await judge_mod.judge_submission({"redis": redis}, sid, "submit")

    async with session_factory() as s:
        sub = await s.get(Submission, uuid.UUID(sid))
        assert sub.status == "wrong_answer"
        assert sub.runtime_ms == 5
        assert (sub.verdict_detail["passed"], sub.verdict_detail["total"]) == (1, 2)
        results = sub.verdict_detail["results"]
        assert results[0]["output"] == "a"          # sample visible
        # first failing (hidden) case revealed with embedded input/expected (from make_problem)
        assert results[1]["output"] == "b"
        assert results[1]["input"] == {"value": [[5, 5], 10], "truncated": False}
        assert results[1]["expected"] == {"value": [0, 1], "truncated": False}
    assert redis.deleted  # in-flight lock released


async def _make_operations_problem(session_factory):
    async with session_factory() as s:
        problem = Problem(
            slug="recency-cache", title="Recency Cache", difficulty="medium",
            statement_md="Design an LRU cache.", kind="operations", class_name="LRUCache",
            starter_code="class LRUCache: ...", is_published=True)
        s.add(problem)
        await s.flush()
        s.add(TestCase(problem_id=problem.id, ordinal=0,
                       input=[["LRUCache", "put", "get"], [[2], [1, 1], [1]]],
                       expected=[None, None, 1], is_sample=True))
        await s.commit()
        return str(problem.id)


async def test_judge_submission_passes_kind_and_class_name_to_run_judgement(
        session_factory, make_user, monkeypatch):
    """An operations-kind problem's `kind`/`class_name` must reach `run_judgement`
    — the harness has no other way to know to instantiate a class instead of
    calling a function."""
    pid = await _make_operations_problem(session_factory)
    user, _ = await make_user()
    sid = await _make_pending(session_factory, user["id"], pid, code="class LRUCache: ...")

    captured = {}

    async def fake_run(**kwargs):
        captured.update(kwargs)
        return Verdict(status="accepted", runtime_ms=1, passed=1, total=1, results=[
            {"test_case_id": 0, "status": "passed", "runtime_ms": 1,
             "output": "[null,null,1]", "stdout": "", "error": None},
        ])

    monkeypatch.setattr(judge_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(judge_mod, "run_judgement", fake_run)

    await judge_mod.judge_submission({"redis": FakeRedis()}, sid, "submit")

    assert captured["kind"] == "operations"
    assert captured["class_name"] == "LRUCache"
    async with session_factory() as s:
        assert (await s.get(Submission, uuid.UUID(sid))).status == "accepted"


async def test_judge_missing_submission_is_noop(session_factory, monkeypatch):
    monkeypatch.setattr(judge_mod, "SessionLocal", session_factory)
    await judge_mod.judge_submission({"redis": FakeRedis()}, str(uuid.uuid4()), "submit")


async def test_judge_exception_sets_judge_error(session_factory, make_problem, make_user,
                                                monkeypatch):
    pid, _ = await make_problem()
    user, _ = await make_user()
    sid = await _make_pending(session_factory, user["id"], pid)

    async def boom(**_kwargs):
        raise RuntimeError("docker exploded")

    monkeypatch.setattr(judge_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(judge_mod, "run_judgement", boom)

    await judge_mod.judge_submission({"redis": FakeRedis()}, sid, "submit")
    async with session_factory() as s:
        assert (await s.get(Submission, uuid.UUID(sid))).status == "judge_error"


async def test_sweep_stale_fails_old_running(session_factory, make_problem, make_user,
                                             monkeypatch):
    pid, _ = await make_problem()
    user, _ = await make_user()
    sid = await _make_pending(session_factory, user["id"], pid)
    async with session_factory() as s:
        sub = await s.get(Submission, uuid.UUID(sid))
        sub.status = "running"
        sub.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        await s.commit()

    monkeypatch.setattr(sweeper, "SessionLocal", session_factory)

    await sweeper.sweep_stale({"redis": FakeRedis()})
    async with session_factory() as s:
        assert (await s.get(Submission, uuid.UUID(sid))).status == "judge_error"


async def test_sweep_stale_spares_a_recently_started_run(session_factory, make_problem,
                                                          make_user, monkeypatch):
    """Keying the sweep off `created_at` would count time spent queued against
    the same 5-minute budget as actual run time — a submission that waited in
    a backlog before the worker picked it up could be killed mid-judge despite
    barely having run. Keying off
    `updated_at` (bumped when `judge_submission` sets status="running")
    decouples queue wait from run time: an old `created_at` alone must not
    make a submission that started running recently look stale."""
    pid, _ = await make_problem()
    user, _ = await make_user()
    sid = await _make_pending(session_factory, user["id"], pid)
    async with session_factory() as s:
        sub = await s.get(Submission, uuid.UUID(sid))
        sub.status = "running"
        sub.created_at = datetime.now(timezone.utc) - timedelta(minutes=10)  # queued a while
        # updated_at is left alone (defaults to "now"), simulating the worker
        # having just picked this job up and flipped it to "running".
        await s.commit()

    monkeypatch.setattr(sweeper, "SessionLocal", session_factory)

    await sweeper.sweep_stale({"redis": FakeRedis()})
    async with session_factory() as s:
        assert (await s.get(Submission, uuid.UUID(sid))).status == "running"


async def test_sweep_stale_fails_old_pending_run(session_factory, make_problem, make_user,
                                                  monkeypatch):
    """Runs are swept too: a Run stuck pending/running (worker crash, lost
    job) would otherwise never resolve — unlike a real Submit, a Run has no history row for a user to notice and
    retry from, so nothing would ever move it out of "pending" and the client
    would poll it forever."""
    pid, _ = await make_problem()
    user, _ = await make_user()
    sid = await _make_pending(session_factory, user["id"], pid, is_run=True)
    async with session_factory() as s:
        sub = await s.get(Submission, uuid.UUID(sid))
        sub.updated_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        await s.commit()

    monkeypatch.setattr(sweeper, "SessionLocal", session_factory)

    await sweeper.sweep_stale({"redis": FakeRedis()})
    async with session_factory() as s:
        assert (await s.get(Submission, uuid.UUID(sid))).status == "judge_error"


async def test_sweep_stale_prunes_expired_email_tokens(session_factory, make_user, monkeypatch):
    """Expired email tokens accumulate forever unless swept."""
    user, _ = await make_user()
    now = datetime.now(timezone.utc)
    async with session_factory() as s:
        s.add(EmailToken(user_id=uuid.UUID(user["id"]), token_hash="expired",
                         purpose=PURPOSE_VERIFY, expires_at=now - timedelta(hours=1)))
        s.add(EmailToken(user_id=uuid.UUID(user["id"]), token_hash="live",
                         purpose=PURPOSE_VERIFY, expires_at=now + timedelta(hours=1)))
        await s.commit()

    monkeypatch.setattr(sweeper, "SessionLocal", session_factory)

    await sweeper.sweep_stale({"redis": FakeRedis()})

    async with session_factory() as s:
        remaining = (await s.execute(select(EmailToken.token_hash))).scalars().all()
    assert remaining == ["live"]


CORRECT_PAIR_SUM = (
    "def pair_sum(nums, target):\n"
    "    seen = {}\n"
    "    for i, n in enumerate(nums):\n"
    "        if target - n in seen:\n"
    "            return [seen[target - n], i]\n"
    "        seen[n] = i\n"
)


@pytest.mark.docker
async def test_judge_real_container_end_to_end(session_factory, make_problem, make_user,
                                               monkeypatch):
    pid, _ = await make_problem(slug="pair-sum")
    user, _ = await make_user()
    sid = await _make_pending(session_factory, user["id"], pid, code=CORRECT_PAIR_SUM)
    monkeypatch.setattr(judge_mod, "SessionLocal", session_factory)

    await judge_mod.judge_submission({"redis": FakeRedis()}, sid, "submit")
    async with session_factory() as s:
        assert (await s.get(Submission, uuid.UUID(sid))).status == "accepted"


async def test_a_late_cleanup_does_not_free_a_newer_submissions_lock(
        session_factory, make_problem, make_user, monkeypatch):
    """After a TTL expiry the lock can belong to a *newer* submission. The older job's cleanup
    must not delete it, or a third submit could slip in."""
    pid, _ = await make_problem(slug="pair-sum")
    user, _ = await make_user()
    sid = await _make_pending(session_factory, user["id"], pid)

    async def fake_run(**_kwargs):
        return Verdict(status="accepted", runtime_ms=1, passed=2, total=2, results=[])

    monkeypatch.setattr(judge_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(judge_mod, "run_judgement", fake_run)
    redis = FakeRedis()
    newer = str(uuid.uuid4())
    redis.store[inflight_key(user["id"], pid)] = newer

    await judge_mod.judge_submission({"redis": redis}, sid, "submit")

    assert redis.store[inflight_key(user["id"], pid)] == newer     # untouched


async def test_sweeper_only_frees_a_lock_the_stale_submission_owns(
        session_factory, make_problem, make_user, monkeypatch):
    pid, _ = await make_problem(slug="pair-sum")
    user, _ = await make_user()
    stale = await _make_pending(session_factory, user["id"], pid)
    async with session_factory() as s:
        await s.execute(text("update submissions set updated_at = now() - interval '1 hour'"))
        await s.commit()
    monkeypatch.setattr(sweeper, "SessionLocal", session_factory)

    key = inflight_key(user["id"], pid)

    redis = FakeRedis()
    redis.store[key] = "someone-newer"
    await sweeper.sweep_stale({"redis": redis})
    assert redis.store[key] == "someone-newer"                    # not ours: left alone

    async with session_factory() as s:                            # the row is failed again
        await s.execute(text("update submissions set status='pending', "
                             "updated_at = now() - interval '1 hour'"))
        await s.commit()
    redis.store[key] = stale
    await sweeper.sweep_stale({"redis": redis})
    assert key not in redis.store                                 # ours: freed



async def test_a_job_for_a_row_the_sweeper_already_failed_does_not_resurrect_it(
        session_factory, make_problem, make_user, monkeypatch):
    """The sweep now runs on another worker, so it can fail a row while its job still sits in the
    queue (judge worker at zero, or backlogged). When that job finally runs it must leave the
    row alone: overwriting judge_error would contradict what the user was already shown."""
    pid, _ = await make_problem(slug="pair-sum")
    user, _ = await make_user()
    sid = await _make_pending(session_factory, user["id"], pid)
    async with session_factory() as s:
        (await s.get(Submission, uuid.UUID(sid))).status = "judge_error"
        await s.commit()
    ran = []

    async def fake_run(**_kwargs):
        ran.append(1)
        return Verdict(status="accepted", runtime_ms=1, passed=2, total=2, results=[])

    monkeypatch.setattr(judge_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(judge_mod, "run_judgement", fake_run)
    redis = FakeRedis()
    newer = str(uuid.uuid4())
    redis.store[inflight_key(user["id"], pid)] = newer   # a later submit now holds the lock

    await judge_mod.judge_submission({"redis": redis}, sid, "submit")

    assert ran == []                                            # nothing was judged
    async with session_factory() as s:
        assert (await s.get(Submission, uuid.UUID(sid))).status == "judge_error"
    assert redis.store[inflight_key(user["id"], pid)] == newer  # and the newer lock is untouched


# --- a row whose job was never queued gets one (the submit dual write) ------------------------

async def _age(session_factory, sid, seconds):
    async with session_factory() as s:
        await s.execute(text("update submissions set updated_at = now() - make_interval(secs => :n), "
                             "created_at = now() - make_interval(secs => :n) where id = :i"),
                        {"n": seconds, "i": uuid.UUID(sid)})
        await s.commit()


async def test_the_sweeper_requeues_a_pending_row_whose_job_was_never_queued(
        session_factory, make_problem, make_user, monkeypatch):
    """The API commits the row, then enqueues. A crash in between leaves a pending row with no job;
    it's re-enqueued rather than failed as judge_error for a submission never attempted."""
    pid, _ = await make_problem(slug="pair-sum")
    user, _ = await make_user()
    submit = await _make_pending(session_factory, user["id"], pid)
    run = await _make_pending(session_factory, user["id"], pid, is_run=True)
    for sid in (submit, run):
        await _age(session_factory, sid, 60)
    monkeypatch.setattr(sweeper, "SessionLocal", session_factory)
    redis = FakeRedis()

    await sweeper.sweep_stale({"redis": redis})

    assert {(args, kw["_job_id"]) for _, args, kw in redis.enqueued} == {
        ((submit, "submit"), submit), ((run, "run"), run)}
    assert all(fn == "judge_submission" for fn, _, _ in redis.enqueued)
    async with session_factory() as s:                    # still pending: the job will judge it
        assert (await s.get(Submission, uuid.UUID(submit))).status == "pending"


async def test_the_sweeper_leaves_a_row_that_already_has_a_job(
        session_factory, make_problem, make_user, monkeypatch):
    """A row waiting behind a backlog has a job: enqueueing again must not create a second one.
    arq refuses a duplicate job id, which is the whole guard."""
    pid, _ = await make_problem(slug="pair-sum")
    user, _ = await make_user()
    sid = await _make_pending(session_factory, user["id"], pid)
    await _age(session_factory, sid, 120)
    monkeypatch.setattr(sweeper, "SessionLocal", session_factory)
    redis = FakeRedis()
    redis.jobs.add(sid)                                    # its job is queued

    await sweeper.sweep_stale({"redis": redis})

    assert redis.enqueued == []


async def test_the_sweeper_ignores_young_pending_rows(
        session_factory, make_problem, make_user, monkeypatch):
    """Seconds after a commit the API's own enqueue may simply not have happened yet."""
    pid, _ = await make_problem(slug="pair-sum")
    user, _ = await make_user()
    sid = await _make_pending(session_factory, user["id"], pid)
    await _age(session_factory, sid, 5)
    monkeypatch.setattr(sweeper, "SessionLocal", session_factory)
    redis = FakeRedis()

    await sweeper.sweep_stale({"redis": redis})

    assert redis.enqueued == []


async def test_the_sweeper_fails_a_row_past_its_budget_instead_of_requeueing_it(
        session_factory, make_problem, make_user, monkeypatch):
    pid, _ = await make_problem(slug="pair-sum")
    user, _ = await make_user()
    sid = await _make_pending(session_factory, user["id"], pid)
    await _age(session_factory, sid, 3600)
    monkeypatch.setattr(sweeper, "SessionLocal", session_factory)
    redis = FakeRedis()

    await sweeper.sweep_stale({"redis": redis})

    assert redis.enqueued == []
    async with session_factory() as s:
        assert (await s.get(Submission, uuid.UUID(sid))).status == "judge_error"


async def test_a_lost_job_is_found_among_many_rows_that_have_theirs(
        session_factory, make_problem, make_user, monkeypatch):
    """A backlog of legitimately queued rows must not crowd a genuinely lost job out of the window:
    all of them are checked in one pipelined round trip and only the missing one is enqueued."""
    pid, _ = await make_problem(slug="pair-sum")
    user, _ = await make_user()
    redis = FakeRedis()
    for _ in range(6):
        sid = await _make_pending(session_factory, user["id"], pid)
        await _age(session_factory, sid, 120)
        redis.jobs.add(sid)                                  # queued behind the backlog
    lost = await _make_pending(session_factory, user["id"], pid)
    await _age(session_factory, lost, 60)                   # newest, so last in the window
    monkeypatch.setattr(sweeper, "SessionLocal", session_factory)

    await sweeper.sweep_stale({"redis": redis})

    assert [args for _, args, _ in redis.enqueued] == [(lost, "submit")]


async def test_a_redis_error_releasing_a_stale_lock_does_not_abort_the_sweep(
        session_factory, make_problem, make_user, monkeypatch):
    """The stale row is still failed (committed before any Redis call) and the rest of the
    sweep still runs; the lock is left to its TTL."""
    pid, _ = await make_problem(slug="pair-sum")
    user, _ = await make_user()
    sid = await _make_pending(session_factory, user["id"], pid)
    async with session_factory() as s:
        await s.execute(text("update submissions set updated_at = now() - interval '1 hour'"))
        s.add(EmailToken(user_id=uuid.UUID(user["id"]), token_hash="expired",
                         purpose=PURPOSE_VERIFY,
                         expires_at=datetime.now(timezone.utc) - timedelta(hours=1)))
        await s.commit()
    monkeypatch.setattr(sweeper, "SessionLocal", session_factory)

    class LockDown(FakeRedis):
        async def eval(self, *_args):
            raise ConnectionError("redis timed out")

    await sweeper.sweep_stale({"redis": LockDown()})         # does not raise

    async with session_factory() as s:
        assert (await s.get(Submission, uuid.UUID(sid))).status == "judge_error"
        hashes = (await s.execute(select(EmailToken.token_hash))).scalars().all()
        assert "expired" not in hashes


async def test_a_redis_error_while_requeueing_does_not_abort_the_rest_of_the_sweep(
        session_factory, make_problem, make_user, monkeypatch):
    """A Redis hiccup during the re-enqueue must not skip token pruning and the final commit."""
    pid, _ = await make_problem(slug="pair-sum")
    user, _ = await make_user()
    sid = await _make_pending(session_factory, user["id"], pid)
    await _age(session_factory, sid, 60)
    async with session_factory() as s:
        s.add(EmailToken(user_id=uuid.UUID(user["id"]), token_hash="expired",
                         purpose=PURPOSE_VERIFY,
                         expires_at=datetime.now(timezone.utc) - timedelta(hours=1)))
        await s.commit()
    monkeypatch.setattr(sweeper, "SessionLocal", session_factory)
    redis = FakeRedis()
    redis.fail_pipeline = True

    await sweeper.sweep_stale({"redis": redis})              # does not raise

    async with session_factory() as s:
        hashes = (await s.execute(select(EmailToken.token_hash))).scalars().all()
        assert "expired" not in hashes                       # the rest of the sweep still ran
        assert (await s.get(Submission, uuid.UUID(sid))).status == "pending"   # retried next pass
