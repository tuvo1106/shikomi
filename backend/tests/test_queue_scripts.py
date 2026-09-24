"""The Lua scripts in `app/queue.py`, against a real Redis (the fakes can't run Lua).

Skipped when no Redis is reachable (the in-memory fakes cover the callers). Uses DB 2 and
random ids, so it never touches dev data and needs no cleanup beyond the keys' own TTLs.
"""
import os
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.queue import Queue, inflight_key

URL = os.environ.get("QUEUE_TEST_REDIS_URL", "redis://localhost:6379/2")


@pytest_asyncio.fixture
async def real_queue():
    client = aioredis.from_url(URL)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("no Redis reachable for the script tests")
    yield Queue(client), client
    await client.aclose()


async def test_only_the_owner_can_release_the_inflight_lock(real_queue):
    queue, client = real_queue
    user, problem = uuid.uuid4(), uuid.uuid4()
    assert await queue.acquire_inflight(user, problem, "first")
    assert not await queue.acquire_inflight(user, problem, "second")   # held

    assert not await queue.release_inflight(user, problem, "second")   # not the owner: no-op
    assert await client.get(inflight_key(user, problem)) == b"first"

    assert await queue.release_inflight(user, problem, "first")
    assert await client.get(inflight_key(user, problem)) is None
    assert await queue.acquire_inflight(user, problem, "third")        # free again
    await queue.release_inflight(user, problem, "third")


async def test_a_stale_release_cannot_free_a_newer_holders_lock(real_queue):
    """The bug: job A outlives the TTL, B takes the lock, A's cleanup deletes B's."""
    queue, client = real_queue
    user, problem = uuid.uuid4(), uuid.uuid4()
    await queue.acquire_inflight(user, problem, "A")
    await client.delete(inflight_key(user, problem))                   # A's TTL expired
    assert await queue.acquire_inflight(user, problem, "B")

    assert not await queue.release_inflight(user, problem, "A")        # A's late cleanup
    assert not await queue.acquire_inflight(user, problem, "C")        # B still holds it
    await queue.release_inflight(user, problem, "B")


async def test_login_failure_counter_always_gets_a_ttl(real_queue):
    queue, client = real_queue
    email = f"{uuid.uuid4()}@x.com"
    assert await queue.incr_login_failures(email, 600) == 1
    assert await queue.incr_login_failures(email, 600) == 2
    assert 0 < await client.ttl(f"authfail:{email}") <= 600


async def test_a_counter_leaked_without_a_ttl_heals_on_the_next_hit(real_queue):
    """A crash between INCR and EXPIRE would leave `authfail:` immortal; the script re-arms
    the expiry whenever it finds none."""
    queue, client = real_queue
    email = f"{uuid.uuid4()}@x.com"
    await client.set(f"authfail:{email}", 3)                           # no TTL: the leaked state
    assert await client.ttl(f"authfail:{email}") == -1

    assert await queue.incr_login_failures(email, 600) == 4
    assert 0 < await client.ttl(f"authfail:{email}") <= 600


async def test_rate_limit_counts_and_expires(real_queue):
    queue, client = real_queue
    user = uuid.uuid4()
    assert [await queue.within_rate_limit(user, "submit", 2) for _ in range(3)] == [
        True, True, False]


async def test_arq_refuses_a_second_job_with_the_same_id():
    """The sweeper's safe re-enqueue rests on this arq behaviour, which the fakes only assume:
    `enqueue_judge` uses the submission id as the job id, so enqueueing twice makes one job."""
    from arq import create_pool
    from arq.connections import RedisSettings

    settings = RedisSettings.from_dsn(URL)
    try:
        pool = await create_pool(settings)
        await pool.ping()
    except Exception:
        pytest.skip("no Redis reachable for the script tests")
    sub_id = str(uuid.uuid4())
    try:
        queue = Queue(pool)
        assert await queue.enqueue_judge(sub_id, "submit") is True
        assert await queue.enqueue_judge(sub_id, "submit") is False       # already queued
        assert await pool.zcard("arq:queue") >= 1
    finally:
        await pool.zrem("arq:queue", sub_id)
        await pool.delete(f"arq:job:{sub_id}")
        await pool.aclose()


async def test_missing_judge_jobs_reports_only_ids_arq_has_no_job_for(real_queue):
    """One pipelined check over the two keys arq's own duplicate check reads."""
    from arq import create_pool
    from arq.connections import RedisSettings

    _, client = real_queue
    pool = await create_pool(RedisSettings.from_dsn(URL))
    queued, lost = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        queue = Queue(pool)
        assert await queue.enqueue_judge(queued, "submit") is True
        assert await queue.missing_judge_jobs([queued, lost]) == [lost]
        assert await queue.missing_judge_jobs([]) == []
    finally:
        await pool.zrem("arq:queue", queued)
        await pool.delete(f"arq:job:{queued}")
        await pool.aclose()
