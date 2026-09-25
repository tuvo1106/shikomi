"""Sandbox isolation tests (DESIGN.md §10.1, second layer).

These run the real judge container and verify the security boundary, so they are
gated behind the `docker` marker and skipped in the default local run. CI runs
them as a separate job (`pytest -m docker`) with the image built:

    docker build -t shikomi-judge:latest judge/
"""
import json
import pathlib
import subprocess
import sys

import pytest

pytestmark = pytest.mark.docker

IMAGE = "shikomi-judge:latest"
BACKEND = pathlib.Path(__file__).resolve().parents[2] / "backend"


def _payload(user_code, inp=None, time_limit_ms=2000):
    return json.dumps({
        "function_name": "f",
        "user_code": user_code,
        "test_cases": [{"id": 0, "input": inp or [0], "expected": None}],
        "comparison": {"mode": "exact"},
        "time_limit_ms": time_limit_ms,
    })


def run_container(payload_json, name="judge-test", memory_mb=128, timeout=20):
    sys.path.insert(0, str(BACKEND))
    from worker.docker_runner import build_run_args

    args = build_run_args(image=IMAGE, container_name=name,
                          memory_mb=memory_mb, cpus="1", pids_limit=64)
    return subprocess.run(args, input=payload_json, capture_output=True,
                          text=True, timeout=timeout)


def _first_status(proc):
    return json.loads(proc.stdout)["results"][0]["status"]


def test_memory_limit_is_enforced():
    # Allocating past the container limit is contained one of two ways: an OOM
    # SIGKILL (exit 137 → memory_limit_exceeded) or a Python MemoryError caught
    # by the harness (runtime_error). Both prove the host isn't exhausted.
    proc = run_container(_payload("def f(x):\n    y = [0] * (500 * 1024 * 1024)\n    return len(y)"))
    assert proc.returncode == 137 or "runtime_error" in proc.stdout


def test_network_is_blocked():
    code = ("import socket\n"
            "def f(x):\n"
            "    socket.create_connection(('1.1.1.1', 80), timeout=3)\n")
    assert _first_status(run_container(_payload(code))) == "runtime_error"


def test_filesystem_is_read_only_outside_tmp():
    code = ("def f(x):\n"
            "    open('/escape.txt', 'w').write('nope')\n")
    assert _first_status(run_container(_payload(code))) == "runtime_error"


def test_tmp_is_writable():
    code = ("def f(x):\n"
            "    open('/tmp/ok.txt', 'w').write('fine')\n"
            "    return 1\n")
    assert _first_status(run_container(_payload(code))) in {"passed", "wrong_answer"}


def test_pids_limit_contains_fork_bomb():
    # --pids-limit caps thread creation: the harness hits "can't start new thread"
    # (runtime_error). The spawned non-daemon threads then keep the process alive,
    # which the worker's wall-clock kill reaps — so either outcome proves the host
    # is protected. Run it through the real runner so the kill path is exercised.
    import asyncio

    sys.path.insert(0, str(BACKEND))
    from worker import docker_runner

    code = ("import threading, time\n"
            "def f(x):\n"
            "    while True:\n"
            "        threading.Thread(target=lambda: time.sleep(60)).start()\n")
    result = asyncio.run(docker_runner.run_in_container(
        _payload(code, time_limit_ms=1000),
        image=IMAGE, container_name="judge-forkbomb",
        memory_mb=128, cpus="1", pids_limit=64, wall_timeout_s=8))
    assert result.timed_out or "runtime_error" in result.stdout


def test_wall_clock_kill_when_alarm_defeated():
    # User code disables SIGALRM, defeating the harness's own timeout. The worker's
    # wall-clock kill (docker_runner) is the backstop.
    import asyncio

    sys.path.insert(0, str(BACKEND))
    from worker import docker_runner

    code = ("import signal\n"
            "def f(x):\n"
            "    signal.signal(signal.SIGALRM, signal.SIG_IGN)\n"
            "    while True:\n"
            "        pass\n")
    result = asyncio.run(docker_runner.run_in_container(
        _payload(code, time_limit_ms=500),
        image=IMAGE, container_name="judge-wallclock",
        memory_mb=128, cpus="1", pids_limit=64, wall_timeout_s=3))
    assert result.timed_out


# The sweep tests below name their containers `sweeptest-*` and sweep by that prefix, not
# `judge-*`. A worker running anywhere on the same Docker daemon (another checkout's dev stack,
# say) sweeps every `judge-*` container it doesn't own, and would kill a test's container out
# from under it. One such failure was seen once and never reproduced; this takes the tests out
# of the way regardless of the cause.


def test_sweep_orphans_kills_leftovers(monkeypatch):
    import asyncio
    import time

    sys.path.insert(0, str(BACKEND))
    from worker import docker_runner

    # The sweep only reaps containers older than any live job can be (six minutes), so a
    # container made a moment ago would be spared. A negative threshold makes "just now" old.
    monkeypatch.setattr(docker_runner, "MAX_LIVE_SANDBOX_AGE_S", -1)
    name = "sweeptest-orphan"
    subprocess.run(["docker", "run", "-d", "--name", name, "--entrypoint", "sleep",
                    IMAGE, "300"], check=True, capture_output=True)
    try:
        asyncio.run(docker_runner.sweep_orphans("sweeptest-orphan"))
        # `docker kill` returns once the daemon accepts the signal, not once `docker
        # ps` reflects the container as stopped — under CI load that state update
        # can lag a beat behind. Poll briefly instead of asserting on a single
        # immediate snapshot (this raced intermittently in CI: e.g. run 33183357203).
        deadline = time.monotonic() + 5
        listing = None
        while time.monotonic() < deadline:
            listing = subprocess.run(["docker", "ps", "-q", "--filter", "name=%s" % name],
                                     capture_output=True, text=True)
            if listing.stdout.strip() == "":
                break
            time.sleep(0.2)
        assert listing.stdout.strip() == ""
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_sweep_orphans_spares_a_young_container_it_does_not_own():
    # Another worker replica's live sandbox is not in this process's `_active`; only its age
    # (younger than any live job can be) protects it from this replica's cron.
    import asyncio

    sys.path.insert(0, str(BACKEND))
    from worker import docker_runner

    name = "sweeptest-young"
    subprocess.run(["docker", "run", "-d", "--name", name, "--entrypoint", "sleep",
                    IMAGE, "300"], check=True, capture_output=True)
    try:
        asyncio.run(docker_runner.sweep_orphans("sweeptest-young"))
        listing = subprocess.run(["docker", "ps", "-q", "--filter", "name=%s" % name],
                                 capture_output=True, text=True)
        assert listing.stdout.strip() != ""
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def test_sweep_orphans_skips_container_this_process_started():
    # Killing *every* judge-* container by name can't tell a genuine orphan
    # from a submission still legitimately judging in this same process — so
    # the once-a-minute cron would kill a live submission out from under it (reported as memory_limit_exceeded via
    # the exit-137 aggregate-precedence bug, since a `docker kill` and an OOM
    # kill share the same exit code). `run_in_container` now registers its
    # container name in `docker_runner._active` for the run's lifetime, and
    # `sweep_orphans` must skip anything registered there.
    import asyncio

    sys.path.insert(0, str(BACKEND))
    from worker import docker_runner

    async def scenario():
        code = "import time\ndef f(x):\n    time.sleep(2)\n    return x\n"
        task = asyncio.create_task(docker_runner.run_in_container(
            _payload(code, time_limit_ms=5000),
            image=IMAGE, container_name="sweeptest-active",
            memory_mb=128, cpus="1", pids_limit=64, wall_timeout_s=10))
        # Give the container time to actually start before sweeping.
        await asyncio.sleep(0.5)
        assert "sweeptest-active" in docker_runner._active
        await docker_runner.sweep_orphans("sweeptest-active")
        return await task

    try:
        result = asyncio.run(scenario())
        assert not result.timed_out
        assert not result.oom_killed
    finally:
        subprocess.run(["docker", "rm", "-f", "sweeptest-active"], capture_output=True)
