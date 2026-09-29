"""Judge job lifecycle: the seed-time budget, cancellation, and the cross-replica-safe
sweep (DESIGN.md §5.2, §5.7; `app/judge_budget.py`).

Three failure modes, each easy to miss:

* a problem whose run can't fit arq's `job_timeout` (so arq cancels the job before our own kill);
* the cancellation itself (`CancelledError` is a `BaseException`: `except Exception` skips it);
* one worker replica's sweep reaping another replica's live sandbox.
"""
import asyncio
import json
import pathlib
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app import judge_budget as jb
from app import sandbox as sandbox_mod
from app.sandbox import rust_method_name
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
                                               (30_000, "python"), (2000, "mysql"),
                                               (2000, "rust")])
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
        languages = [v["language"] for v in d.get("languages", [])] or [d.get("language", "python")]
        for language in languages:
            assert jb.fits_job_timeout(len(d.get("test_cases", [])),
                                       d.get("time_limit_ms", 2000), language), path


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
                         code="x", language="python", status="pending")
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


async def _set_comparison(session_factory, problem_id, comparison):
    from app.models import Problem
    async with session_factory() as s:
        (await s.get(Problem, uuid.UUID(problem_id))).comparison = comparison
        await s.commit()


@pytest.mark.parametrize("stored", [
    {"python": "def validate(*a, **k): return True", "rust": "fn validate() {}"},
    "def validate(*a, **k): return True",  # a row seeded before the per-language map
])
async def test_the_worker_hands_the_harness_its_languages_validator(
        session_factory, make_problem, make_user, monkeypatch, stored):
    """ADR-0007: the harness gets one source string, for the submission's language."""
    from worker.aggregate import Verdict
    pid, _ = await make_problem()
    await _set_comparison(session_factory, pid, {"mode": "custom_validator", "validator_code": stored})
    user, _ = await make_user()
    sid = await _pending(session_factory, user["id"], pid)
    seen = {}

    async def fake_run_judgement(**kwargs):
        seen.update(kwargs)
        return Verdict("accepted", 1.0, [], passed=0, total=0)

    monkeypatch.setattr(judge_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(judge_mod, "run_judgement", fake_run_judgement)
    await judge_mod.judge_submission({"redis": FakeRedis()}, sid, "submit")

    assert seen["comparison"] == {"mode": "custom_validator",
                                  "validator_code": "def validate(*a, **k): return True"}


async def test_no_validator_for_the_submissions_language_is_a_judge_error(
        session_factory, make_problem, make_user, monkeypatch):
    """Never a fallback to another language's validator (ADR-0007)."""
    pid, _ = await make_problem()
    await _set_comparison(session_factory, pid,
                          {"mode": "custom_validator", "validator_code": {"rust": "fn validate() {}"}})
    user, _ = await make_user()
    sid = await _pending(session_factory, user["id"], pid)

    async def never_called(**_kwargs):
        raise AssertionError("judged without a validator")

    monkeypatch.setattr(judge_mod, "SessionLocal", session_factory)
    monkeypatch.setattr(judge_mod, "run_judgement", never_called)
    await judge_mod.judge_submission({"redis": FakeRedis()}, sid, "submit")

    assert await _status(session_factory, sid) == "judge_error"


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



# --- language rules the Rust judge relies on (ADR-0004) ---------------------------------------

def _rust_ops_file(*ops_lists, params=None):
    cases = [{"ordinal": i, "input": [["C", *ops], [[]] + [[] for _ in ops]],
              "expected": [None] * (len(ops) + 1), "is_sample": True}
             for i, ops in enumerate(ops_lists)]
    return {**_file(1, language="rust", kind="operations", function_name=None, class_name="C",
                    params=params or []), "test_cases": cases}


def test_rust_supports_operations_mode():
    """harness_rs dispatches each op name to a method (harness.rs `operations_glue`)."""
    ProblemFile.model_validate(_rust_ops_file(["push", "getState"], ["type"]))


_REPO = pathlib.Path(__file__).resolve().parents[2]
_METHOD_NAMES = json.loads((_REPO / "judge/tests/rust_method_names.json").read_text())["names"]


@pytest.mark.parametrize("op, method", sorted(_METHOD_NAMES.items()))
def test_rust_method_name_mirrors_the_harness(op, method):
    """The table judge/tests/test_rust_protocol.py also runs through the real
    harness.rs `method_name`, so the two implementations are held to one spec."""
    assert rust_method_name(op) == method


def test_rust_keyword_list_matches_the_harness():
    """harness.rs `KEYWORDS` and `_RUST_KEYWORDS` decide which ops get `r#`. The
    Docker test can only exercise a few keywords, so the lists are compared here,
    where CI always runs."""
    source = (_REPO / "judge/harness_rs/harness.rs").read_text()
    block = re.search(r"const KEYWORDS: &\[&str\] = &\[(.*?)\];", source, re.S)
    assert block, "KEYWORDS not found in harness.rs"
    assert set(re.findall(r'"([^"]+)"', block.group(1))) == set(sandbox_mod._RUST_KEYWORDS)


def test_rust_refuses_an_op_named_like_the_constructor():
    with pytest.raises(ValidationError, match="op 'new' can't be a Rust method name"):
        ProblemFile.model_validate(_rust_ops_file(["push", "new"]))


def test_rust_refuses_an_op_that_cant_be_a_method():
    with pytest.raises(ValidationError, match="op 'has-dash' can't be a Rust method name"):
        ProblemFile.model_validate(_rust_ops_file(["push", "has-dash"]))


def test_rust_refuses_two_ops_mapping_to_one_method():
    with pytest.raises(ValidationError, match="'getState' and 'get_state' both map"):
        ProblemFile.model_validate(_rust_ops_file(["getState"], ["get_state"]))


def test_python_only_operations_problems_keep_any_op_name():
    """The snake_case mapping is Rust's; a Python-only problem isn't held to it."""
    data = _rust_ops_file(["getState"], ["get_state"])
    data["language"] = "python"
    data["memory_limit_mb"] = 64
    ProblemFile.model_validate(data)


def test_rust_operations_constructor_node_values_must_fit_i32():
    """`params` describe an operations problem's constructor, whose args are the
    first argument list, so its node values are checked like a function's."""
    data = _rust_ops_file(["size"], params=[{"name": "head", "type": "ListNode"}])
    data["test_cases"][0]["input"][1][0] = [[1, 2**31]]
    with pytest.raises(ValidationError, match="constructor argument head holds the node value"):
        ProblemFile.model_validate(data)


@pytest.mark.parametrize("node_type", ["ListNode", "TreeNode", "List[ListNode]", "List[TreeNode]",
                                       "CyclicListNode", "RandomListNode", "GraphNode"])
def test_rust_accepts_its_node_codecs(node_type):
    """prelude.rs `nodes` implements these, so a Rust variant may declare them
    (as a param or the return type) and the workspace draws its samples."""
    ProblemFile.model_validate(_file(1, language="rust", params=[{"name": "x", "type": node_type}]))
    ProblemFile.model_validate(_file(1, language="rust", return_type=node_type))


@pytest.mark.parametrize("language,node_type", [
    ("js", "ListNode"), ("js", "TreeNode"), ("js", "GraphNode"),
])
def test_a_language_refuses_node_types_its_harness_lacks(language, node_type):
    with pytest.raises(ValidationError, match=f"language '{language}' does not support the '{node_type}'"):
        ProblemFile.model_validate(_file(1, language=language, params=[{"name": "x", "type": node_type}]))
    with pytest.raises(ValidationError, match=f"does not support the '{node_type}'"):
        ProblemFile.model_validate(_file(1, language=language, return_type=node_type))


def test_rust_accepts_the_iterator_constructor_arg():
    """prelude.rs `IntIter` is Rust's decode-only Iterator: valid as a param,
    never as a return type (in any language)."""
    ProblemFile.model_validate(_file(1, language="rust", params=[{"name": "nums", "type": "Iterator"}]))
    with pytest.raises(ValidationError):
        ProblemFile.model_validate(_file(1, language="rust", return_type="Iterator"))


def test_rust_node_types_match_what_the_rust_harness_implements():
    """`RUST_NODE_TYPES` is what a Rust variant may declare, so it must be exactly
    the node structs harness.rs generates (and their `List[...]` forms) plus the
    prelude's `IntIter` for "Iterator"."""
    root = pathlib.Path(__file__).resolve().parents[2] / "judge" / "harness_rs"
    structs = set(re.findall(r'NodeStruct \{ name: "(\w+)"', (root / "harness.rs").read_text()))
    expected = structs | {f"List[{n}]" for n in ("ListNode", "TreeNode")}
    if "pub struct IntIter" in (root / "prelude.rs").read_text():
        expected.add("Iterator")
    assert sandbox_mod.RUST_NODE_TYPES == expected


def test_rust_refuses_a_node_type_missing_from_its_profile(monkeypatch):
    """The gate is the profile's `node_types`: drop a codec from Rust's and a variant
    declaring it is refused (so a codec added for Python alone can't leak into Rust)."""
    import dataclasses
    rust = sandbox_mod.PROFILES["rust"]
    monkeypatch.setitem(sandbox_mod.PROFILES, "rust",
                        dataclasses.replace(rust, node_types=rust.node_types - {"GraphNode"}))
    with pytest.raises(ValidationError, match="language 'rust' does not support the 'GraphNode'"):
        ProblemFile.model_validate(_file(1, language="rust", params=[{"name": "x", "type": "GraphNode"}]))


def test_rust_memory_limit_must_leave_room_for_rustc():
    """rustc runs inside the submission's own memory limit, so a Rust problem
    can't declare less than the compiler needs."""
    with pytest.raises(ValidationError, match="memory_limit_mb >= 128"):
        ProblemFile.model_validate(_file(1, language="rust", memory_limit_mb=64))
    ProblemFile.model_validate(_file(1, language="rust", memory_limit_mb=128))
    ProblemFile.model_validate(_file(1, language="python", memory_limit_mb=64))  # unaffected


def test_rust_budget_reserves_the_compile_timeout_with_margin():
    # The worker sends RUST_COMPILE_TIMEOUT_S as the harness's rustc deadline; the wall
    # budget must cover all of it (plus harness startup) or a slow compile would read
    # as a whole-run TLE.
    from app.sandbox import RUST_COMPILE_TIMEOUT_S, profile_for
    assert profile_for("rust").startup_slack_s > RUST_COMPILE_TIMEOUT_S
    assert jb.wall_budget_s(1, 2000, "rust") == pytest.approx(
        2 + jb.WALL_CLOCK_SLACK_S + RUST_COMPILE_TIMEOUT_S + 2)


def test_return_type_literal_is_every_node_type_but_iterator():
    """One list of node types (`ALL_NODE_TYPES`); the `ReturnType` Literal has to be
    spelled out for Pydantic, so this keeps the two from drifting."""
    from typing import get_args

    from app.sandbox import ALL_NODE_TYPES, PROFILES
    from app.schemas.problem import ReturnType
    assert set(get_args(ReturnType)) - {""} == ALL_NODE_TYPES - {"Iterator"}
    for profile in PROFILES.values():
        assert profile.node_types <= ALL_NODE_TYPES


def _rust_node_file(inp, expected, **extra):
    return {**BODY, "language": "rust", "params": [{"name": "head", "type": "ListNode"}],
            "return_type": "ListNode",
            "test_cases": [{"ordinal": 0, "input": [inp], "expected": expected, "is_sample": True}],
            **extra}


@pytest.mark.parametrize("bad", [2**31, -(2**31) - 1, 1.5, "7", True])
def test_rust_node_values_must_fit_an_i32(bad):
    """Rust's node structs hold an i32; a value that doesn't fit would fail every Rust
    submission with a decode error, so the seed refuses it."""
    with pytest.raises(ValidationError, match=r"test case 0: head holds the node value .* integer in"):
        ProblemFile.model_validate(_rust_node_file([1, bad], [1]))
    with pytest.raises(ValidationError, match="the expected output holds the node value"):
        ProblemFile.model_validate(_rust_node_file([1], [bad]))


def test_rust_node_values_at_the_i32_edges_load_and_python_takes_anything():
    ProblemFile.model_validate(_rust_node_file([2**31 - 1, -(2**31)], []))
    python = {**BODY, "params": [{"name": "head", "type": "ListNode"}], "return_type": "ListNode",
              "test_cases": [{"ordinal": 0, "input": [["a", 2**40]], "expected": [], "is_sample": True}]}
    ProblemFile.model_validate(python)


def test_rust_node_values_are_checked_in_each_any_of_option_and_tree_nulls_are_fine():
    tree = {**BODY, "language": "rust", "params": [{"name": "root", "type": "TreeNode"}],
            "return_type": "TreeNode", "comparison": {"mode": "any_of"},
            "test_cases": [{"ordinal": 0, "input": [[1, None, 2]], "expected": [[1], [2]], "is_sample": True}]}
    ProblemFile.model_validate(tree)
    tree["test_cases"][0]["expected"] = [[1], [2**40]]
    with pytest.raises(ValidationError, match="the expected output holds the node value"):
        ProblemFile.model_validate(tree)


def _node_case_file(node_type, inp, expected=None, language="python"):
    return {**BODY, "language": language, "params": [{"name": "x", "type": node_type}],
            "test_cases": [{"ordinal": 0, "input": [inp], "expected": expected, "is_sample": True}]}


@pytest.mark.parametrize("node_type,wire,why", [
    ("CyclicListNode", [[1, 2], 2], "cycle position 2"),
    ("CyclicListNode", [[1, 2], -2], "cycle position -2"),   # Python would wrap it to the last node
    ("CyclicListNode", [[], 0], "cycle position 0"),
    ("CyclicListNode", [1, 2, 3], "expected [values, pos]"),
    ("RandomListNode", [[1, None], [2, -1]], "random index -1"),
    ("RandomListNode", [[1, 2]], "random index 2"),
    ("GraphNode", [[2], [0]], "neighbour 0"),                # Python: nodes[-1]
    ("GraphNode", [[3], [1]], "neighbour 3"),
])
@pytest.mark.parametrize("language", ["python", "rust"])
def test_node_encodings_with_indices_off_the_structure_are_refused(node_type, wire, why, language):
    """harness.py wraps a bad index, harness_rs refuses it; either way it's an
    authoring bug, refused at seed time for every language."""
    with pytest.raises(ValidationError, match=f"test case 0: x is not a valid {node_type}: .*{re.escape(why)}"):
        ProblemFile.model_validate(_node_case_file(node_type, wire, language=language))


@pytest.mark.parametrize("node_type,wire", [
    ("CyclicListNode", [[1, 2], -1]), ("CyclicListNode", [[1, 2], 1]), ("CyclicListNode", []),
    ("RandomListNode", [[1, None], [2, 0]]), ("RandomListNode", []),
    ("GraphNode", [[2], [1]]), ("GraphNode", [[]]), ("GraphNode", []),
])
def test_well_formed_node_encodings_load(node_type, wire):
    ProblemFile.model_validate(_node_case_file(node_type, wire))


def test_a_cyclic_list_output_is_an_index_not_a_list():
    """A CyclicListNode return is the answer node's index (harness.py's `_idx`), so
    the structural check applies to its input only."""
    f = _node_case_file("CyclicListNode", [[3, 2, 0], 1], expected=1)
    f["return_type"] = "CyclicListNode"
    ProblemFile.model_validate(f)
    ProblemFile.model_validate({**f, "language": "rust"})

