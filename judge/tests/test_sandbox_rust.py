"""Sandbox isolation tests for the Rust judge image (DESIGN.md §10.1, §13;
ADR-0004): the Rust counterpart of test_sandbox.py/test_sandbox_js.py. These
prove the Rust sandbox is as locked down as the others, and that it still is
with the one flag it relaxes (`exec` on the /tmp tmpfs). Same `docker`
marker; build the image first:

    docker build -f judge/Dockerfile.rust -t shikomi-judge-rust:latest judge/

Rust submissions are native code, so these probe the kernel-level controls
directly (a raw TCP connect, a raw file write, a real fork loop), not a
language runtime's view of them.
"""
import asyncio
import json
import pathlib
import sys

import pytest

from rust_runner import IMAGE, run_rust_container

pytestmark = pytest.mark.docker

BACKEND = pathlib.Path(__file__).resolve().parents[2] / "backend"


def _payload(user_code, time_limit_ms=2000, memory_limit_mb=256):
    return json.dumps({
        "function_name": "f",
        "user_code": user_code,
        "test_cases": [{"id": 0, "input": [0], "expected": "blocked"}],
        "comparison": {"mode": "exact"},
        "time_limit_ms": time_limit_ms,
        "memory_limit_mb": memory_limit_mb,
    })


def _first(proc):
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["results"][0]


def test_network_is_blocked():
    code = """
fn f(_x: i32) -> String {
    match std::net::TcpStream::connect_timeout(
        &"1.1.1.1:443".parse().unwrap(), std::time::Duration::from_secs(1)) {
        Ok(_) => "connected".into(),
        Err(_) => "blocked".into(),
    }
}
"""
    assert _first(run_rust_container(_payload(code)))["status"] == "passed"


def test_filesystem_is_read_only_outside_tmp():
    code = """
fn f(_x: i32) -> String {
    let home = std::fs::write("/home/runner/escape.txt", "x").is_err();
    let judge = std::fs::write("/opt/judge/harness", "x").is_err();
    if home && judge { "blocked".into() } else { "wrote".into() }
}
"""
    assert _first(run_rust_container(_payload(code)))["status"] == "passed"


def test_submission_cannot_tamper_with_the_harness_between_cases():
    # The harness binary and the prelude it links are on the read-only root; only
    # the submission's own scratch files live in the writable /tmp.
    code = """
fn f(_x: i32) -> String {
    let lib = std::fs::remove_file("/opt/judge/lib/libshikomi_prelude.rlib").is_err();
    let bin = std::fs::OpenOptions::new().append(true).open("/opt/judge/harness").is_err();
    if lib && bin { "blocked".into() } else { "tampered".into() }
}
"""
    assert _first(run_rust_container(_payload(code)))["status"] == "passed"


def _run_with_payload_file(payload_json, container_name="judge-rust-payload"):
    """Deliver the payload the way the k8s runner does — as a file the harness
    reads via JUDGE_PAYLOAD_FILE — instead of on stdin, so the parent's
    read-then-unlink path runs. A shell wrapper writes stdin to /tmp/payload.json
    (the writable tmpfs, since the root FS is read-only), points the harness at
    it, and execs the real entrypoint."""
    sys.path.insert(0, str(BACKEND))
    from app.sandbox import profile_for
    from worker.docker_runner import build_run_args
    import subprocess

    profile = profile_for("rust")
    base = build_run_args(image=IMAGE, container_name=container_name, memory_mb=256,
                          cpus="1", pids_limit=64, tmpfs_size_mb=profile.tmpfs_size_mb,
                          tmpfs_exec=profile.tmpfs_exec)
    wrapper = ("cat > /tmp/payload.json && "
               "JUDGE_PAYLOAD_FILE=/tmp/payload.json exec /opt/judge/harness")
    args = base[:-1] + ["--entrypoint", "sh", IMAGE, "-c", wrapper]
    return subprocess.run(args, input=payload_json, capture_output=True, text=True, timeout=60)


def test_k8s_payload_file_is_deleted_before_the_submission_runs():
    # On the k8s runner the payload is a file at a fixed path in a volume the
    # case's process shares (same uid), so a submission could otherwise hardcode
    # that path and read every hidden case's `expected` out of it. The parent
    # reads the file and unlinks it before any case runs (mirroring harness.py/js;
    # ADR-0006), so by the time the submission runs the path is gone.
    code = """
fn f(_x: i32) -> String {
    match std::fs::read_to_string("/tmp/payload.json") {
        Ok(_) => "leaked".into(),   // still readable → the gap is open
        Err(_) => "blocked".into(), // deleted before we ran
    }
}
"""
    proc = _run_with_payload_file(_payload(code))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["results"][0]["status"] == "passed"


def test_tmp_is_noexec_without_the_rust_flag():
    # Regression guard for the reason `tmpfs_exec` exists: under Docker's default
    # noexec tmpfs the compiled program can't start. That's a judge fault, not the
    # submission's, so the harness exits non-zero with no results (the worker's
    # aggregate() turns that into judge_error) and says why on stderr.
    proc = run_rust_container(_payload('fn f(_x: i32) -> String { "blocked".into() }'),
                              tmpfs_exec=False)
    assert proc.returncode == 3
    assert proc.stdout == ""
    assert "could not start the program" in proc.stderr


def test_container_memory_limit_bounds_the_whole_run():
    # With no per-case RLIMIT_AS (memory_limit_mb omitted), the container's
    # cgroup is the only bound. An allocation bomb is still contained: the OOM
    # killer takes the case's process, and the harness reports it for that case.
    code = "fn f(_x: i32) -> String { let mut v: Vec<Vec<u8>> = Vec::new(); loop { v.push(vec![1u8; 1 << 20]); } }"
    pl = json.loads(_payload(code))
    del pl["memory_limit_mb"]
    proc = run_rust_container(json.dumps(pl), memory_mb=160)
    assert proc.returncode == 137 or _first(proc)["status"] == "memory_limit_exceeded"


def test_pids_limit_contains_fork_bomb():
    # Each spawn that succeeds sleeps forever, so --pids-limit is what stops the
    # loop: spawn() starts failing with EAGAIN. Containment shows up as the
    # per-case time limit (the loop keeps retrying), a wall-clock kill or an
    # OOM; any of them proves the host is protected.
    sys.path.insert(0, str(BACKEND))
    from worker import docker_runner

    code = """
fn f(_x: i32) -> String {
    let mut kids = Vec::new();
    loop {
        if let Ok(c) = std::process::Command::new("sleep").arg("60").spawn() { kids.push(c); }
    }
}
"""
    result = asyncio.run(docker_runner.run_in_container(
        _payload(code, time_limit_ms=1000),
        image=IMAGE, container_name="judge-rust-forkbomb",
        memory_mb=256, cpus="1", pids_limit=64, wall_timeout_s=15,
        tmpfs_size_mb=32, tmpfs_exec=True))
    assert result.timed_out or result.oom_killed or "time_limit_exceeded" in result.stdout
