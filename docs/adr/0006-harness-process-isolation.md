# ADR-0006: The interpreted harnesses run the submission in a child process

- **Status:** Accepted: implemented in `judge/harness.py`, `judge/harness.js`, `judge/harness_rs/harness.rs` (the k8s payload delete), `backend/worker/aggregate.py`, and `backend/worker/k8s_runner.py`
- **Date:** 2026-09-28

## Context

The Rust harness (ADR-0004) runs each submission in its own child process: the trusted
parent holds the test cases and `expected`, and the child only ever receives inputs. The
Python and JavaScript harnesses did not. Each ran the submission *in the harness process
itself* — Python with `exec()` into a namespace, JavaScript with `vm.runInContext()` — the
same process that held every case's `expected` and owned the container's stdout, the stream
the worker parses into the verdict.

That gave a submission two ways to be judged **accepted without solving anything**, confirmed
against the real judge images:

1. **Read the answers.** Running in the grader's process, a submission could reach `expected`
   — Python by walking the call stack, JavaScript by escaping the `vm` to the host realm
   (`vm` is explicitly not a security boundary) — and simply return it.
2. **Forge the report.** A submission could write its own `{"results": [...]}` document to the
   real stdout descriptor and exit before the harness wrote the true one, and the worker
   accepted it. It didn't even need the case ids: the aggregator marked a run accepted when no
   row was non-passing, so `{"results": []}` sufficed. Runtime percentiles were forgeable the
   same way.

Both are ordinary submissions through the normal Run/Submit path; no special access. SQL is a
query (no code) and Rust already isolated the submission, so both were unaffected.

The container is still the boundary that contains *damage* (no network, read-only root, dropped
caps, non-root). This is a different problem: the integrity of the verdict against code that
runs to completion and returns a value, which the container is not meant to protect.

## Decision

Give the Python and JS harnesses the Rust harness's shape: a **trusted parent** and an
**untrusted child**, split across a real process boundary.

- The **parent** parses the payload, holds every case's `expected`, and is the only process
  that writes the report to stdout. For every fixed-answer comparison mode it computes pass/fail
  itself, with `compare()`, against an `expected` the child never receives — so a fabricated
  return value cannot match without already knowing the answer.
- The **child** is spawned once per run (a re-exec of the same harness in `--child` mode). It
  receives only the submission and, one at a time, each case's **input**. It returns the value
  the submission produced over a *separate private pipe* (a passed fd on Python, stdio fd 3 on
  Node); its own stdout and stderr go to `/dev/null`, so a submission writing to those raw
  descriptors reaches nothing the parent reads. The submission *can* write to the result pipe
  itself (that's how it returns values, and its runtime — Python builtins, Node's `require` — can
  reach the fd), but that buys it nothing: the parent still computes pass/fail from an `expected`
  the child never sees, and treats a frame it can't parse, or one that arrives out of turn, as a
  desynced child — that case fails and a fresh child runs the next — rather than letting a bad
  frame raise and lose the whole run. One child for the whole run keeps the per-case cost a pipe
  round trip, not a process launch (Python startup is ~20–30ms); the parent respawns it only
  after a timeout or a crash. Each child is its own process-group leader (`start_new_session` /
  `detached`), and the parent kills the **group**, so any grandchildren the submission spawned
  are reaped with it.
- **Timeouts** are enforced by the parent: it kills a child that doesn't answer within the case
  limit and reports `time_limit_exceeded` for that case — so a submission that defeats the
  harness's own in-process alarm (e.g. `signal.signal(SIGALRM, SIG_IGN)`) is still bounded per
  case, where before only the worker's container-wide wall-clock kill caught it. A crash is a
  per-case `runtime_error`. Either way the remaining cases still run.
- **Defence in depth:** the parent is marked non-dumpable on Python (`prctl(PR_SET_DUMPABLE, 0)`)
  so the child can't read its memory via `/proc`; in-container the container's non-root ptrace
  rules block that for every language regardless. And `worker/aggregate.py` refuses a report that
  doesn't carry exactly one row per case sent (an early stop only on a failure) — so even a
  malformed or partial report can never read as "every case passed".
- **Kubernetes payload.** The k8s runner used to mount the payload as a read-only ConfigMap file
  in the judge container, which the harness therefore couldn't delete before running the
  submission — leaving `expected` readable at a fixed path to a same-uid case process (the Rust
  harness runs each case as the same uid too, so it had this gap even though it always isolated
  the case). An init container now copies it into a writable `emptyDir` the judge mounts instead,
  and the parent unlinks it before any user code runs (as it already does for the stdin path);
  all three harnesses — Python, JS and Rust — do this, and **fail closed**: if the delete fails
  they refuse the run as a judge fault rather than grade with `expected` still readable (the
  emptyDir is world-writable and the parent owns the file, so this never fires in practice — it
  guards a future misconfiguration). That `emptyDir` is node-backed, **not**
  `medium: Memory`: a tmpfs emptyDir's bytes are charged to the judge container's memory cgroup,
  which would silently shrink the `memory_limit_mb` a submission is graded under. The payload
  holds only `expected` (not a secret) and is gone before the submission's peak memory is
  measured, so node-local ephemeral storage is the right home for it; `/tmp` stays tmpfs.

## Consequences

- Python and JS submissions can no longer read the expected answers or forge the verdict;
  regression tests pin all three properties (read, forge, blocked memory read) per language.
- A defeated per-case alarm and a fork bomb are now contained as clean per-case verdicts rather
  than a container-level wall-clock kill; two sandbox tests that pinned the old behaviour were
  updated to accept either.
- A submission's raw fd-1 writes are discarded rather than captured. `print()` / `console.log`
  is still captured and returned, which is all the `stdout` field ever showed.
- One residual, documented limit: an `operations` `custom_validator` calls into the *live*
  object, which exists only in the child, so the (operator-authored, trusted) validator runs
  there and decides pass/fail. A `custom_validator` problem must therefore not rely on `expected`
  being unreadable by the submission. No bundled problem uses `custom_validator`. Closing this
  would mean moving the live object across the process boundary (it can't) or a third,
  validator-only process; deferred as a follow-up.
- **Rejected:** a fresh process *per case* (as Rust does). Python's per-process startup makes
  that materially slower for a problem with many cases; one persistent child with respawn keeps
  the isolation while paying process startup only on a timeout or crash.
