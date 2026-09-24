"""Shared helper for running a payload against the real SQL judge sandbox
image — used by both test_sql_protocol.py and test_seed_solutions.py so the
container-run configuration (image, memory/tmpfs sizing) lives in exactly one
place instead of two copies drifting apart. 32MB tmpfs / 192MB memory match
ADR-0002's Phase 0 spike results (docs/adr/0002-sql-judge-engine-mysql-vs-
mariadb.md) — not the python/js images' smaller defaults.
"""
import pathlib
import subprocess
import sys

IMAGE = "shikomi-judge-sql:latest"
BACKEND = pathlib.Path(__file__).resolve().parents[2] / "backend"


def run_sql_container(payload_json, container_name="judge-sql-test", memory_mb=192, timeout=20):
    sys.path.insert(0, str(BACKEND))
    from worker.docker_runner import build_run_args

    args = build_run_args(image=IMAGE, container_name=container_name,
                          memory_mb=memory_mb, cpus="1", pids_limit=64, tmpfs_size_mb=32)
    return subprocess.run(args, input=payload_json, capture_output=True,
                          text=True, timeout=timeout)
