"""Offline judge CLI: run one sandbox payload and print the verdict (DESIGN.md §5.2).

    python -m worker.judge_local payload.json               # real Docker sandbox
    python -m worker.judge_local --subprocess payload.json   # run harness.py directly (dev, no Docker)

The payload file is the sandbox payload described in DESIGN.md §5.2.
"""
import argparse
import asyncio
import json
import pathlib
import sys

from worker import docker_runner
from worker.aggregate import Verdict, aggregate, parse_harness_output

HARNESS = pathlib.Path(__file__).resolve().parents[2] / "judge" / "harness.py"


async def _run_docker(payload_json, args):
    result = await docker_runner.run_in_container(
        payload_json,
        image=args.image,
        container_name="judge-local",
        memory_mb=args.memory_mb,
        cpus=args.cpus,
        pids_limit=args.pids_limit,
        wall_timeout_s=args.timeout,
    )
    return result


def _run_subprocess(payload_json, args):
    """Run harness.py directly — a Docker-free path for local development."""
    import subprocess

    timed_out = False
    try:
        proc = subprocess.run(
            [sys.executable, str(HARNESS)],
            input=payload_json, capture_output=True, text=True, timeout=args.timeout,
        )
        stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        code = -1

    return docker_runner.ContainerResult(
        stdout=stdout[: docker_runner.MAX_STDOUT_BYTES],
        stderr=stderr,
        exit_code=code,
        timed_out=timed_out,
        stdout_truncated=len(stdout.encode()) > docker_runner.MAX_STDOUT_BYTES,
    )


def _print_verdict(verdict: Verdict):
    print("verdict: %s (%d/%d passed)" % (verdict.status, verdict.passed, verdict.total))
    if verdict.runtime_ms is not None:
        print("runtime: %d ms" % verdict.runtime_ms)
    for r in verdict.results:
        line = "  case %s: %s (%s ms)" % (
            r.get("test_case_id"), r.get("status"), r.get("runtime_ms"))
        print(line)
        if r.get("error"):
            print("    error: %s" % r["error"].splitlines()[-1])


def main(argv=None):
    p = argparse.ArgumentParser(description="Run a judge payload and print its verdict.")
    p.add_argument("payload", type=pathlib.Path, help="sandbox payload JSON file")
    p.add_argument("--subprocess", action="store_true",
                   help="run harness.py directly instead of via Docker")
    p.add_argument("--image", default="shikomi-judge:latest")
    p.add_argument("--memory-mb", type=int, default=256)
    p.add_argument("--cpus", default="1")
    p.add_argument("--pids-limit", type=int, default=64)
    p.add_argument("--timeout", type=float, default=30.0, help="wall-clock seconds")
    args = p.parse_args(argv)

    payload_json = args.payload.read_text()

    if args.subprocess:
        container_result = _run_subprocess(payload_json, args)
    else:
        container_result = asyncio.run(_run_docker(payload_json, args))

    total_cases = len(json.loads(payload_json).get("test_cases", []))
    verdict = aggregate(container_result, parse_harness_output(container_result.stdout),
                        total_cases=total_cases)
    _print_verdict(verdict)
    if container_result.stderr.strip():
        sys.stderr.write(container_result.stderr)
    return 0 if verdict.status != "judge_error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
