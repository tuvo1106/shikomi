"""Judge job lifecycle: the seed-time budget, cancellation, and the cross-replica-safe
sweep (DESIGN.md §5.2, §5.7; `app/judge_budget.py`).

Three failure modes, each easy to miss:

* a problem whose run can't fit arq's `job_timeout` (so arq cancels the job before our own kill);
* the cancellation itself (`CancelledError` is a `BaseException`: `except Exception` skips it);
* one worker replica's sweep reaping another replica's live sandbox.
"""
import asyncio
import threading
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app import judge_budget as jb
from app.schemas.problem import ProblemFile
from app.queue import inflight_key
from fake_redis import ScriptedRedis
from app.models import Submission
from worker import docker_runner, k8s_runner
from worker import judge as judge_mod

BODY = {
    "title": "Budgeted", "difficulty": "easy", "statement_md": "x", "function_name": "f",
    "starter_code": "def f(): ...", "params": [], "comparison": {"mode": "exact"},
    "is_published": False, "tags": [], "time_limit_ms": 2000,
}


# --- the shared budget -------------------------------------------------------------------

def test_wall_budget_matches_what_the_runner_uses():
    assert jb.wall_budget_s(10, 2000, "python") == 10 * 2 + jb.WALL_CLOCK_SLACK_S
    assert jb.wall_budget_s(1, 2000, "mysql") == 2 + jb.WALL_CLOCK_SLACK_S + 2  # sql cold start


def test_the_job_timeout_bound_is_exact_at_its_edge():
    limit_ms = 2000
    most = jb.max_cases_within_job_timeout(limit_ms)
    assert jb.fits_job_timeout(most, limit_ms)
    assert not jb.fits_job_timeout(most + 1, limit_ms)


@pytest.mark.parametrize("limit_ms,language", [(500, "python"), (2000, "python"), (6000, "js"),
                                               (30_000, "python"), (2000, "mysql")])
def test_max_cases_always_agrees_with_fits_job_timeout(limit_ms, language):
    most = jb.max_cases_within_job_timeout(limit_ms, language)
    assert most == 0 or jb.fits_job_timeout(most, limit_ms, language)
    assert not jb.fits_job_timeout(most + 1, limit_ms, language)


def test_the_whole_seed_catalog_fits_with_room_to_spare():
    """The check must not reject a bundled problem: every starter fits the job timeout."""
    import glob
    import json
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2] / "seed" / "problems"
    for path in glob.glob(str(root / "*.json")):
        d = json.load(open(path))
        assert jb.fits_job_timeout(len(d.get("test_cases", [])), d.get("time_limit_ms", 2000),
                                   d.get("language", "python")), path


def test_a_live_sandbox_can_never_be_older_than_the_sweep_threshold():
    """The sweep's safety argument: every live job is cancelled at the job timeout, so no
    legitimate sandbox outlives it by more than the (generous) margin the sweep allows."""
    assert jb.MAX_LIVE_SANDBOX_AGE_S > jb.JUDGE_JOB_TIMEOUT_SECONDS


def test_the_worker_uses_the_shared_job_timeout():
    from worker.main import WorkerSettings
    assert WorkerSettings.job_timeout == jb.JUDGE_JOB_TIMEOUT_SECONDS


# --- a problem file that can't finish is refused ------------------------------------------

def _file(n, **extra):
    cases = [{"ordinal": i, "input": [], "expected": None, "is_sample": i == 0} for i in range(n)]
    return {**BODY, "test_cases": cases, **extra}


def test_a_problem_file_at_the_budget_edge_loads():
    ProblemFile.model_validate(_file(jb.max_cases_within_job_timeout(2000)))


def test_a_problem_file_over_the_budget_is_refused_with_the_limit():
    """Checked on the file itself (cases x limit together), before seed writes anything."""
    most = jb.max_cases_within_job_timeout(2000)
    with pytest.raises(ValidationError) as exc:
        ProblemFile.model_validate(_file(most + 1))
    assert f"at most {most} cases" in str(exc.value)  # actionable

    with pytest.raises(ValidationError):              # 60 x 30s = 30 min, over the 5-min job
        ProblemFile.model_validate(_file(60, time_limit_ms=30_000))
    ProblemFile.model_validate(_file(60, time_limit_ms=3000))  # 60 x 3s = 3 min fits


# --- cancellation: the job settles instead of spinning in `running` -----------------------

class FakeRedis(ScriptedRedis):
    def __init__(self):
        self.deleted, self.store = [], {}

    async def delete(self, *keys):
        self.deleted.extend(keys)

    async def set(self, key, value, ex=None):
        self.store[key] = value
        return True

    async def get(self, key):
        return self.store.get(key)


async def _pending(session_factory, user_id, problem_id):
    async with session_factory() as s:
        sub = Submission(user_id=uuid.UUID(user_id), problem_id=uuid.UUID(problem_id),
                         code="x", status="pending")
        s.add(sub)
        await s.commit()
        await s.refresh(sub)
        return str(sub.id)


async def _status(session_factory, sid):
    async with session_factory() as s:
        return (await s.get(Submission, uuid.UUID(sid))).status


async def test_a_cancelled_judge_job_is_marked_judge_error_and_releases_the_lock(
        session_factory, make_problem, make_user, monkeypatch):
    """arq cancels at `job_timeout` with CancelledError, which `except Exception` never caught:
    the write was skipped and the row spun in `running` until the sweeper failed it."""
    pid, _ = await make_problem()
    user, _ = await make_user()
    sid = await _pending(session_factory, user["id"], pid)

    async def hangs_until_cancelled(**_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(judge_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(judge_mod, "run_judgement", hangs_until_cancelled)
    redis = FakeRedis()
    lock = inflight_key(user["id"], pid)
    redis.store[lock] = sid                                # this submission holds the lock

    with pytest.raises(asyncio.CancelledError):           # re-raised: arq still sees a cancel
        await judge_mod.judge_submission({"redis": redis}, sid, "submit")

    assert await _status(session_factory, sid) == "judge_error"
    assert lock not in redis.store                         # the in-flight lock was released


async def test_cancellation_never_overwrites_a_verdict_that_already_landed(
        session_factory, make_problem, make_user):
    pid, _ = await make_problem()
    user, _ = await make_user()
    sid = await _pending(session_factory, user["id"], pid)
    async with session_factory() as s:
        (await s.get(Submission, uuid.UUID(sid))).status = "accepted"
        await s.commit()

    with patch.object(judge_mod, "SessionLocal", session_factory):
        await judge_mod._fail_submission(uuid.UUID(sid))

    assert await _status(session_factory, sid) == "accepted"


class _HangingProc:
    """A `docker run` client process whose output never arrives (a hung sandbox)."""
    returncode = None

    async def communicate(self, _input=None):
        await asyncio.Event().wait()

    async def wait(self):
        return 0


async def test_cancelling_a_docker_run_kills_the_container(monkeypatch):
    """Cancelling the awaiting task does not stop a subprocess: without an explicit kill the
    sandbox kept running after arq cancelled the job."""
    killed = []

    async def fake_exec(*_args, **_kwargs):
        return _HangingProc()

    async def fake_kill(name):
        killed.append(name)

    monkeypatch.setattr(docker_runner.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(docker_runner, "kill", fake_kill)

    task = asyncio.create_task(docker_runner.run_in_container(
        "{}", image="img", container_name="judge-abc", memory_mb=64, cpus="1",
        pids_limit=64, wall_timeout_s=999))
    await asyncio.sleep(0.05)
    assert "judge-abc" in docker_runner._active            # registered while running
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert killed == ["judge-abc"]
    assert "judge-abc" not in docker_runner._active


async def test_cancelling_a_k8s_run_stops_the_thread_and_deletes_the_pod(monkeypatch):
    """The k8s run happens in a thread, which cancellation cannot stop: it must be told to
    stop, and the pod deleted immediately rather than at the thread's own deadline."""
    seen = {}
    started = threading.Event()

    def fake_run_sync(*args):
        cancel = args[-1]
        seen["cancel"] = cancel
        started.set()
        cancel.wait(5)                                     # "polls" until told to stop

    monkeypatch.setattr(k8s_runner, "_run_sync", fake_run_sync)
    monkeypatch.setattr(k8s_runner, "_delete_run", lambda name: seen.setdefault("deleted", name))

    task = asyncio.create_task(k8s_runner.run_in_container(
        "{}", image="img", container_name="judge-xyz", memory_mb=64, cpus="1",
        pids_limit=64, wall_timeout_s=999))
    await asyncio.get_running_loop().run_in_executor(None, started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert seen["cancel"].is_set()
    assert seen["deleted"] == "judge-xyz"
    assert "judge-xyz" not in k8s_runner._active


async def test_a_failing_pod_cleanup_does_not_swallow_the_cancellation(monkeypatch):
    """If the API server is unreachable during cleanup, the cancel must still propagate as a
    CancelledError, not be replaced by the cleanup's own error."""
    started = threading.Event()

    def fake_run_sync(*args):
        started.set()
        args[-1].wait(5)

    def broken_delete(_name):
        raise ConnectionError("api server unreachable")

    monkeypatch.setattr(k8s_runner, "_run_sync", fake_run_sync)
    monkeypatch.setattr(k8s_runner, "_delete_run", broken_delete)

    task = asyncio.create_task(k8s_runner.run_in_container(
        "{}", image="img", container_name="judge-err", memory_mb=64, cpus="1",
        pids_limit=64, wall_timeout_s=999))
    await asyncio.get_running_loop().run_in_executor(None, started.wait, 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- the sweep is safe across worker replicas ----------------------------------------------

OLD = jb.MAX_LIVE_SANDBOX_AGE_S + 30
YOUNG = jb.MAX_LIVE_SANDBOX_AGE_S - 120                    # a live pod: within a job's lifetime


def _docker_ps_line(name, age_s):
    when = datetime.now(timezone.utc) - timedelta(seconds=age_s)
    return f"{name}\t{when.strftime('%Y-%m-%d %H:%M:%S +0000 UTC')}"


class _PsProc:
    def __init__(self, lines):
        self._out = ("\n".join(lines) + "\n").encode()

    async def communicate(self):
        return self._out, b""


async def _docker_sweep(monkeypatch, lines, active=()):
    killed = []

    async def fake_exec(*_args, **_kwargs):
        return _PsProc(lines)

    async def fake_docker(*args):
        killed.append(args)

    monkeypatch.setattr(docker_runner.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(docker_runner, "_docker", fake_docker)
    monkeypatch.setattr(docker_runner, "_active", set(active))
    await docker_runner.sweep_orphans()
    return {a[1] for a in killed}                          # ("kill", name)


async def test_docker_sweep_spares_another_replicas_live_container(monkeypatch):
    """`_active` is per-process, so another worker's container is never in it. Age is what tells
    a live sandbox from an orphan: a young container is left alone whoever started it."""
    killed = await _docker_sweep(monkeypatch, [
        _docker_ps_line("judge-other-replica-live", YOUNG),
        _docker_ps_line("judge-orphan", OLD),
    ])
    assert killed == {"judge-orphan"}


async def test_docker_sweep_still_spares_its_own_active_container_even_when_old(monkeypatch):
    killed = await _docker_sweep(monkeypatch, [
        _docker_ps_line("judge-mine", OLD), _docker_ps_line("judge-orphan", OLD)],
        active={"judge-mine"})
    assert killed == {"judge-orphan"}


async def test_docker_sweep_never_kills_what_it_cannot_age(monkeypatch):
    killed = await _docker_sweep(monkeypatch, ["judge-weird\tnot a timestamp", "judge-bare"])
    assert killed == set()


def test_parse_created_at_reads_dockers_format():
    parsed = docker_runner._parse_created_at("2026-09-19 12:34:56 -0700 PDT")
    assert parsed == datetime(2026, 9, 19, 12, 34, 56, tzinfo=timezone(timedelta(hours=-7)))
    assert docker_runner._parse_created_at("garbage") is None
    assert docker_runner._parse_created_at("") is None


def _k8s_item(name, age_s):
    obj = MagicMock()
    obj.metadata.name = name
    obj.metadata.creation_timestamp = (
        None if age_s is None else datetime.now(timezone.utc) - timedelta(seconds=age_s))
    return obj


def _k8s_sweep(pods, cms, active=()):
    v1 = MagicMock()
    v1.list_namespaced_pod.return_value.items = pods
    v1.list_namespaced_config_map.return_value.items = cms
    with patch.object(k8s_runner, "_load_config"), \
         patch("kubernetes.client.CoreV1Api", return_value=v1), \
         patch.object(k8s_runner, "_active", set(active)):
        k8s_runner._sweep_sync()
    return ({c.args[0] for c in v1.delete_namespaced_pod.call_args_list},
            {c.args[0] for c in v1.delete_namespaced_config_map.call_args_list})


def test_k8s_sweep_spares_another_replicas_live_pod():
    """The bug: every KEDA replica sweeps the whole namespace and `_active` is per-process, so
    replica A reaped replica B's live pod mid-judge (a correct submission -> judge_error)."""
    pods, cms = _k8s_sweep(
        [_k8s_item("judge-b-live", YOUNG), _k8s_item("judge-orphan", OLD)],
        [_k8s_item("judge-b-live", YOUNG), _k8s_item("judge-orphan", OLD)])
    assert pods == {"judge-orphan"} and cms == {"judge-orphan"}


def test_k8s_sweep_still_spares_its_own_active_pod_even_when_old():
    pods, _ = _k8s_sweep([_k8s_item("judge-mine", OLD), _k8s_item("judge-orphan", OLD)],
                         [], active={"judge-mine"})
    assert pods == {"judge-orphan"}


def test_k8s_sweep_keeps_a_pair_alive_while_either_half_is_young():
    """A name's pod and configmap are created back to back; the newer one decides."""
    pods, cms = _k8s_sweep([_k8s_item("judge-pair", OLD)], [_k8s_item("judge-pair", YOUNG)])
    assert pods == set() and cms == set()


def test_k8s_sweep_never_deletes_what_it_cannot_age():
    pods, cms = _k8s_sweep([_k8s_item("judge-unknown", None)], [_k8s_item("judge-unknown", None)])
    assert pods == set() and cms == set()


# --- the sweep is split by what it needs, so it survives scale-to-zero ------------------------

def _cron_names(settings):
    return {c.name for c in settings.cron_jobs}


def test_stale_submissions_are_swept_by_the_always_on_accounts_worker():
    """KEDA can scale the judge worker to zero, taking a cron on it with it. The DB/Redis half
    of the sweep must live where it keeps running."""
    from worker.main import AccountsWorkerSettings, WorkerSettings
    assert any("sweep_stale" in name for name in _cron_names(AccountsWorkerSettings))
    assert not any("sweep_stale" in name for name in _cron_names(WorkerSettings))


def test_orphan_reaping_stays_on_the_judge_worker():
    """Only the judge worker has the Docker socket / judge-Pod RBAC."""
    from worker.main import AccountsWorkerSettings, WorkerSettings
    assert any("reap_orphans" in name for name in _cron_names(WorkerSettings))
    assert not any("reap_orphans" in name for name in _cron_names(AccountsWorkerSettings))


def test_the_sweeper_imports_no_sandbox_runner():
    """The accounts worker runs from the lean api image (no Docker CLI, no kubernetes client),
    so importing the sweeper must not pull in a runner. Checked in a fresh interpreter, since
    this process has long since imported everything."""
    import subprocess
    import sys
    code = ("import sys, worker.sweeper; "
            "bad = [m for m in ('worker.runner', 'worker.docker_runner', 'worker.k8s_runner') "
            "if m in sys.modules]; sys.exit(1 if bad else 0)")
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


async def test_reap_orphans_delegates_to_the_runner(monkeypatch):
    calls = []

    async def fake_sweep():
        calls.append(1)

    monkeypatch.setattr(judge_mod.runner, "sweep_orphans", fake_sweep)
    await judge_mod.reap_orphans({})
    assert calls == [1]


async def test_enqueue_judge_uses_the_submission_id_as_the_job_id_on_the_judge_queue():
    """The id makes enqueueing idempotent (arq refuses a duplicate), which is what lets the sweeper
    re-enqueue safely; the explicit queue is because the sweeper runs on the accounts worker,
    whose pool defaults to `arq:accounts`."""
    from arq.constants import default_queue_name

    from app import queue as queue_mod

    class Pool:
        def __init__(self, result):
            self.calls, self.result = [], result

        async def enqueue_job(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return self.result

    created, duplicate = Pool(object()), Pool(None)
    assert await queue_mod.Queue(created).enqueue_judge("sub-1", "submit") is True
    assert await queue_mod.Queue(duplicate).enqueue_judge("sub-1", "submit") is False
    assert created.calls == [(("judge_submission", "sub-1", "submit"),
                              {"_job_id": "sub-1", "_queue_name": default_queue_name})]

