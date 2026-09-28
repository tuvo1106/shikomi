"""Shared helper for running a payload against the real Rust judge sandbox
image, used by test_rust_protocol.py, test_sandbox_rust.py and
test_seed_solutions.py so the container configuration lives in one place.

The Rust harness compiles inside the sandbox, so it can't run as a bare local
subprocess the way harness.py/harness.js do (that would need a local rustc
plus the prebuilt prelude rlib). Every Rust test is therefore Docker-marked.
The flags mirror worker/judging.py for language "rust": a 32MB tmpfs mounted
`exec`, since the harness runs the binary it just compiled there (ADR-0004).
"""
import json
import os
import pathlib
import subprocess
import sys

# RUST_IMAGE lets a run target another build, e.g. a pre-fix image to prove a
# regression test actually fails without its fix.
IMAGE = os.environ.get("RUST_IMAGE", "shikomi-judge-rust:latest")
BACKEND = pathlib.Path(__file__).resolve().parents[2] / "backend"


def run_rust_container(payload_json, container_name="judge-rust-test", memory_mb=256,
                       timeout=60, tmpfs_exec=True):
    sys.path.insert(0, str(BACKEND))
    from worker.docker_runner import build_run_args

    args = build_run_args(image=IMAGE, container_name=container_name,
                          memory_mb=memory_mb, cpus="1", pids_limit=64,
                          tmpfs_size_mb=32, tmpfs_exec=tmpfs_exec)
    return subprocess.run(args, input=payload_json, capture_output=True,
                          text=True, timeout=timeout)


def rust_results(payload, **kwargs):
    """Run `payload` (a dict) and return the parsed `results` list."""
    proc = run_rust_container(json.dumps(payload), **kwargs)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["results"]
