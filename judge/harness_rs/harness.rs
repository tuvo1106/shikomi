//! Judge harness (Rust): compiles a submission and judges it against the test
//! cases inside the sandbox (DESIGN.md §5.3, §13; the design and its
//! measurements are ADR-0004).
//!
//! Same protocol as judge/harness.py and judge/harness.js: the payload JSON
//! arrives on stdin (or in JUDGE_PAYLOAD_FILE, the k8s runner's mounted
//! ConfigMap), and exactly one `{"results": [...]}` document goes to stdout
//! with the same per-case shape. The worker's aggregator doesn't know which
//! language judged.
//!
//! What's different is that Rust has to be *compiled*, and a compiled program
//! fails in ways an interpreter never lets out:
//!
//! 1. **Generate.** The user's code is written to `solution.rs`, followed by a
//!    few lines of glue (`glue()`) that call `function_name` with each argument
//!    decoded by `shikomi_prelude::arg`. The glue never spells out a type;
//!    rustc infers each one from the user's own signature.
//! 2. **Compile** once with `rustc` (not cargo: no crates, since the sandbox
//!    has no network, and cargo would only add processes and startup time).
//!    A compile failure becomes a single `runtime_error` row carrying rustc's
//!    diagnostics, the same shape harness.py uses for a `SyntaxError`
//!    (DESIGN.md §5.3).
//! 3. **Run one fresh process per test case.** This is the core decision. In
//!    one shared process, a stack overflow (an uncatchable abort), an
//!    allocation failure, `std::process::exit` or an infinite loop would each
//!    lose *every* case's result. The spike measured all of these. A process
//!    per case contains each failure to its own case for ~1.3ms of spawn
//!    overhead. It also keeps `expected` out of reach: the child gets only the
//!    input, and the comparison happens here in the trusted process.
//!
//! Limits applied to each case's process: wall time `time_limit_ms` (enforced
//! here, with SIGKILL), address space `memory_limit_mb` (`RLIMIT_AS`, so an
//! allocation bomb fails that case's allocation instead of drawing the
//! container's OOM killer), and a 64MB stack (`RLIMIT_STACK`, since deep
//! recursion is normal in DFS-style solutions and 8MB is the default). The
//! container's own limits (memory cgroup, pids, no network, read-only root)
//! still bound everything, the harness included.
//!
//! `runtime_ms` is measured from *this* side, spawn to exit, so a submission
//! can't under-report its own time. The cost is a ~1ms process-startup floor.
//! That floor is the same for every submission to a problem, and a problem
//! has exactly one language, so runtime percentiles still compare like with
//! like.

use shikomi_prelude::Json;
use std::collections::BTreeMap;
use std::fs;
use std::io::{Read, Write};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::Path;
use std::process::{Command, ExitStatus, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

const TRUNC: usize = 4096; // per-field cap for output/stdout/error (DESIGN.md §5.3)
const WORK: &str = "/tmp/judge";
const PRELUDE_DIR: &str = "/opt/judge/lib";
/// rustc's own hard stop. ADR-0004 measured ~150ms for a normal submission;
/// the slowest hostile input it tried (a `const fn` loop) hit rustc's own
/// const-eval limit at 1.7s. The worker's wall budget reserves time for this
/// (`STARTUP_SLACK_S_BY_LANGUAGE["rust"]`, backend/app/judge_budget.py).
const COMPILE_TIMEOUT: Duration = Duration::from_secs(10);
const CASE_STACK_BYTES: u64 = 64 << 20;

// libc's kill/setrlimit, declared by hand: std already links libc, and
// pulling in the `libc` crate isn't an option with no network at build time.
// The values are Linux ABI constants, identical on x86_64 and aarch64.
#[repr(C)]
struct RLimit {
    cur: u64,
    max: u64,
}
unsafe extern "C" {
    fn kill(pid: i32, sig: i32) -> i32;
    fn setrlimit(resource: i32, rlim: *const RLimit) -> i32;
    fn setpgid(pid: i32, pgid: i32) -> i32;
    fn waitpid(pid: i32, status: *mut i32, options: i32) -> i32;
}
const WNOHANG: i32 = 1;
const SIGKILL: i32 = 9;
const SIGSEGV: i32 = 11;
const SIGABRT: i32 = 6;
const RLIMIT_STACK: i32 = 3;
const RLIMIT_AS: i32 = 9;

fn truncate(s: &str) -> String {
    if s.len() <= TRUNC {
        return s.to_string();
    }
    let mut end = TRUNC;
    while !s.is_char_boundary(end) {
        end -= 1; // never cut a multi-byte char in half
    }
    format!("{}…(truncated)", &s[..end])
}

// --- comparison modes (mirrors harness.py's compare(), DESIGN.md §5.4) -----

fn float_equal(a: &Json, b: &Json, eps: f64) -> bool {
    match (a, b) {
        (Json::Arr(x), Json::Arr(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(p, q)| float_equal(p, q, eps))
        }
        _ => match (a.as_f64(), b.as_f64()) {
            (Some(p), Some(q)) => (p.is_nan() && q.is_nan()) || (p - q).abs() <= eps,
            _ => a == b,
        },
    }
}

/// Multiset equality at the top level only; nested lists still compare in
/// order, the same semantics as harness.py/harness.js. O(n²) matching is
/// fine for the sizes an `unordered` answer has. There's no hash or total
/// order on `Json` to sort by, since floats and `1 == 1.0` get in the way.
fn multiset_equal(a: &Json, b: &Json) -> bool {
    match (a, b) {
        (Json::Arr(x), Json::Arr(y)) => {
            if x.len() != y.len() {
                return false;
            }
            let mut used = vec![false; y.len()];
            x.iter().all(|item| {
                match (0..y.len()).find(|&j| !used[j] && y[j] == *item) {
                    Some(j) => {
                        used[j] = true;
                        true
                    }
                    None => false,
                }
            })
        }
        _ => a == b,
    }
}

/// Unknown modes fall back to exact equality, as harness.py and harness.js do.
/// `custom_validator` never reaches here: ProblemIn rejects it for any
/// language but Python.
fn compare(actual: &Json, expected: &Json, comparison: &Json) -> bool {
    match comparison.get("mode") {
        Json::Str(m) if m == "unordered" => multiset_equal(actual, expected),
        Json::Str(m) if m == "float_tolerance" => {
            float_equal(actual, expected, comparison.get("epsilon").as_f64().unwrap_or(1e-6))
        }
        Json::Str(m) if m == "any_of" => match expected {
            Json::Arr(options) => options.iter().any(|o| actual == o),
            other => actual == other,
        },
        _ => actual == expected,
    }
}

// --- results ----------------------------------------------------------------

struct CaseResult {
    id: Json,
    status: &'static str,
    runtime_ms: f64,
    output: Option<String>,
    stdout: String,
    error: Option<String>,
}

impl CaseResult {
    fn error_row(id: Json, error: String) -> CaseResult {
        CaseResult { id, status: "runtime_error", runtime_ms: 0.0, output: None, stdout: String::new(), error: Some(error) }
    }

    fn to_json(&self) -> Json {
        let mut m = BTreeMap::new();
        m.insert("test_case_id".into(), self.id.clone());
        m.insert("status".into(), Json::Str(self.status.into()));
        // 3 decimals, matching harness.py's round(..., 3)
        m.insert("runtime_ms".into(), Json::Num((self.runtime_ms * 1000.0).round() / 1000.0));
        m.insert("output".into(), self.output.as_deref().map_or(Json::Null, |s| Json::Str(truncate(s))));
        m.insert("stdout".into(), Json::Str(truncate(&self.stdout)));
        m.insert("error".into(), self.error.as_deref().map_or(Json::Null, |s| Json::Str(truncate(s))));
        Json::Obj(m)
    }
}

// --- process supervision ----------------------------------------------------

/// How a supervised child process ended.
enum Exit {
    Finished(ExitStatus, Duration),
    TimedOut,
}

/// Wait for `child` for at most `limit`, SIGKILLing it on expiry.
///
/// `Child::wait` blocks and has no timeout, so it runs on a helper thread and
/// the caller waits on a channel with `recv_timeout`. The elapsed time is taken
/// the moment `wait` returns. A poll loop would round every runtime up to its
/// sleep interval, or spin on a CPU the child needs (`--cpus=1`).
fn supervise(child: std::process::Child, start: Instant, limit: Duration) -> Exit {
    let pid = child.id() as i32;
    let (tx, rx) = mpsc::channel();
    let waiter = std::thread::spawn(move || {
        let mut child = child;
        let status = child.wait();
        let _ = tx.send((status, start.elapsed()));
    });
    let exit = match rx.recv_timeout(limit) {
        Ok((Ok(status), took)) => Exit::Finished(status, took),
        Ok((Err(_), _)) => Exit::TimedOut, // wait() itself failed; treat as a lost process
        Err(_) => {
            // Still running at the deadline. The waiter hasn't reaped it (it would
            // have sent), so the pid still names our child and can't have been reused.
            unsafe { kill(pid, SIGKILL) };
            Exit::TimedOut
        }
    };
    let _ = waiter.join();
    exit
}

/// Clean up whatever a finished case left running.
///
/// A case can spawn processes that outlive it (`Command::new("sleep").spawn()`
/// and return). They'd keep holding slots under the container's
/// `--pids-limit`, so a later case couldn't spawn, or couldn't even be
/// started. So: SIGKILL the case's whole process group, then reap. The harness
/// is the container's PID 1 (it's the ENTRYPOINT), so orphans are reparented
/// to *it*, and until it `waitpid`s them their zombie entries still count
/// against the limit. A process that deliberately leaves the group
/// (`setsid`) escapes the kill, but it's still boxed in by the pids limit and
/// dies with the container.
fn end_case(pgid: i32) {
    let mut status = 0;
    unsafe {
        kill(-pgid, SIGKILL); // negative pid = the whole group; ESRCH if already empty
        // *Blocking* wait on that group until none of its members are left
        // (ECHILD). SIGKILL is asynchronous, so a non-blocking reap here would
        // race the dying processes and leave most of them holding their pids.
        // Every member has just been killed, so this returns promptly.
        while waitpid(-pgid, &mut status, 0) > 0 {}
        // Then sweep up any other orphan that already exited (e.g. one that
        // left the group with setsid and has since died).
        while waitpid(-1, &mut status, WNOHANG) > 0 {}
    }
}

/// Read up to TRUNC+1 bytes of a captured output file: enough to decide
/// whether it needs the "…(truncated)" marker, without loading a submission's
/// multi-megabyte print loop into memory.
fn read_capped(path: &Path) -> String {
    let mut buf = Vec::new();
    if let Ok(f) = fs::File::open(path) {
        let _ = f.take(TRUNC as u64 + 1).read_to_end(&mut buf);
    }
    String::from_utf8_lossy(&buf).into_owned()
}

// --- compile -------------------------------------------------------------------

fn is_identifier(s: &str) -> bool {
    let mut chars = s.chars();
    matches!(chars.next(), Some(c) if c == '_' || c.is_ascii_alphabetic())
        && chars.all(|c| c == '_' || c.is_ascii_alphanumeric())
}

/// The generated tail of `solution.rs`: a `main` that calls the user's
/// function with `arity` decoded arguments. It goes *after* the user's code so
/// rustc's line numbers for the user's own mistakes match what they typed.
fn glue(function_name: &str, arity: usize) -> String {
    let args: Vec<String> =
        (0..arity).map(|i| format!("::shikomi_prelude::arg(__args, {})?", i)).collect();
    format!(
        "\n\n// ---- generated by the shikomi judge harness (not part of your code) ----\n\
         fn main() {{\n    ::shikomi_prelude::__run(|__args| Ok(::shikomi_prelude::ret({}({}))));\n}}\n",
        function_name,
        args.join(", ")
    )
}

/// Compile `solution.rs` in WORK. `Err` carries the message for the single
/// runtime_error row.
fn compile() -> Result<(), String> {
    let stderr_path = Path::new(WORK).join("rustc.stderr");
    let child = Command::new("rustc")
        .current_dir(WORK) // relative path, so diagnostics read "solution.rs:3:5"
        .args(["--edition", "2024", "--crate-type", "bin", "--crate-name", "solution"])
        // opt-level=1: nearly all of O2's runtime on algorithmic code, ~140ms cheaper to
        // compile (ADR-0004). overflow-checks stay on, so `i32` overflow panics with
        // "attempt to add with overflow" instead of silently wrapping into a wrong answer.
        .args(["-C", "opt-level=1", "-C", "overflow-checks=on", "-C", "strip=symbols", "-C", "debuginfo=0"])
        .args(["-A", "warnings", "--color", "never"])
        .args(["-L", PRELUDE_DIR, "--extern"])
        .arg(format!("shikomi_prelude={}/libshikomi_prelude.rlib", PRELUDE_DIR))
        .args(["-o", "solution", "solution.rs"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(fs::File::create(&stderr_path).map_err(|e| format!("judge: {}", e))?)
        .spawn()
        .map_err(|e| format!("judge: could not start rustc: {}", e))?;
    match supervise(child, Instant::now(), COMPILE_TIMEOUT) {
        Exit::TimedOut => Err(format!("Compilation timed out after {}s", COMPILE_TIMEOUT.as_secs())),
        Exit::Finished(status, _) if status.success() => Ok(()),
        Exit::Finished(status, _) if status.signal() == Some(SIGKILL) => {
            Err("Compilation ran out of memory".into())
        }
        Exit::Finished(_, _) => {
            let diag = fs::read_to_string(&stderr_path).unwrap_or_default();
            Err(format!("Compile error:\n{}", diag.trim_end()))
        }
    }
}

// --- run one case ---------------------------------------------------------------

fn run_case(tc: &Json, time_limit: Duration, memory_bytes: Option<u64>, comparison: &Json) -> CaseResult {
    let id = tc.get("id").clone();
    let work = Path::new(WORK);
    let (result_path, out_path, err_path) =
        (work.join("case.result"), work.join("case.stdout"), work.join("case.stderr"));
    let _ = fs::remove_file(&result_path);

    let mut cmd = Command::new(work.join("solution"));
    cmd.env(shikomi_prelude::RESULT_ENV, &result_path)
        .stdin(Stdio::piped())
        // Files, not pipes: a pipe must be drained while the child runs, or a chatty
        // submission blocks on a full pipe and reads as a false time_limit_exceeded.
        .stdout(fs::File::create(&out_path).expect("create case.stdout"))
        .stderr(fs::File::create(&err_path).expect("create case.stderr"));
    unsafe {
        // Runs in the forked child just before exec. setrlimit is async-signal-safe.
        // cur == max, so the submission can't raise its own limit back up.
        cmd.pre_exec(move || {
            // Own process group (pgid = the case's pid), so `end_case` can kill
            // everything the case started with one signal.
            setpgid(0, 0);
            let stack = RLimit { cur: CASE_STACK_BYTES, max: CASE_STACK_BYTES };
            setrlimit(RLIMIT_STACK, &stack);
            if let Some(bytes) = memory_bytes {
                let mem = RLimit { cur: bytes, max: bytes };
                setrlimit(RLIMIT_AS, &mem);
            }
            Ok(())
        });
    }

    let start = Instant::now();
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => return CaseResult::error_row(id, format!("judge: could not start the program: {}", e)),
    };
    let input = tc.get("input").dump();
    if let Some(mut stdin) = child.stdin.take() {
        // Ignore EPIPE: a program that exits without reading its input is judged
        // by how it exited, not by our write failing.
        let _ = stdin.write_all(input.as_bytes());
    } // dropping stdin closes it, so the child's read_to_string sees EOF
    let pid = child.id() as i32;
    let exit = supervise(child, start, time_limit);
    end_case(pid);
    let stdout = read_capped(&out_path);

    let (status, took) = match exit {
        Exit::TimedOut => {
            return CaseResult {
                id,
                status: "time_limit_exceeded",
                runtime_ms: time_limit.as_secs_f64() * 1000.0,
                output: None,
                stdout,
                error: None,
            };
        }
        Exit::Finished(status, took) => (status, took),
    };
    let runtime_ms = took.as_secs_f64() * 1000.0;
    let fail = |status: &'static str, error: Option<String>| CaseResult {
        id: id.clone(), status, runtime_ms, output: None, stdout: stdout.clone(), error,
    };

    // The per-case program's result file: {"ok": v} | {"panic": msg} | {"decode": msg}.
    let result = fs::read_to_string(&result_path).ok().and_then(|s| Json::parse(&s).ok());
    match result.as_ref() {
        Some(r) if has_key(r, "ok") => {
            let actual = r.get("ok");
            let passed = compare(actual, tc.get("expected"), comparison);
            return CaseResult {
                id,
                status: if passed { "passed" } else { "wrong_answer" },
                runtime_ms,
                output: Some(actual.dump()),
                stdout,
                error: None,
            };
        }
        Some(r) => {
            if let Json::Str(msg) = r.get("panic") {
                return fail("runtime_error", Some(msg.clone()));
            }
            if let Json::Str(msg) = r.get("decode") {
                return fail("runtime_error", Some(format!("could not decode the test case: {}", msg)));
            }
        }
        None => {}
    }

    // No result: the process died without returning. Tell the user how.
    let stderr = read_capped(&err_path);
    if stderr.contains("memory allocation of") {
        // RLIMIT_AS refused an allocation, and Rust's alloc-error handler aborted.
        return fail("memory_limit_exceeded", None);
    }
    if stderr.contains("has overflowed its stack") {
        return fail("runtime_error", Some("stack overflow (recursion too deep?)".into()));
    }
    match (status.code(), status.signal()) {
        // We didn't send this SIGKILL (a timeout returns above), so it came from the
        // container's OOM killer: the cgroup filled up before RLIMIT_AS tripped.
        (_, Some(SIGKILL)) => fail("memory_limit_exceeded", None),
        (_, Some(SIGSEGV)) => fail("runtime_error", Some("segmentation fault".into())),
        (_, Some(SIGABRT)) => fail("runtime_error", Some(format!("aborted{}", stderr_suffix(&stderr)))),
        (_, Some(sig)) => fail("runtime_error", Some(format!("terminated by signal {}", sig))),
        (Some(code), _) => fail(
            "runtime_error",
            Some(format!("the program exited (code {}) before returning a value{}", code, stderr_suffix(&stderr))),
        ),
        (None, None) => fail("runtime_error", Some("the program ended without returning a value".into())),
    }
}

/// Whether `key` is present at all. `{"ok": null}` is a legitimate result
/// (a function returning `()` or `None`), and `get` can't tell a present
/// `null` from a missing key.
fn has_key(r: &Json, key: &str) -> bool {
    matches!(r, Json::Obj(m) if m.contains_key(key))
}

fn stderr_suffix(stderr: &str) -> String {
    let t = stderr.trim();
    if t.is_empty() { String::new() } else { format!(": {}", t) }
}

// --- main ---------------------------------------------------------------------

fn run(payload: &Json) -> Vec<CaseResult> {
    let cases = payload.get("test_cases").as_arr();
    let first_id = cases.first().map_or(Json::Int(0), |c| c.get("id").clone());

    let kind = match payload.get("kind") {
        Json::Str(k) => k.as_str(),
        _ => "function",
    };
    if kind != "function" {
        // Scope (DESIGN.md §13): function mode only, like harness.js. ProblemIn
        // rejects anything else for Rust at seed time; this is the backstop.
        return vec![CaseResult::error_row(first_id, format!("kind '{}' is not supported by the Rust harness", kind))];
    }
    let function_name = match payload.get("function_name") {
        Json::Str(f) if is_identifier(f) => f.clone(),
        // The name is pasted into generated source, so anything but a plain
        // identifier is refused rather than compiled.
        other => return vec![CaseResult::error_row(first_id, format!("judge: invalid function_name {}", other.dump()))],
    };
    // Arity comes from the problem's declared params (the types are display-only; see
    // prelude.rs). With none declared, fall back to the first case's argument count.
    let arity = match payload.get("params").as_arr().len() {
        0 => cases.first().map_or(0, |c| c.get("input").as_arr().len()),
        n => n,
    };
    let user_code = match payload.get("user_code") {
        Json::Str(s) => s.as_str(),
        _ => "",
    };

    if let Err(e) = fs::create_dir_all(WORK)
        .and_then(|_| fs::write(Path::new(WORK).join("solution.rs"), format!("{}{}", user_code, glue(&function_name, arity))))
    {
        return vec![CaseResult::error_row(first_id, format!("judge: could not write the source: {}", e))];
    }
    if let Err(e) = compile() {
        // One row, reported against the real case count by the aggregator
        // (0/N), the same as a Python SyntaxError.
        return vec![CaseResult::error_row(first_id, e)];
    }

    let time_limit = Duration::from_millis(match payload.get("time_limit_ms") {
        Json::Int(n) => (*n).max(1) as u64,
        _ => 2000,
    });
    let memory_bytes = match payload.get("memory_limit_mb") {
        Json::Int(n) if *n > 0 => Some((*n as u64) << 20),
        _ => None,
    };
    let comparison = payload.get("comparison");
    let stop_on_first_failure = payload.get("stop_on_first_failure") == &Json::Bool(true);

    let mut results = Vec::new();
    for tc in cases {
        let r = run_case(tc, time_limit, memory_bytes, comparison);
        let failed = r.status != "passed";
        results.push(r);
        if failed && stop_on_first_failure {
            break;
        }
    }
    results
}

fn main() {
    let raw = match std::env::var("JUDGE_PAYLOAD_FILE") {
        Ok(path) => fs::read_to_string(path),
        Err(_) => {
            let mut s = String::new();
            std::io::stdin().read_to_string(&mut s).map(|_| s)
        }
    };
    let payload = match raw.map_err(|e| e.to_string()).and_then(|s| Json::parse(&s)) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("harness: invalid payload JSON: {}", e);
            std::process::exit(2); // same exit code as harness.js for a bad payload
        }
    };
    let results: Vec<Json> = run(&payload).iter().map(CaseResult::to_json).collect();
    let mut top = BTreeMap::new();
    top.insert("results".to_string(), Json::Arr(results));
    println!("{}", Json::Obj(top).dump());
}
