"""Sandbox isolation tests for the JS judge image (DESIGN.md §10.1, §13) — the
JS-image counterpart of test_sandbox.py, proving the JS sandbox is exactly as
locked down as the Python one, not just functionally correct. Same `docker`
marker/gating; CI builds the image first:

    docker build -f judge/Dockerfile.js -t shikomi-judge-js:latest judge/

`test_network_is_blocked_for_async_submissions` below only became possible once
harness.js could await a returned Promise (it's what actually yields back to
the event loop for a connection attempt's callback to fire) — before that, a
synchronous function-mode call never yielded, so a busy-wait "until a flag
flips" couldn't distinguish "blocked" from "would have succeeded," and there
was nothing here to test beyond what test_sandbox.py already proves for
`--network=none` (shared, language-independent infrastructure in
`docker_runner.build_run_args`).

Not covered here, and why:

* "wall-clock kill when the harness's own timeout is defeated" — an ordinary
  hung Promise is now caught by harness.js's own async-race timeout (see
  `awaitIfThenable`), not just the outer wall-clock kill. What's left
  undefeated by anything but the outer kill is a submission that starves the
  event loop entirely with an unyielding microtask chain (harness.js's module
  docstring), which has no clean one-line repro to provoke here.
"""
import json
import pathlib
import subprocess
import sys

import pytest

pytestmark = pytest.mark.docker

IMAGE = "shikomi-judge-js:latest"
BACKEND = pathlib.Path(__file__).resolve().parents[2] / "backend"


def _payload(user_code, inp=None, time_limit_ms=2000):
    return json.dumps({
        "function_name": "f",
        "user_code": user_code,
        "test_cases": [{"id": 0, "input": inp or [0], "expected": None}],
        "comparison": {"mode": "exact"},
        "time_limit_ms": time_limit_ms,
    })


def run_container(payload_json, name="judge-js-test", memory_mb=128, timeout=20):
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
    # SIGKILL (exit 137) or a V8 "out of memory"/RangeError caught by the
    # harness (runtime_error). Both prove the host isn't exhausted.
    code = "function f(x) { var y = new Array(500 * 1024 * 1024).fill(0); return y.length; }"
    proc = run_container(_payload(code))
    assert proc.returncode == 137 or "runtime_error" in proc.stdout


def test_filesystem_is_read_only_outside_tmp():
    code = ("const fs = require('fs');\n"
            "function f(x) { fs.writeFileSync('/escape.txt', 'nope'); }")
    assert _first_status(run_container(_payload(code))) == "runtime_error"


def test_tmp_is_writable():
    code = ("const fs = require('fs');\n"
            "function f(x) { fs.writeFileSync('/tmp/ok.txt', 'fine'); return 1; }")
    assert _first_status(run_container(_payload(code))) in {"passed", "wrong_answer"}


def test_network_is_blocked_for_async_submissions():
    # Only reachable now that the harness awaits a returned Promise (see the
    # module docstring) — an outbound request's failure surfaces as a rejected
    # Promise, which the async path maps to runtime_error same as a thrown
    # exception would.
    code = ("const https = require('https');\n"
            "function f(x) {\n"
            "  return new Promise((resolve, reject) => {\n"
            "    https.get('https://example.com', resolve).on('error', reject);\n"
            "  });\n"
            "}")
    assert _first_status(run_container(_payload(code))) == "runtime_error"


def test_pids_limit_contains_fork_bomb():
    # --pids-limit caps process creation, but `child_process.spawnSync` doesn't
    # throw on the resulting EAGAIN — it just sets `.error` and returns — so
    # the loop keeps retrying rather than surfacing a JS-level runtime_error.
    # Each retry still costs memory/process-table entries, so containment shows
    # up as one of: the wall-clock kill (exercised via the real runner here),
    # an OOM kill (exit 137) from the accumulating retries, or — if a retry
    # ever does throw — a runtime_error. Any of the three proves the host is
    # protected; which one wins is a timing race, not something to pin down.
    import asyncio

    sys.path.insert(0, str(BACKEND))
    from worker import docker_runner

    code = ("const { spawnSync } = require('child_process');\n"
            "function f(x) {\n"
            "  while (true) { spawnSync('sleep', ['60']); }\n"
            "}")
    result = asyncio.run(docker_runner.run_in_container(
        _payload(code, time_limit_ms=1000),
        image=IMAGE, container_name="judge-js-forkbomb",
        memory_mb=128, cpus="1", pids_limit=64, wall_timeout_s=8))
    assert result.timed_out or result.oom_killed or "runtime_error" in result.stdout
