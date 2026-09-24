"""Test fixtures: a fresh schema per test against a real Postgres (DESIGN.md §10.2).

Each test gets its own engine bound to the running event loop and a freshly
(re)created schema, so tests are fully isolated with no cross-test bleed.
"""
import os
import re

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import breached_passwords
from app.db import get_session
from app.main import app
from app.models import Base, Problem, Solution, Submission, TestCase, User
from app.queue import get_queue
from fake_redis import ScriptedRedis
from worker import accounts as worker_accounts
from app.security import create_access_token, hash_password


class FakeRedis(ScriptedRedis):
    def __init__(self):
        self.deleted: list[str] = []
        self.store: dict[str, str] = {}

    async def delete(self, *keys):
        self.deleted.extend(keys)
        for key in keys:
            self.store.pop(key, None)
        return len(keys)

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        return True


class FakeQueue:
    """In-memory stand-in for the arq queue (DESIGN.md §10.2: fake the queue)."""
    def __init__(self):
        self.enqueued: list[tuple[str, str]] = []
        self.emails: list[tuple[str, str]] = []
        self.fail_email_enqueue = False
        self.deliver_emails = True  # run the worker job inline so `outbox` fills
        self.delivery_errors: list[Exception] = []  # inline-job failures; see `queue` fixture
        self.locks: dict = {}  # (user, problem) -> owner token
        self.rate_counts: dict = {}
        self.login_failures: dict = {}
        self.login_locks: dict = {}
        self.fail_lock = False
        self.released: list = []  # every release attempt: ((user, problem), owner)
        self.fail_enqueue = False
        self.fail_ping = False
        self.redis = FakeRedis()

    async def enqueue_judge(self, submission_id, mode):
        if self.fail_enqueue:
            raise RuntimeError("redis unavailable")
        self.enqueued.append((submission_id, mode))
        return True

    async def enqueue_email(self, kind, email):
        if self.fail_email_enqueue:
            raise RuntimeError("redis unavailable")
        self.emails.append((kind, email))
        if self.deliver_emails:
            # The API swallows enqueue errors by design (best-effort), which would also
            # hide a bug in the job itself, so record it here; the `queue` fixture
            # fails the test at teardown with the real traceback.
            try:
                await worker_accounts.send_account_email({}, kind, email)
            except Exception as exc:  # noqa: BLE001
                self.delivery_errors.append(exc)

    async def acquire_inflight(self, user_id, problem_id, owner):
        if self.fail_lock:
            raise RuntimeError("redis unavailable")
        key = (str(user_id), str(problem_id))
        if key in self.locks:
            return False
        self.locks[key] = owner
        return True

    async def release_inflight(self, user_id, problem_id, owner):
        key = (str(user_id), str(problem_id))
        self.released.append((key, owner))
        if self.locks.get(key) == owner:
            del self.locks[key]
            return True
        return False

    async def within_rate_limit(self, user_id, action, limit):
        key = (str(user_id), action)
        self.rate_counts[key] = self.rate_counts.get(key, 0) + 1
        return self.rate_counts[key] <= limit

    async def incr_login_failures(self, email, window_seconds):
        e = email.lower()
        self.login_failures[e] = self.login_failures.get(e, 0) + 1
        return self.login_failures[e]

    async def set_login_lock(self, email, seconds):
        self.login_locks[email.lower()] = seconds

    async def get_login_lock(self, email):
        return self.login_locks.get(email.lower(), 0)

    async def clear_login_failures(self, email):
        self.login_failures.pop(email.lower(), None)
        self.login_locks.pop(email.lower(), None)

    async def get_cached_percentile(self, problem_id, runtime_ms):
        raw = await self.redis.get(f"percentile:{problem_id}:{runtime_ms}")
        return float(raw) if raw is not None else None

    async def set_cached_percentile(self, problem_id, runtime_ms, value):
        await self.redis.set(f"percentile:{problem_id}:{runtime_ms}", value)

    async def ping(self):
        if self.fail_ping:
            raise RuntimeError("redis unavailable")
        return True

    async def queue_depth(self):
        return getattr(self, "depth", 0)

    async def accounts_queue_stats(self):
        return getattr(self, "accounts_depth", 0), getattr(self, "accounts_oldest_age", None)

    async def release_alert(self, name):
        getattr(self, "claimed_alerts", set()).discard(name)

    async def claim_alert(self, name, cooldown_seconds):
        claimed = getattr(self, "claimed_alerts", set())
        self.claimed_alerts = claimed
        if name in claimed:
            return False
        claimed.add(name)
        return True

@pytest.fixture(autouse=True)
def no_breach_network(monkeypatch):
    """Keep the suite off the network: breach screening is off by default, and
    any HIBP request that happens anyway hits a transport that fails the test
    loudly (an AssertionError isn't an httpx error, so fail-open can't swallow it).
    Tests of the screening itself re-enable it with their own mock transport."""
    def refuse(request):
        raise AssertionError(f"unexpected network call in tests: {request.url}")

    monkeypatch.setattr(breached_passwords.settings, "breached_password_check", False)
    monkeypatch.setattr(breached_passwords, "_transport", httpx.MockTransport(refuse))


TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://app:app@localhost:5432/shikomi_test")


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(TEST_DB_URL)
    async with eng.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
def queue(session_factory, monkeypatch):
    # The API only enqueues account emails; the worker sends them. FakeQueue runs
    # that job inline against the test DB so tests can still read the `outbox`.
    monkeypatch.setattr(worker_accounts, "SessionLocal", session_factory)
    fake = FakeQueue()
    yield fake
    assert not fake.delivery_errors, f"inline email job failed: {fake.delivery_errors!r}"


@pytest_asyncio.fixture
def outbox():
    """The console email backend's in-memory outbox, cleared for isolation."""
    from app.email import outbox as box
    box.clear()
    yield box
    box.clear()


@pytest_asyncio.fixture
async def client(session_factory, queue):
    async def override_get_session():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_queue] = lambda: queue
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
def signup(client, outbox):
    """Register through the API *and* follow the emailed verify link.

    Unverified accounts can't log in (DESIGN.md §4.1: that's what keeps
    register from being an email-enumeration oracle), so any test that signs up
    via the API and then logs in needs both steps.
    """
    async def _signup(creds):
        r = await client.post("/api/v1/auth/register", json=creds)
        assert r.status_code == 202, r.text
        token = re.search(r"verify-email\?token=([A-Za-z0-9_-]+)", outbox[-1].body).group(1)
        verified = await client.post("/api/v1/auth/verify-email", json={"token": token})
        assert verified.status_code == 200, verified.text
    return _signup


@pytest_asyncio.fixture
def make_user(session_factory):
    """Factory that inserts a user directly and returns (user_dict, auth_headers)."""
    async def _make(email="user@example.com", username="user", password="password123",
                    verified=True):
        async with session_factory() as session:
            user = User(email=email, username=username,
                        password_hash=hash_password(password),
                        email_verified=verified)
            session.add(user)
            await session.commit()
            await session.refresh(user)
            headers = {"Authorization": f"Bearer {create_access_token(user)}"}
            return {"id": str(user.id), "email": email, "username": username,
                    "password": password}, headers
    return _make


@pytest_asyncio.fixture
async def user_headers(make_user):
    _, headers = await make_user()
    return headers


@pytest_asyncio.fixture
def make_problem(session_factory):
    """Insert a problem with one sample + one hidden test case (and optional solution)."""
    async def _make(slug="pair-sum", published=True, with_solutions=True,
                    title="Pair Sum", collections=None, tags=None):
        async with session_factory() as s:
            problem = Problem(
                slug=slug, title=title, difficulty="easy", statement_md="Add two.",
                function_name="pair_sum", starter_code="def pair_sum(nums, target): ...",
                params=[{"name": "nums", "type": "List[int]"},
                        {"name": "target", "type": "int"}],
                comparison={"mode": "exact"}, is_published=published,
                tags=tags or ["array"], collections=collections or [])
            s.add(problem)
            await s.flush()
            s.add(TestCase(problem_id=problem.id, ordinal=0, input=[[1, 6], 7],
                           expected=[0, 1], is_sample=True))
            s.add(TestCase(problem_id=problem.id, ordinal=1, input=[[5, 5], 10],
                           expected=[0, 1], is_sample=False))  # hidden
            if with_solutions:
                s.add(Solution(problem_id=problem.id, ordinal=0, title="Hash Map",
                               intuition_md="Use a dict.", algorithm_md="Scan once.",
                               code="def pair_sum(): ...",
                               time_complexity="O(n)", space_complexity="O(n)"))
            await s.commit()
            return str(problem.id), slug
    return _make


@pytest_asyncio.fixture
def make_submission(session_factory):
    async def _make(user_id, problem_id, status="accepted", is_run=False, runtime_ms=None):
        async with session_factory() as s:
            sub = Submission(user_id=user_id, problem_id=problem_id, code="x",
                             status=status, is_run=is_run, runtime_ms=runtime_ms)
            s.add(sub)
            await s.commit()
            await s.refresh(sub)
            return str(sub.id)
    return _make
