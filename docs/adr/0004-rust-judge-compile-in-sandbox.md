# ADR-0004: Rust judge — compile inside the sandbox, one process per case

- **Status:** Proposed — spike run (`judge/spike_rust/`), no kill-criteria hit; not yet implemented
- **Date:** 2026-09-27

## Context

We want Rust as a fourth `problems.language`, after `python`, `js` and `mysql` (DESIGN.md §13). Python and JS are interpreted: the harness `exec()`s or `vm`-runs the submission string and calls `fn(*json_args)` directly. Rust differs in two ways that matter:

1. **It has to be compiled**, and compiling has to happen in the same locked-down `docker run` as everything else (`backend/worker/docker_runner.py` `build_run_args`): `--network=none`, `--memory=<problem limit>` with swap disabled, `--cpus=1`, `--pids-limit=64`, `--read-only`, `--tmpfs /tmp:size=16m`, `--cap-drop=ALL`, `--user 1000:1000`. rustc is a heavy, multi-threaded toolchain, and the container can't download crates.
2. **It's statically typed**, so there's no way to call a function with an arbitrary JSON array. The harness has to *generate* a `main.rs` that decodes each argument into its declared Rust type, calls the user's function, and encodes the result.

The spike answers one question: can the compile and the run both fit inside the existing sandbox contract without making it weaker in any meaningful way?

## Evidence: measured in-session

Docker 29.5.2 on arm64 (Docker Desktop), `rust:1-slim` (rustc 1.98.1), with the exact `build_run_args` flags except where noted. Probe scripts are in `judge/spike_rust/`: `measure.sh`, `adversarial.sh` and `iso.sh`.

| Measurement | Result |
|---|---|
| Docker's default `--tmpfs /tmp` | **`noexec`**: the compiled binary fails with `Permission denied`. Needs `/tmp:size=16m,exec`. |
| First compile in a fresh container: user fn + generated main, prelude **pre-built as an rlib in the image**, `-O1 -C strip=symbols` | **130–180ms** (6 runs) |
| Same, but the prelude compiled per submission (`cold-O0`) | 130–210ms |
| `-O2` instead of `-O1` | about +140ms |
| rustc peak memory (container cgroup `memory.peak`) | **55–73MB**, well under the 256MB default limit |
| Binary size in tmpfs | 4.7MB unstripped → **~400KB** with `-C strip=symbols` |
| `--pids-limit=64` | Enough for rustc plus a linker, and for the per-case children below |
| Per-case process overhead (200 trivial cases, one child each) | **~1.3ms/case** (256ms total) |
| Image size | **1.12GB** uncompressed (vs 214MB python, 313MB js, 696MB mysql) |

(An early run showed first compiles of about 1s. That turned out to be the Docker Desktop VM's page cache warming up, and it didn't reproduce across 9 later fresh containers.)

### Hostile submissions

`adversarial.sh` runs each submission **in-process** (`main_gen.rs`: one binary calls every case, wrapped in `catch_unwind`):

| Submission | Outcome |
|---|---|
| `panic!` | Caught by `catch_unwind` → `runtime_error`, run continues ✅ |
| Stack overflow | **Process aborts** (`SIGABRT`, uncatchable): every case's result lost ❌ |
| `loop {}` | Hangs until the outer kill: every result lost ❌ |
| Allocation bomb | Cgroup OOM-kills the process: every result lost ❌ |
| `std::process::exit(0)` | Empty stdout: every result lost ❌ |
| `println!("{\"results\":[]}")` | **Corrupts the stdout protocol** (the worker would read a forged/garbled result) ❌ |
| Type error | rustc exits 1 in 21ms with a clean `error[E0308]` → maps to a compile-error verdict ✅ |
| `const fn` loop bomb | rustc gives up by itself ("constant evaluation is taking a long time") in 1.7s ✅ |
| Exponential type bomb | rustc OOM-killed (137) in 0.7s; contained by the memory cap ✅ |
| `include_str!("/etc/passwd")` | Compiles. This reads the image FS at compile time, which is the same exposure as Python's `open()` at runtime, so nothing new. |

`iso.sh` re-runs the same hostile set through **per-case process isolation** (`main_iso.rs`). The compiled binary re-execs itself once per case: the driver sends the child **only the input** and never `expected`, polls against `time_limit_ms`, classifies how the child exited, and does the comparison itself. Every case got the right verdict in a single run, and later cases still ran:

`panic` → `runtime_error` (with the panic message) · stack overflow → `runtime_error` ("has overflowed its stack") · `loop {}` → `time_limit_exceeded` · allocation bomb → `memory_limit_exceeded` (child SIGKILLed, driver survived) · `exit(0)` → `runtime_error` ("exited before returning") · forged stdout → `accepted`, with the forgery captured in the case's `stdout` field where it belongs.

## Decision (proposed)

Build `judge/harness_rs` as a Rust-specific judge. It speaks the same stdin-JSON-in/stdout-JSON-out protocol (§5.3), so nothing downstream of the sandbox changes. In outline:

1. **Image `judge/Dockerfile.rust`** (`rust:1-slim`). A dependency-free JSON **prelude** (value type, parser/printer, `FromJson`/`ToJson` for the param types we support) is **pre-compiled to an rlib at image build time**. No serde: `--network=none` rules out fetching crates, and a hand-rolled prelude keeps both compile time and the trusted surface small.
2. **Per submission**, the harness generates `main.rs` from the problem's `params`/`return_type` (or method signatures for operations; see Consequences). It runs `rustc --edition 2021 -C opt-level=1 -C strip=symbols --extern prelude=…` under a compile timeout, then runs the binary in **driver mode, with one child process per case**.
3. **Worker changes**: `"rust"` entries in `IMAGE_BY_LANGUAGE`, `STARTUP_SLACK_S_BY_LANGUAGE` (compile budget, ~2s to cover the rustc self-limit on const-eval bombs) and a new per-language **tmpfs `exec`** flag in both `docker_runner.build_run_args` and `k8s_runner` (an `emptyDir` isn't `noexec` by default; verify this on a real cluster).
4. **Verdicts**: a new `compile_error` status, with rustc's first errors in `error`. Right now "compilation fails" has no representation, because python/js surface syntax errors as `runtime_error` per case.
5. `opt-level=1`: nearly all of `-O2`'s runtime benefit on this kind of code, and ~140ms cheaper to compile.

## Alternatives considered

| Option | Why not |
|---|---|
| In-process judging with `catch_unwind` (the python/js shape) | Measured above: stack overflow, OOM, `exit()` and infinite loops each lose the *whole* run's results, and user stdout can corrupt the protocol. Python/JS get by in-process because their runtimes turn these into catchable exceptions; Rust doesn't. |
| serde / serde_json vendored into the image | Works offline if pre-built, but brings macro-heavy compile time and a much larger trusted surface for what's only a handful of types. |
| Compile in a separate, less-restricted container, run in the sandbox | Breaks the "one `run_in_container` → one `ContainerResult`" contract (`worker/runner.py`) and both runners. And rustc is the part *most* exposed to hostile input (type/const bombs), so it belongs *inside* the sandbox. |
| Use cargo | Pointless without crates, and it adds process fan-out, `CARGO_HOME` writes and startup cost. Calling `rustc` directly is enough. |
| `-O0` / `-O2` | `-O0` binaries run far slower on algorithmic workloads (unfair against `time_limit_ms`); `-O2` costs about +140ms of compile for little gain. |

## Consequences

- **`/tmp` becomes executable for Rust.** A submission could already run arbitrary code (that's the point), so exec-on-tmpfs doesn't grant a new capability. It only lets that code drop and run *another* binary, which stays inside the same cgroup/seccomp/no-network/no-caps box. Scope the flag to `language == "rust"` so python/js/mysql keep `noexec`.
- **Compile memory counts against `memory_limit_mb`.** That's fine at the 256MB default (rustc peaked at ~73MB), but a problem declaring e.g. 64MB could fail to *compile*. Either set a per-language floor or have `ProblemIn` validate a minimum for Rust.
- **Per-case `runtime_ms` includes about 1.2ms of spawn overhead** in the spike (timed from the driver). The real harness should time inside the child.
- **Memory attribution**: the spike relied on the cgroup OOM-killer picking the child (it's the largest process). A per-child `RLIMIT_AS` would make `memory_limit_exceeded` deterministic, but std has no `setrlimit`, so it needs `libc` in the prelude or a small `sh -c 'ulimit -v …'` wrapper.
- **Anti-cheat improves slightly over python/js**: the child never sees `expected`, so a submission can't forge `accepted` by inspecting the case. (In k8s mode the payload file is still mounted and readable, the same as for Python today.)
- **A 1.12GB image** adds cold-pull cost on new nodes. A `rustup --profile minimal` build on `debian:slim` is the obvious trim; not measured.
- **Typed glue is the real work.** Function mode needs a `params[].type` → Rust type table (the existing strings are Python-flavored: `List[int]`, `str`, …) with explicit `i32`/`i64` choices and float formatting that matches harness.py's output. Operations mode (all 5 current seed problems) additionally needs machine-readable method signatures, which today exist only inside the Python `starter_code`, so it's a schema addition to `ProblemIn`.
- **Rust problems are their own problems**, like JS ones: a problem has one `language` and one `starter_code`. Making existing problems solvable in Rust is a separate, larger data-model change and out of scope here.
- **Unverified on k8s**: `emptyDir` exec semantics, and compile time under gVisor (`runsc` adds syscall overhead that rustc is sensitive to). Measure both before relying on the k8s runner for Rust.
