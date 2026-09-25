"""Submission endpoint tests with a faked queue (DESIGN.md §4.3, §10.2)."""
import uuid

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import APIError
from app.config import get_settings
from app.models import Submission, User
from app.services import submission_service

_settings = get_settings()

SUBMIT = "/api/v1/submissions"
RUN = "/api/v1/run"
MISSING = "/api/v1/submissions/00000000-0000-0000-0000-000000000000"
CODE = "def pair_sum(nums, target): return [0, 1]"


async def test_submit_enqueues_and_returns_pending(client, queue, make_problem, make_user):
    pid, _ = await make_problem(slug="pair-sum")
    _, headers = await make_user()
    r = await client.post(SUBMIT, headers=headers, json={"problem_id": pid, "code": CODE})
    assert r.status_code == 202
    assert r.json()["status"] == "pending"
    assert queue.enqueued == [(r.json()["id"], "submit")]


async def test_submit_requires_auth(client, make_problem):
    pid, _ = await make_problem()
    assert (await client.post(SUBMIT, json={"problem_id": pid, "code": CODE})).status_code == 401


async def test_unverified_user_cannot_judge(client, make_problem, make_user):
    pid, _ = await make_problem()
    _, headers = await make_user(verified=False)
    for path in (SUBMIT, RUN):
        r = await client.post(path, headers=headers, json={"problem_id": pid, "code": CODE})
        assert r.status_code == 403
        assert r.json()["code"] == "EMAIL_NOT_VERIFIED"


async def test_submit_unpublished_problem_404(client, make_problem, user_headers):
    pid, _ = await make_problem(slug="draft", published=False)
    r = await client.post(SUBMIT, headers=user_headers, json={"problem_id": pid, "code": CODE})
    assert r.status_code == 404


async def test_in_flight_lock_blocks_second_submit(client, make_problem, make_user):
    pid, _ = await make_problem()
    _, headers = await make_user()
    body = {"problem_id": pid, "code": CODE}
    assert (await client.post(SUBMIT, headers=headers, json=body)).status_code == 202
    second = await client.post(SUBMIT, headers=headers, json=body)
    assert second.status_code == 429
    assert second.json()["code"] == "SUBMISSION_IN_FLIGHT"


async def test_run_takes_no_inflight_lock_and_is_flagged(client, queue, make_problem, make_user):
    pid, _ = await make_problem()
    _, headers = await make_user()
    body = {"problem_id": pid, "code": CODE}
    r1 = await client.post(RUN, headers=headers, json=body)
    r2 = await client.post(RUN, headers=headers, json=body)  # no in-flight lock for run
    assert r1.status_code == r2.status_code == 202
    assert not queue.locks
    assert all(mode == "run" for _, mode in queue.enqueued)


async def test_poll_returns_submission(client, make_problem, make_user):
    pid, _ = await make_problem()
    _, headers = await make_user()
    sid = (await client.post(SUBMIT, headers=headers,
                             json={"problem_id": pid, "code": CODE})).json()["id"]
    r = await client.get(f"/api/v1/submissions/{sid}", headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    assert r.json()["is_run"] is False


async def test_poll_enforces_ownership(client, make_problem, make_user):
    pid, _ = await make_problem()
    _, owner = await make_user(email="owner@x.com", username="owner")
    _, other = await make_user(email="other@x.com", username="other")
    sid = (await client.post(SUBMIT, headers=owner,
                             json={"problem_id": pid, "code": CODE})).json()["id"]
    assert (await client.get(f"/api/v1/submissions/{sid}", headers=other)).status_code == 403
    assert (await client.get(f"/api/v1/submissions/{sid}", headers=owner)).status_code == 200


async def test_poll_missing_404(client, user_headers):
    assert (await client.get(MISSING, headers=user_headers)).status_code == 404


async def test_oversized_code_rejected(client, make_problem, make_user):
    pid, _ = await make_problem()
    _, headers = await make_user()
    code = "🚀" * 20000  # 20k chars, 80k bytes — passes the char guard, fails the byte check
    r = await client.post(SUBMIT, headers=headers, json={"problem_id": pid, "code": code})
    assert r.status_code == 413
    assert r.json()["code"] == "CODE_TOO_LARGE"


async def test_redis_down_on_lock_returns_503(client, queue, make_problem, make_user):
    queue.fail_lock = True
    pid, _ = await make_problem()
    _, headers = await make_user()
    r = await client.post(SUBMIT, headers=headers, json={"problem_id": pid, "code": CODE})
    assert r.status_code == 503
    assert r.json()["code"] == "QUEUE_UNAVAILABLE"


async def test_enqueue_failure_marks_judge_error(client, queue, session_factory,
                                                 make_problem, make_user):
    queue.fail_enqueue = True
    pid, _ = await make_problem()
    _, headers = await make_user()
    r = await client.post(RUN, headers=headers, json={"problem_id": pid, "code": CODE})
    assert r.status_code == 503
    # The row was written before the (failed) enqueue and must be marked judge_error (§5.7).
    async with session_factory() as s:
        sub = (await s.execute(select(Submission))).scalar_one()
        assert sub.status == "judge_error"


async def test_submit_enqueue_failure_releases_lock(client, queue, session_factory,
                                                    make_problem, make_user):
    queue.fail_enqueue = True
    pid, _ = await make_problem()
    _, headers = await make_user()
    r = await client.post(SUBMIT, headers=headers, json={"problem_id": pid, "code": CODE})
    assert r.status_code == 503
    async with session_factory() as s:
        assert (await s.execute(select(Submission))).scalar_one().status == "judge_error"


async def test_write_failure_releases_inflight_lock(session_factory, queue, make_problem, make_user):
    """A commit failure on the row write must release the lock too — otherwise a
    transient DB error wedges the user out for the full in-flight TTL with no row
    to even show for it."""
    pid, _ = await make_problem()
    user, _ = await make_user()
    problem_id, user_id = uuid.UUID(pid), uuid.UUID(user["id"])

    async def boom():
        raise RuntimeError("db write failed")

    async with session_factory() as s:
        s.commit = boom
        with pytest.raises(RuntimeError):
            await submission_service.create_submission(
                s, queue, await s.get(User, user_id), problem_id, CODE, mode="submit")

    assert [key for key, _ in queue.released] == [(str(user_id), str(problem_id))]
    assert queue.enqueued == []  # never got past the failed write
    async with session_factory() as s:
        assert (await s.execute(select(Submission))).scalar_one_or_none() is None


async def test_enqueue_and_judge_error_write_both_failing_still_503s_and_frees_the_lock(
        session_factory, queue, make_problem, make_user):
    """Redis down *and* the judge_error commit failing: the caller still gets the 503 (not a
    500) and the lock is released, so a retry isn't blocked for the in-flight TTL. The row
    stays pending, and the sweeper re-enqueues it later."""
    queue.fail_enqueue = True
    pid, _ = await make_problem()
    user, _ = await make_user()
    problem_id, user_id = uuid.UUID(pid), uuid.UUID(user["id"])

    async with session_factory() as s:
        real_commit, calls = s.commit, []

        async def commit_once_then_fail():
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("db write failed")
            await real_commit()

        s.commit = commit_once_then_fail
        with pytest.raises(APIError) as exc:
            await submission_service.create_submission(
                s, queue, await s.get(User, user_id), problem_id, CODE, mode="submit")

    assert exc.value.status_code == 503
    assert [key for key, _ in queue.released] == [(str(user_id), str(problem_id))]
    async with session_factory() as s:
        assert (await s.execute(select(Submission))).scalar_one().status == "pending"


async def test_an_enqueue_error_after_a_worker_claimed_the_job_leaves_it_alone(
        client, queue, session_factory, make_problem, make_user):
    """Redis stored the job but the reply timed out, and a worker already flipped the row to
    `running`. The API must not overwrite that with judge_error or free the worker's lock."""
    pid, _ = await make_problem()
    _, headers = await make_user()

    async def stored_then_timed_out(submission_id, mode):
        async with session_factory() as s:          # the worker's claim lands first
            await s.execute(update(Submission).where(Submission.id == uuid.UUID(submission_id))
                            .values(status="running"))
            await s.commit()
        raise TimeoutError("reply lost")

    queue.enqueue_judge = stored_then_timed_out
    r = await client.post(SUBMIT, headers=headers, json={"problem_id": pid, "code": CODE})
    assert r.status_code == 503
    async with session_factory() as s:
        assert (await s.execute(select(Submission))).scalar_one().status == "running"
    assert queue.released == []                          # the worker's lock is untouched


async def test_run_is_rate_limited(client, make_problem, make_user):
    limit = _settings.run_rate_limit_per_minute
    pid, _ = await make_problem()
    _, headers = await make_user()
    body = {"problem_id": pid, "code": CODE}
    for _ in range(limit):
        assert (await client.post(RUN, headers=headers, json=body)).status_code == 202
    blocked = await client.post(RUN, headers=headers, json=body)
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "RATE_LIMITED"
    assert "Retry-After" in blocked.headers


async def test_runtime_percentile(client, make_problem, make_user, make_submission):
    pid, _ = await make_problem(slug="pair-sum")
    user, headers = await make_user()
    slow = await make_submission(user["id"], pid, status="accepted", runtime_ms=30.0)
    await make_submission(user["id"], pid, status="accepted", runtime_ms=20.0)
    fast = await make_submission(user["id"], pid, status="accepted", runtime_ms=10.0)

    r_fast = await client.get(f"/api/v1/submissions/{fast}", headers=headers)
    assert r_fast.json()["runtime_percentile"] == 100.0  # fastest of 3 → beats both others
    r_slow = await client.get(f"/api/v1/submissions/{slow}", headers=headers)
    assert r_slow.json()["runtime_percentile"] == 0.0  # slowest → beats neither other


async def test_runtime_percentile_none_when_sole_accepted_submission(
        client, make_problem, make_user, make_submission):
    """With nobody else to compare against, the percentile is None (the
    frontend shows "you're the first!") rather than a hollow 100%."""
    pid, _ = await make_problem(slug="pair-sum")
    user, headers = await make_user()
    sid = await make_submission(user["id"], pid, status="accepted", runtime_ms=15.0)
    r = await client.get(f"/api/v1/submissions/{sid}", headers=headers)
    assert r.json()["runtime_percentile"] is None


async def test_runtime_percentile_is_cached(session_factory, queue, make_problem, make_user,
                                            make_submission, monkeypatch):
    """The two COUNT(*) scans behind runtime_percentile only run on a cache miss —
    a repeat lookup for the same (problem, runtime) is served from cache."""
    pid, _ = await make_problem(slug="pair-sum")
    user, _ = await make_user()
    # Two submissions: a single accepted submission short-circuits to None after
    # just one COUNT(*) (nothing to compare against), which wouldn't exercise the
    # two-scan/cache path this test is about.
    await make_submission(user["id"], pid, status="accepted", runtime_ms=10.0)
    await make_submission(user["id"], pid, status="accepted", runtime_ms=20.0)

    calls = 0
    original_scalar = AsyncSession.scalar

    async def counting_scalar(self, *a, **kw):
        nonlocal calls
        calls += 1
        return await original_scalar(self, *a, **kw)

    monkeypatch.setattr(AsyncSession, "scalar", counting_scalar)

    async with session_factory() as s:
        first = await submission_service.runtime_percentile(s, queue, uuid.UUID(pid), 20.0)
    assert calls == 2  # cache miss: both COUNT(*) scans ran

    async with session_factory() as s:
        second = await submission_service.runtime_percentile(s, queue, uuid.UUID(pid), 20.0)
    assert second == first
    assert calls == 2  # cache hit: no additional scans


async def test_percentile_none_when_not_accepted(client, make_problem, make_user, make_submission):
    pid, _ = await make_problem(slug="pair-sum")
    user, headers = await make_user()
    wa = await make_submission(user["id"], pid, status="wrong_answer", runtime_ms=5.0)
    r = await client.get(f"/api/v1/submissions/{wa}", headers=headers)
    assert r.json()["runtime_percentile"] is None


async def test_delete_submission(client, make_problem, make_user, make_submission):
    pid, _ = await make_problem(slug="pair-sum")
    owner, owner_h = await make_user(email="o@x.com", username="owner")
    _, other_h = await make_user(email="e@x.com", username="other")
    sid = await make_submission(owner["id"], pid, status="accepted", runtime_ms=5.0)

    # a non-owner cannot delete it
    assert (await client.delete(f"/api/v1/submissions/{sid}", headers=other_h)).status_code == 403
    # the owner can
    assert (await client.delete(f"/api/v1/submissions/{sid}", headers=owner_h)).status_code == 204
    assert (await client.get(f"/api/v1/submissions/{sid}", headers=owner_h)).status_code == 404


async def test_runtime_distribution(client, make_problem, make_user, make_submission):
    pid, _ = await make_problem(slug="pair-sum")
    user, headers = await make_user()
    for rt in (10.0, 20.0, 20.0, 30.0):
        await make_submission(user["id"], pid, status="accepted", runtime_ms=rt)
    sid = await make_submission(user["id"], pid, status="accepted", runtime_ms=15.0)

    r = await client.get(f"/api/v1/submissions/{sid}/distribution", headers=headers)
    assert r.status_code == 200
    d = r.json()
    assert d["total"] == 5
    assert sum(d["buckets"]) == 5
    assert (d["lo"], d["hi"]) == (10.0, 30.0)


async def test_list_user_submissions(client, make_problem, make_user, make_submission):
    pid, _ = await make_problem(slug="pair-sum")
    user, headers = await make_user()
    await make_submission(user["id"], pid, status="accepted", runtime_ms=10.0)
    await make_submission(user["id"], pid, status="wrong_answer")
    await make_submission(user["id"], pid, status="accepted", is_run=True)  # runs excluded

    r = await client.get("/api/v1/problems/pair-sum/submissions", headers=headers)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    assert {i["status"] for i in items} == {"accepted", "wrong_answer"}


def test_stats_index_exists():
    """The (problem_id, is_run, status) index the stats queries rely on."""
    index_columns = {tuple(c.name for c in idx.columns) for idx in Submission.__table__.indexes}
    assert ("problem_id", "is_run", "status") in index_columns


async def test_submit_is_rate_limited(client, queue, make_problem, make_user):
    limit = _settings.submit_rate_limit_per_minute
    pid, _ = await make_problem()
    _, headers = await make_user()
    body = {"problem_id": pid, "code": CODE}
    for _ in range(limit):
        queue.locks.clear()  # simulate the worker releasing the in-flight lock each time
        assert (await client.post(SUBMIT, headers=headers, json=body)).status_code == 202
    queue.locks.clear()
    blocked = await client.post(SUBMIT, headers=headers, json=body)
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "RATE_LIMITED"


async def test_the_lock_is_owned_by_the_submission_it_was_taken_for(
        client, queue, make_problem, make_user):
    """The owner token is the submission's id, so the worker and sweeper (who only know the
    row) can release exactly their own lock."""
    pid, _ = await make_problem()
    _, headers = await make_user()
    r = await client.post(SUBMIT, headers=headers, json={"problem_id": pid, "code": CODE})
    assert r.status_code == 202
    assert list(queue.locks.values()) == [r.json()["id"]]
