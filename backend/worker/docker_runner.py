"""Runs a judge payload inside an ephemeral, locked-down Docker container.

See DESIGN.md §5.2 (execution flow) and §5.5 (sandbox flags). This module owns
container lifecycle only — verdict aggregation lives in the judge job. It has no
third-party dependencies so it can be exercised before the backend package exists.
"""
import asyncio
import dataclasses
import logging
from datetime import datetime, timezone

from app.judge_budget import MAX_LIVE_SANDBOX_AGE_S

logger = logging.getLogger(__name__)

MAX_STDOUT_BYTES = 1_000_000  # 1 MB → output_limit_exceeded beyond this (§5.2 step 5)
OOM_EXIT_CODE = 137  # 128 + SIGKILL; Docker's signal for a --memory OOM kill

# Container names this process currently has a submission running in — added
# before `docker run` starts, removed once it returns. `sweep_orphans` skips
# anything in here so the once-a-minute cron (which runs in this same worker
# process, concurrently with in-flight judge jobs — see worker/judge.py's
# `reap_orphans`) never kills a submission that's still legitimately judging.
# This is the precise, in-process half of the guard; the age rule in `sweep_orphans`
# is the cross-process half (another worker process's containers aren't in here).
_active: set[str] = set()


@dataclasses.dataclass
class ContainerResult:
    """The raw outcome of one sandbox run, before it's interpreted into a verdict.

    Captures everything the aggregator needs to classify the result: the harness's
    `stdout` (its JSON report), `stderr` (diagnostics), the process `exit_code`,
    and the two out-of-band failure flags — `timed_out` (we wall-clock-killed it)
    and `stdout_truncated` (it blew the 1 MB output cap).
    """

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    stdout_truncated: bool

    @property
    def oom_killed(self):
        """True if Docker OOM-killed the container (exit 137 = 128 + SIGKILL)."""
        return self.exit_code == OOM_EXIT_CODE


def build_run_args(*, image, container_name, memory_mb, cpus, pids_limit, tmpfs_size_mb=16):
    """The `docker run` argv for one submission — the whole sandbox in one list.

    We run *arbitrary user code*, so the container is locked down along every axis
    an attacker could push on. Each flag closes a specific door (DESIGN.md §5.5):

    * `--rm` — auto-delete the container on exit (no accumulation).
    * `-i` / stdin — the only channel in; the payload is piped, nothing on disk.
    * `--network=none` — no network at all: no exfiltration, no callbacks, no
      pulling tools. The single biggest lockdown.
    * `--memory` + `--memory-swap` equal — hard RAM cap with **swap disabled**, so
      a memory bomb is OOM-killed (exit 137) instead of thrashing swap to survive.
    * `--cpus` — CPU quota, so a busy loop can't starve the host (paired with the
      wall-clock kill in `run_in_container`).
    * `--pids-limit` — cap process/thread count, defeating fork bombs.
    * `--read-only` root FS + a small `--tmpfs /tmp` — code can't write anywhere
      except a tiny, capped scratch dir that vanishes with the container.
      `tmpfs_size_mb` defaults to 16 (python/js's stateless-interpreter needs);
      the SQL image passes a larger value since a database server's data
      directory has to live entirely in this scratch space too (ADR-0002).
    * `--security-opt=no-new-privileges` — a child can never gain privileges (e.g.
      via setuid), blocking a class of escalation.
    * `--cap-drop=ALL` — drop every Linux capability; the process keeps none of
      root's special powers.
    * `--user 1000:1000` — run as an unprivileged non-root user, not root.
    """
    return [
        "docker", "run", "--rm", "-i",
        "--name", container_name,
        "--network=none",
        "--memory=%dm" % memory_mb,
        "--memory-swap=%dm" % memory_mb,  # == memory → swap disabled
        "--cpus=%s" % cpus,
        "--pids-limit=%d" % pids_limit,
        "--read-only",
        "--tmpfs", "/tmp:size=%dm" % tmpfs_size_mb,
        "--security-opt=no-new-privileges",
        "--cap-drop=ALL",
        "--user", "1000:1000",
        image,
    ]


async def _docker(*args):
    proc = await asyncio.create_subprocess_exec(
        "docker", *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


async def kill(container_name):
    await _docker("kill", container_name)


def _parse_created_at(text):
    """Parse `docker ps`'s `{{.CreatedAt}}` (e.g. `2026-09-19 12:34:56 -0700 PDT`).

    Returns None for anything unrecognised, and the caller then leaves that container alone:
    a sweep must never kill something it can't age.
    """
    parts = text.split()
    if len(parts) < 3:
        return None
    try:
        return datetime.strptime(" ".join(parts[:3]), "%Y-%m-%d %H:%M:%S %z")
    except ValueError:
        return None


async def sweep_orphans(prefix="judge-"):
    """Kill leftover judge containers that no live job can own (worker startup +
    sweeper cron, §5.7).

    A container is reaped only if **both** hold:

    * it is not in `_active` (this process isn't waiting on it), and
    * it is older than `MAX_LIVE_SANDBOX_AGE_S`.

    The second rule is what makes this safe when more than one worker process shares a Docker
    daemon (`docker compose up --scale worker=N`): a container started by a *different*
    process is not in *this* process's `_active`, so `_active` alone would let one worker's
    cron kill another's live sandbox mid-judge. But a live sandbox belongs to a job arq
    cancels at `JUDGE_JOB_TIMEOUT_SECONDS`, and authoring refuses any problem whose run could
    outlast that (app/judge_budget.py), so no live container is older than the bound. Older
    means orphaned, whoever started it. The cost: a container leaked by a crashed worker
    lingers up to about six minutes before it is reaped (it is `--rm`, resource-capped and
    wall-clock bounded by the harness meanwhile).
    """
    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "--format", "{{.Names}}\t{{.CreatedAt}}", "--filter", "name=%s" % prefix,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    now = datetime.now(timezone.utc)
    for line in out.decode().splitlines():
        name, _, created = line.partition("\t")
        name = name.strip()
        if not name or name in _active:
            continue
        created_at = _parse_created_at(created)
        if created_at is None:
            # Never kill what can't be aged, but say so: if a Docker version changes the
            # format, every orphan would otherwise linger silently forever.
            logger.warning("cannot parse CreatedAt %r for %s; leaving it alone", created, name)
            continue
        if (now - created_at).total_seconds() <= MAX_LIVE_SANDBOX_AGE_S:
            continue
        await _docker("kill", name)


async def _kill_and_reap(container_name, proc):
    """Kill a container from outside and reap its `docker run` client process.

    Used when the job is cancelled (arq's `job_timeout`, or a worker shutdown): cancelling the
    awaiting task does not stop a subprocess, so without this the sandbox keeps running.
    """
    await kill(container_name)
    if proc is not None:
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            # `docker kill` fails if the daemon hasn't registered the named container yet (a
            # cancel right after spawn), leaving the client alive to start it moments later.
            # Killing the client at least stops it being started by a request still in flight.
            try:
                proc.kill()
            except ProcessLookupError:
                pass


async def run_in_container(payload_json, *, image, container_name,
                           memory_mb, cpus, pids_limit, wall_timeout_s, tmpfs_size_mb=16):
    """Run the sandbox for one submission, piping payload JSON to its stdin.

    The wall-clock timeout is defense-in-depth: the harness enforces a per-case
    SIGALRM inside the container, but if that's somehow evaded (or the container
    hangs on startup), `asyncio.wait_for` fires and we `docker kill` from outside —
    a control the sandboxed code can't touch. After killing we briefly drain output
    so a partial report isn't lost. Captured stdout is capped at 1 MB (a flood
    becomes `output_limit_exceeded` rather than eating worker memory). `"replace"`
    decoding tolerates non-UTF-8 bytes from hostile output without crashing.
    (DESIGN.md §5.2 steps 4–5)
    """
    args = build_run_args(
        image=image, container_name=container_name,
        memory_mb=memory_mb, cpus=cpus, pids_limit=pids_limit,
        tmpfs_size_mb=tmpfs_size_mb,
    )
    # Registered for the container's whole lifetime (including the drain-after-
    # kill below) so `sweep_orphans` can't mistake it for an orphan while it's
    # still ours — see `_active`'s docstring.
    _active.add(container_name)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        timed_out = False
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(payload_json.encode()), timeout=wall_timeout_s)
        except asyncio.TimeoutError:
            timed_out = True
            await kill(container_name)
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            except asyncio.TimeoutError:
                stdout, stderr = b"", b""

        truncated = len(stdout) > MAX_STDOUT_BYTES
        return ContainerResult(
            stdout=stdout[:MAX_STDOUT_BYTES].decode("utf-8", "replace"),
            stderr=stderr.decode("utf-8", "replace"),
            exit_code=proc.returncode if proc.returncode is not None else -1,
            timed_out=timed_out,
            stdout_truncated=truncated,
        )
    except asyncio.CancelledError:
        # arq cancelled this job (its job_timeout, or the worker is shutting down). A
        # cancelled task does not stop its subprocess, so kill the container ourselves. Shielded
        # so a second cancel can't abort the kill half-way; then let the cancellation continue.
        await asyncio.shield(_kill_and_reap(container_name, proc))
        raise
    finally:
        _active.discard(container_name)
