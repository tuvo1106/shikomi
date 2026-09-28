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
//!    rustc infers each one from the user's own signature. An operations
//!    problem gets `operations_glue()` instead, which constructs `class_name`
//!    and dispatches each op name to a method the same way.
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
//!    overhead. It also keeps `expected` away from the submission: the child
//!    gets only the input (on stdin, with a scrubbed environment), and the
//!    comparison happens here in the trusted process. As defense in depth the
//!    harness also marks itself non-dumpable, which refuses same-uid access to
//!    its `/proc/1/environ` (readable without it; `/proc/1/mem` was already
//!    refused by Docker's defaults, measured).
//!    On k8s the payload arrives as a file (JUDGE_PAYLOAD_FILE) rather than on
//!    stdin, at a fixed path in a volume the case's process shares (same uid).
//!    An init container copies it into a *writable* volume, so this parent reads
//!    it and then unlinks it up front (see `main`), before any case's process
//!    runs — otherwise a submission could hardcode that path and read `expected`
//!    even though the path never appears in a case's env.
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
/// rustc's hard stop when the payload doesn't carry one. The worker sends
/// `compile_timeout_s` from backend/app/sandbox.py's
/// `RUST_COMPILE_TIMEOUT_S`, the same number its wall budget reserves, so the
/// two can't drift apart. ADR-0004 measured ~150ms for a normal submission; the
/// slowest hostile input it tried (a `const fn` loop) hit rustc's own
/// const-eval limit at 1.7s.
const DEFAULT_COMPILE_TIMEOUT_S: u64 = 10;
/// The real toolchain binary, by absolute path. The rust base image puts
/// rustup's proxy (`/usr/local/cargo/bin/rustc`) first on PATH, and the proxy
/// re-resolves the toolchain on every call; judge/Dockerfile.rust links the
/// real binary here.
const RUSTC: &str = "/usr/local/bin/rustc";
/// The whole environment a case's process gets. Nothing is inherited, so
/// runner-specific variables (JUDGE_PAYLOAD_FILE on k8s) never reach it.
const CASE_ENV: [(&str, &str); 3] = [
    ("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
    ("HOME", "/tmp"),
    ("TMPDIR", "/tmp"),
];
const CASE_STACK_BYTES: u64 = 64 << 20;
/// How much of a case's stderr is kept: enough for Rust's abort messages
/// ("memory allocation of … failed", "has overflowed its stack") that
/// classify a crash, which the runtime prints before anything else.
const STDERR_KEEP: usize = 8192;
/// How long to wait for a case's output pipes to close after the case (and its
/// process group) is gone. Only a process that escaped the group with `setsid`
/// can still be holding them; its output is then abandoned, not waited on.
const DRAIN_GRACE: Duration = Duration::from_millis(500);
/// Exit code for a judge-side fault. It exits with no results, so the worker's
/// `aggregate()` reports `judge_error` rather than blaming the submission.
const JUDGE_FAULT_EXIT: i32 = 3;

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
    fn prctl(option: i32, arg2: u64, arg3: u64, arg4: u64, arg5: u64) -> i32;
}
const PR_SET_DUMPABLE: i32 = 4;
const WNOHANG: i32 = 1;
const SIGKILL: i32 = 9;
const SIGSEGV: i32 = 11;
const SIGABRT: i32 = 6;
const RLIMIT_STACK: i32 = 3;
const RLIMIT_AS: i32 = 9;

/// Abort the run for a fault in the *judge*, not the submission: a missing
/// toolchain, a /tmp that can't be written or executed, a bad payload. Writing
/// a per-case `runtime_error` here would tell the user their code failed when
/// the sandbox is broken, and would give the operator no signal at all.
fn judge_fault(msg: String) -> ! {
    eprintln!("harness: {}", msg);
    std::process::exit(JUDGE_FAULT_EXIT);
}

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
            // `p == q` first: inf - inf is NaN, which no tolerance accepts, but an
            // infinite answer that matches an infinite expectation is right.
            (Some(p), Some(q)) => p == q || (p.is_nan() && q.is_nan()) || (p - q).abs() <= eps,
            _ => a == b,
        },
    }
}

/// Multiset equality at the top level only; nested lists still compare in
/// order, the same semantics as harness.py/harness.js.
///
/// Sorts each side's canonical keys (`Json::canonical`, where two values are
/// equal exactly when their keys are) and compares the sorted lists, which is
/// O(n log n). The comparison runs untimed, bounded only by the worker's outer
/// wall clock, and a pairwise O(n²) match took ~6s at 100k elements. That turned
/// a correct answer into a whole-run time_limit_exceeded; harness.py's
/// `_multiset_equal` records the same bug and fix.
fn multiset_equal(a: &Json, b: &Json) -> bool {
    match (a, b) {
        (Json::Arr(x), Json::Arr(y)) => {
            if x.len() != y.len() {
                return false;
            }
            let mut xs: Vec<String> = x.iter().map(Json::canonical).collect();
            let mut ys: Vec<String> = y.iter().map(Json::canonical).collect();
            xs.sort_unstable();
            ys.sort_unstable();
            xs == ys
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

/// Read a child's output pipe to EOF on a thread, keeping the first `keep`
/// bytes and discarding the rest, and hand back the kept text.
///
/// It keeps reading after the cap: a reader that stopped would leave the
/// child blocked on a full pipe (or killed by SIGPIPE), which would misreport a
/// chatty but correct solution. The caller waits at most `DRAIN_GRACE` for the
/// result, since only a process that escaped the group kill can hold the pipe
/// open past the case.
fn drain<R: Read + Send + 'static>(pipe: Option<R>, keep: usize) -> mpsc::Receiver<String> {
    let (tx, rx) = mpsc::channel();
    if let Some(mut pipe) = pipe {
        std::thread::spawn(move || {
            let mut kept = Vec::new();
            let mut buf = [0u8; 16384];
            loop {
                match pipe.read(&mut buf) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => {
                        let room = keep.saturating_sub(kept.len());
                        kept.extend_from_slice(&buf[..n.min(room)]);
                    }
                }
            }
            let _ = tx.send(String::from_utf8_lossy(&kept).into_owned());
        });
    } else {
        let _ = tx.send(String::new());
    }
    rx
}

// --- compile -------------------------------------------------------------------

fn is_identifier(s: &str) -> bool {
    let mut chars = s.chars();
    matches!(chars.next(), Some(c) if c == '_' || c.is_ascii_alphabetic())
        && chars.all(|c| c == '_' || c.is_ascii_alphanumeric())
}

/// The generated tail of `solution.rs`: an entry point that calls the user's
/// function with `arity` decoded arguments. It goes *after* the user's code so
/// rustc's line numbers for the user's own mistakes match what they typed.
///
/// The entry point is an exported C `main` inside a module, with `#![no_main]`
/// prepended to the user's first line (`source`, which keeps line numbers
/// intact). A Rust `fn main` of ours would collide with the one users often keep
/// for local testing. Every path is fully qualified
/// (`::core::result::Result::Ok`), so a user's `use MyEnum::*` that brings in a
/// variant named `Ok` can't capture it either. The module is a child of the
/// crate root, so it can call the user's function even when it's private.
///
/// The glue also defines the node structs the problem declares (`node_structs`),
/// which is how a list or tree problem's starter code can use `ListNode`
/// without defining it. Item order doesn't matter in a Rust module, so they
/// work from down here.
fn glue(function_name: &str, arity: usize, nodes: &[String]) -> String {
    let args: Vec<String> =
        (0..arity).map(|i| format!("::shikomi_prelude::arg(__args, {})?", i)).collect();
    format!(
        "\n\n// ---- generated by the shikomi judge harness (not part of your code) ----\n\
         {}\
         mod __shikomi_entry {{\n\
         \x20   #[unsafe(no_mangle)]\n\
         \x20   pub extern \"C\" fn main(_argc: i32, _argv: *const *const u8) -> i32 {{\n\
         \x20       ::shikomi_prelude::__entry(|__args| ::shikomi_prelude::ret(super::{}({})))\n\
         \x20   }}\n\
         }}\n",
        nodes.concat(),
        function_name,
        args.join(", ")
    )
}

/// Rust's reserved words, which a method can only be named with the raw-identifier
/// prefix (`r#type`). `self`, `super`, `crate` and `Self` can't be raw identifiers
/// at all, so `method_name` refuses them.
const KEYWORDS: &[&str] = &[
    "as", "break", "const", "continue", "else", "enum", "extern", "false", "fn", "for", "if",
    "impl", "in", "let", "loop", "match", "mod", "move", "mut", "pub", "ref", "return",
    "static", "struct", "trait", "true", "type", "unsafe", "use", "where", "while", "async",
    "await", "dyn", "abstract", "become", "box", "do", "final", "macro", "override", "priv",
    "typeof", "unsized", "virtual", "yield", "try", "gen",
];

/// The Rust method an operations case's op name calls: the name in snake_case,
/// so the shared cases' `getState` calls `get_state` (Python and JS keep the
/// name as written). An acronym stays one word (`toJSON` → `to_json`). `None`
/// if the op name isn't an identifier, or maps to a name Rust can't declare.
///
/// `new` is refused too: it's the constructor the glue calls, and one `impl`
/// can't also have a method of that name.
///
/// backend `app/sandbox.py` `rust_method_name` mirrors this, so seed validation
/// can refuse the same ops, and two ops that would map to the same method.
/// Both sides are tested against one table, `judge/tests/rust_method_names.json`.
fn method_name(op: &str) -> Option<String> {
    if !is_identifier(op) {
        return None;
    }
    let chars: Vec<char> = op.chars().collect();
    let mut out = String::new();
    for (i, &c) in chars.iter().enumerate() {
        if c.is_ascii_uppercase() {
            let prev = if i > 0 { Some(chars[i - 1]) } else { None };
            let next_lower = chars.get(i + 1).is_some_and(|n| n.is_ascii_lowercase());
            let boundary = match prev {
                Some(p) if p.is_ascii_lowercase() || p.is_ascii_digit() => true,
                Some(p) if p.is_ascii_uppercase() => next_lower,
                _ => false,
            };
            if boundary && !out.ends_with('_') {
                out.push('_');
            }
            out.push(c.to_ascii_lowercase());
        } else {
            out.push(c);
        }
    }
    match out.as_str() {
        "self" | "super" | "crate" | "Self" | "_" | "new" => None,
        kw if KEYWORDS.contains(&kw) => Some(format!("r#{}", kw)),
        _ => Some(out),
    }
}

/// Operations mode's counterpart of `glue`: construct `class_name` with its
/// `new`, then replay each op through a `match` on the op names the cases use
/// (`ops`: each case op name paired with the Rust method it calls). The prelude's
/// `ops::replay` does the work. The glue only supplies what needs names: the
/// constructor and one `call` arm per method, whose signature rustc then infers
/// arity and argument types from, as in function mode.
fn operations_glue(class_name: &str, ops: &[(String, String)], nodes: &[String]) -> String {
    let arms: String = ops
        .iter()
        .map(|(op, method)| {
            format!(
                "\x20           \"{op}\" => ::core::option::Option::Some(::shikomi_prelude::ops::call(\
                 __obj, super::{class_name}::{method}, \"{shown}\", __a)),\n",
                shown = method.trim_start_matches("r#"),
            )
        })
        .collect();
    format!(
        "\n\n// ---- generated by the shikomi judge harness (not part of your code) ----\n\
         {nodes}\
         mod __shikomi_entry {{\n\
         \x20   #[unsafe(no_mangle)]\n\
         \x20   pub extern \"C\" fn main(_argc: i32, _argv: *const *const u8) -> i32 {{\n\
         \x20       ::shikomi_prelude::__entry(|__case| ::shikomi_prelude::ops::replay(\n\
         \x20           __case,\n\
         \x20           super::{class_name}::new,\n\
         \x20           |__obj: &mut super::{class_name}, __op: &str, __a: &[::shikomi_prelude::Json]| match __op {{\n\
         {arms}\
         \x20           _ => ::core::option::Option::None,\n\
         \x20       }}))\n\
         \x20   }}\n\
         }}\n",
        nodes = nodes.concat(),
    )
}

/// The distinct op names the cases call (everything after each case's
/// constructor), in first-use order, each paired with its Rust method. Only
/// these get a dispatch arm, so a Run that judges only the samples compiles
/// only the methods the samples use; an op no case calls needs no method.
fn case_ops(cases: &[Json]) -> Result<Vec<(String, String)>, String> {
    let mut seen: Vec<(String, String)> = Vec::new();
    for tc in cases {
        for op in tc.get("input").as_arr().first().map_or(&[][..], |o| o.as_arr()).iter().skip(1) {
            let Json::Str(op) = op else { return Err(format!("op {} is not a string", op.dump())) };
            if seen.iter().any(|(o, _)| o == op) {
                continue;
            }
            let method = method_name(op).ok_or_else(|| format!("op name {:?} can't be a Rust method name", op))?;
            if let Some((other, _)) = seen.iter().find(|(_, m)| *m == method) {
                return Err(format!("ops {:?} and {:?} both map to the Rust method `{}`", other, op, method));
            }
            seen.push((op.clone(), method));
        }
    }
    Ok(seen)
}

// The node structs, in the conventional Rust shapes (DESIGN.md §13): the same
// fields, derives and `new` as LeetCode's definitions, so solutions port over.
// They're generated into the *submission's* crate, not defined in the prelude,
// because Rust's orphan rule only lets a crate implement a trait for its own
// types: a struct from the prelude would forbid `impl Ord for ListNode` (the
// usual way to put nodes in a `BinaryHeap`) and any helper `impl ListNode`.
// Each struct also implements the prelude's shape trait, the one-line
// accessors its codec (prelude.rs `nodes`) is written against. Paths are fully
// qualified, so a user's own imports can't change what these names mean.
//
// The three `Rc`-shared types (prelude.rs explains why every link is a strong
// `Rc`) have a hand-written `Debug` that shows each link as its target's `val`.
// A derived one would follow the links, and these structures are cyclic when
// they're *right* (every undirected edge is a 2-cycle), so a learner's
// `dbg!(&node)` would recurse until the stack overflowed.

/// One node struct the glue can generate: the codec name a problem declares,
/// the struct's source, and, for an `Rc`-shared type, its codec (the prelude's
/// `decode_*`/`encode_*` suffix) and how it writes `None`.
struct NodeStruct {
    name: &'static str,
    source: &'static str,
    rc_codec: Option<(&'static str, &'static str)>,
}

const EMPTY_ARRAY: &str = "::shikomi_prelude::Json::Arr(::std::vec::Vec::new())";

const NODE_STRUCTS: [NodeStruct; 5] = [
    NodeStruct { name: "ListNode", rc_codec: None, source: "\
#[derive(PartialEq, Eq, Clone, Debug)]
pub struct ListNode {
    pub val: i32,
    pub next: ::std::option::Option<::std::boxed::Box<ListNode>>,
}
impl ListNode {
    #[inline]
    pub fn new(val: i32) -> Self { ListNode { next: ::std::option::Option::None, val } }
}
impl ::shikomi_prelude::nodes::ListShape for ListNode {
    fn make(val: i32) -> Self { ListNode::new(val) }
    fn value(&self) -> i32 { self.val }
    fn next_node(&self) -> ::std::option::Option<&Self> { self.next.as_deref() }
    fn set_next_node(&mut self, next: ::std::option::Option<::std::boxed::Box<Self>>) { self.next = next; }
}
" },
    NodeStruct { name: "TreeNode", rc_codec: Some(("tree", EMPTY_ARRAY)), source: "\
#[derive(Debug, PartialEq, Eq)]
pub struct TreeNode {
    pub val: i32,
    pub left: ::std::option::Option<::std::rc::Rc<::std::cell::RefCell<TreeNode>>>,
    pub right: ::std::option::Option<::std::rc::Rc<::std::cell::RefCell<TreeNode>>>,
}
impl TreeNode {
    #[inline]
    pub fn new(val: i32) -> Self { TreeNode { val, left: ::std::option::Option::None, right: ::std::option::Option::None } }
}
impl ::shikomi_prelude::nodes::TreeShape for TreeNode {
    fn make(val: i32) -> Self { TreeNode::new(val) }
    fn value(&self) -> i32 { self.val }
    fn left_node(&self) -> ::std::option::Option<::std::rc::Rc<::std::cell::RefCell<Self>>> { self.left.clone() }
    fn right_node(&self) -> ::std::option::Option<::std::rc::Rc<::std::cell::RefCell<Self>>> { self.right.clone() }
    fn set_left_node(&mut self, c: ::std::option::Option<::std::rc::Rc<::std::cell::RefCell<Self>>>) { self.left = c; }
    fn set_right_node(&mut self, c: ::std::option::Option<::std::rc::Rc<::std::cell::RefCell<Self>>>) { self.right = c; }
}
" },
    NodeStruct { name: "CyclicListNode", rc_codec: Some(("cyclic", "::shikomi_prelude::Json::Null")), source: "\
pub struct CyclicListNode {
    pub val: i32,
    pub next: ::std::option::Option<::std::rc::Rc<::std::cell::RefCell<CyclicListNode>>>,
}
impl CyclicListNode {
    #[inline]
    pub fn new(val: i32) -> Self { CyclicListNode { val, next: ::std::option::Option::None } }
}
impl ::std::fmt::Debug for CyclicListNode {
    fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
        f.debug_struct(\"CyclicListNode\").field(\"val\", &self.val)
            .field(\"next\", &self.next.as_ref().and_then(|n| n.try_borrow().ok().map(|n| n.val))).finish()
    }
}
impl ::shikomi_prelude::nodes::CyclicShape for CyclicListNode {
    fn make(val: i32) -> Self { CyclicListNode::new(val) }
    fn set_next_node(&mut self, next: ::std::option::Option<::std::rc::Rc<::std::cell::RefCell<Self>>>) { self.next = next; }
}
" },
    NodeStruct { name: "RandomListNode", rc_codec: Some(("random", EMPTY_ARRAY)), source: "\
pub struct RandomListNode {
    pub val: i32,
    pub next: ::std::option::Option<::std::rc::Rc<::std::cell::RefCell<RandomListNode>>>,
    pub random: ::std::option::Option<::std::rc::Rc<::std::cell::RefCell<RandomListNode>>>,
}
impl RandomListNode {
    #[inline]
    pub fn new(val: i32) -> Self { RandomListNode { val, next: ::std::option::Option::None, random: ::std::option::Option::None } }
}
impl ::std::fmt::Debug for RandomListNode {
    fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
        f.debug_struct(\"RandomListNode\").field(\"val\", &self.val)
            .field(\"next\", &self.next.as_ref().and_then(|n| n.try_borrow().ok().map(|n| n.val)))
            .field(\"random\", &self.random.as_ref().and_then(|n| n.try_borrow().ok().map(|n| n.val))).finish()
    }
}
impl ::shikomi_prelude::nodes::RandomShape for RandomListNode {
    fn make(val: i32) -> Self { RandomListNode::new(val) }
    fn value(&self) -> i32 { self.val }
    fn next_node(&self) -> ::std::option::Option<::std::rc::Rc<::std::cell::RefCell<Self>>> { self.next.clone() }
    fn random_node(&self) -> ::std::option::Option<::std::rc::Rc<::std::cell::RefCell<Self>>> { self.random.clone() }
    fn set_next_node(&mut self, n: ::std::option::Option<::std::rc::Rc<::std::cell::RefCell<Self>>>) { self.next = n; }
    fn set_random_node(&mut self, n: ::std::option::Option<::std::rc::Rc<::std::cell::RefCell<Self>>>) { self.random = n; }
}
" },
    NodeStruct { name: "GraphNode", rc_codec: Some(("graph", EMPTY_ARRAY)), source: "\
pub struct GraphNode {
    pub val: i32,
    pub neighbors: ::std::vec::Vec<::std::rc::Rc<::std::cell::RefCell<GraphNode>>>,
}
impl GraphNode {
    #[inline]
    pub fn new(val: i32) -> Self { GraphNode { val, neighbors: ::std::vec::Vec::new() } }
}
impl ::std::fmt::Debug for GraphNode {
    fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
        let neighbors: ::std::vec::Vec<i32> = self.neighbors.iter().filter_map(|n| n.try_borrow().ok().map(|n| n.val)).collect();
        f.debug_struct(\"GraphNode\").field(\"val\", &self.val).field(\"neighbors\", &neighbors).finish()
    }
}
impl ::shikomi_prelude::nodes::GraphShape for GraphNode {
    fn make(val: i32) -> Self { GraphNode::new(val) }
    fn value(&self) -> i32 { self.val }
    fn neighbor_nodes(&self) -> &[::std::rc::Rc<::std::cell::RefCell<Self>>] { &self.neighbors }
    fn push_neighbor(&mut self, n: ::std::rc::Rc<::std::cell::RefCell<Self>>) { self.neighbors.push(n); }
}
" },
];

/// The `RcNode` impl that routes an `Rc`-shared struct to its prelude codec
/// (one blanket `FromJson`/`ToJson` impl for `Rc<RefCell<T>>` can exist, so each
/// type names its codec here).
fn rc_node_impl(name: &str, codec: &str, none: &str) -> String {
    format!(
        "impl ::shikomi_prelude::nodes::RcNode for {name} {{\n\
         \x20   fn decode(j: &::shikomi_prelude::Json) -> ::std::result::Result<\
         ::std::option::Option<::std::rc::Rc<::std::cell::RefCell<Self>>>, ::std::string::String> \
         {{ ::shikomi_prelude::nodes::decode_{codec}(j) }}\n\
         \x20   fn encode(n: &::std::rc::Rc<::std::cell::RefCell<Self>>) -> ::std::result::Result<\
         ::shikomi_prelude::Json, ::std::string::String> \
         {{ ::shikomi_prelude::nodes::encode_{codec}(n) }}\n\
         \x20   fn encode_none() -> ::shikomi_prelude::Json {{ {none} }}\n\
         }}\n"
    )
}

/// The node structs a problem needs, from the codec names its Rust variant
/// declares in `params[].type` and `return_type` (`"ListNode"`,
/// `"List[TreeNode]"`, ...). Only those are generated: a problem that declares
/// none leaves the names free, so a trie problem's own `struct TreeNode` can't
/// collide with ours. Also re-exports the prelude's `IntIter` when a param is
/// `"Iterator"` (decode-only, no struct to generate), so the user's
/// `fn new(nums: IntIter)` can name it; a `use` at crate root is visible to
/// their earlier code, since item order doesn't matter.
fn node_structs(payload: &Json) -> Vec<String> {
    let mut declared: Vec<&str> = payload.get("params").as_arr().iter().filter_map(|p| match p.get("type") {
        Json::Str(t) => Some(t.as_str()),
        _ => None,
    }).collect();
    if let Json::Str(t) = payload.get("return_type") {
        declared.push(t);
    }
    let uses = |name: &str| declared.iter().any(|t| *t == name || *t == format!("List[{}]", name));
    let mut out: Vec<String> = NODE_STRUCTS
        .iter()
        .filter(|n| uses(n.name))
        .map(|n| match n.rc_codec {
            Some((codec, none)) => format!("{}{}", n.source, rc_node_impl(n.name, codec, none)),
            None => n.source.to_string(),
        })
        .collect();
    if declared.iter().any(|t| *t == "Iterator") {
        out.push("use ::shikomi_prelude::IntIter;\n".to_string());
    }
    out
}

/// The whole `solution.rs`: `#![no_main]` on the user's first line (see `glue`),
/// their code, then the glue (`glue` or `operations_glue`).
fn source(user_code: &str, glue: &str) -> String {
    format!("#![no_main] {}{}", user_code, glue)
}

/// Compile `solution.rs` in WORK. `Err` carries the message for the single
/// runtime_error row; `hint` adds a kind-specific hint to a compile error's
/// diagnostics (`ops_hint` in operations mode).
fn compile(timeout: Duration, hint: impl Fn(&str) -> String) -> Result<(), String> {
    let stderr_path = Path::new(WORK).join("rustc.stderr");
    let mut cmd = Command::new(RUSTC);
    // Own process group, so a timeout kills the linker rustc spawned along with
    // rustc itself (`end_case` below), not just rustc's pid.
    unsafe {
        cmd.pre_exec(|| {
            setpgid(0, 0);
            Ok(())
        });
    }
    let child = cmd
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
        .stderr(fs::File::create(&stderr_path)
            .unwrap_or_else(|e| judge_fault(format!("could not create rustc.stderr: {}", e))))
        .spawn()
        .unwrap_or_else(|e| judge_fault(format!("could not start rustc: {}", e)));
    let pid = child.id() as i32;
    let exit = supervise(child, Instant::now(), timeout);
    end_case(pid);
    let diag = fs::read_to_string(&stderr_path).unwrap_or_default();
    match exit {
        Exit::TimedOut => Err(format!("Compilation timed out after {}s", timeout.as_secs())),
        Exit::Finished(status, _) if status.success() => Ok(()),
        // The memory cgroup's OOM killer picks the biggest process, which is often
        // the linker rustc spawned rather than rustc. rustc then exits normally,
        // reporting "linking with `cc` failed: ... signal: 9 (SIGKILL)".
        Exit::Finished(status, _)
            if status.signal() == Some(SIGKILL) || diag.contains("signal: 9 (SIGKILL)") =>
        {
            Err("Compilation ran out of memory".into())
        }
        Exit::Finished(_, _) => Err(format!("Compile error:\n{}{}{}", diag.trim_end(), node_hint(&diag), hint(&diag))),
    }
}

/// A hint for the likeliest node-problem mistake: pasting the starter's
/// commented-out node struct (or its `impl` with `new`) back in. It then
/// collides with the one the glue generates (`node_structs`), and rustc's error
/// points half at glue the user never wrote. Matched on rustc's own wording for
/// that collision, so it can't fire for anything else (a node name that only
/// appears in a comment, or a wrong signature).
///
/// A duplicate `new` is only blamed on a node struct when rustc's excerpt shows
/// the glue's own `new` for one: in operations mode the likelier cause is the
/// user's class defining `new` twice, which rustc's own message already explains.
fn node_hint(diag: &str) -> &'static str {
    let redefined =
        NODE_STRUCTS.iter().any(|n| diag.contains(&format!("the name `{}` is defined multiple times", n.name)));
    let node_new = diag.contains("duplicate definitions with name `new`")
        && NODE_STRUCTS.iter().any(|n| diag.contains(&format!("pub fn new(val: i32) -> Self {{ {} {{", n.name)));
    if redefined || node_new {
        "\n\nHint: the judge already defines the node struct (the one described in the \
         starter's comment). Remove your own definition and its `new`. You can still add \
         other methods or trait impls (`impl Ord for ListNode`) to the judge's struct."
    } else {
        ""
    }
}

/// Hints for the operations-mode mistakes whose rustc error points into the
/// glue rather than at the user's code: a method named as the cases spell it
/// (`getState`) rather than as the judge calls it (`get_state`), a missing `new`,
/// a signature `ops::Method`/`ops::Constructor` can't call, and an `impl Iterator` constructor
/// parameter where the judge's `IntIter` is needed. Each is matched
/// on rustc's own wording, like `node_hint`.
fn ops_hint(diag: &str, class_name: &str, ops: &[(String, String)]) -> String {
    let mut hints = Vec::new();
    // rustc names the item as declared (`r#type` for a keyword) and says "struct" or
    // "enum" depending on how the class is declared, so try each spelling.
    let missing = |name: &str| {
        let bare = name.trim_start_matches("r#");
        [bare.to_string(), format!("r#{}", bare)].iter().any(|n| {
            ["struct", "enum"].iter().any(|k| diag.contains(&format!("named `{}` found for {} `{}`", n, k, class_name)))
        })
    };
    if missing("new") {
        hints.push(format!(
            "the judge builds the object with `{}::new(...)`, called with the test case's \
             first argument list. Give `impl {}` a `fn new(...) -> Self`.",
            class_name, class_name
        ));
    }
    for (op, method) in ops {
        let shown = method.trim_start_matches("r#");
        if op != shown && missing(method) {
            hints.push(format!(
                "the test cases call `{}`, which the judge calls as the Rust method `{}` (op \
                 names are converted to snake_case).",
                op, shown
            ));
        }
    }
    if diag.contains("ops::Method") || diag.contains(": Method<") {
        hints.push(
            "the judge calls each method with arguments decoded from the test case, and encodes \
             what it returns. A method must take `&self` or `&mut self` and at most six owned \
             parameters (`String`, not `&str`), and return an owned value (`Vec<i32>`, not \
             `&Vec<i32>`)."
                .to_string(),
        );
    }
    if diag.contains("ops::Constructor") || diag.contains(": Constructor<") {
        hints.push(format!(
            "`{}::new` must return `Self` and take at most six owned parameters.",
            class_name
        ));
    }
    // `new(nums: impl Iterator<Item = i32>)` leaves the argument's type for rustc to
    // infer from a bound, and it can't: the error names `Iterator` and `Class::new`.
    if diag.contains("Iterator") && diag.contains(&format!("`{}::new`", class_name)) {
        hints.push(
            "a problem's `Iterator` argument is the judge's `IntIter` (an `Iterator<Item = i32>`): \
             declare the parameter as `nums: IntIter`, not `impl Iterator` or `Box<dyn Iterator>`."
                .to_string(),
        );
    }
    hints.iter().map(|h| format!("\n\nHint: {}", h)).collect()
}

// --- run one case ---------------------------------------------------------------

fn run_case(tc: &Json, time_limit: Duration, memory_bytes: Option<u64>, comparison: &Json) -> CaseResult {
    let id = tc.get("id").clone();
    let work = Path::new(WORK);
    let result_path = work.join("case.result");
    let _ = fs::remove_file(&result_path);

    let mut cmd = Command::new(work.join("solution"));
    cmd.env_clear()
        .envs(CASE_ENV)
        .env(shikomi_prelude::RESULT_ENV, &result_path)
        .stdin(Stdio::piped())
        // Pipes drained by threads (`drain`), keeping only the first few KB. Files in
        // /tmp would share the 32MB tmpfs with the result file, so a chatty but correct
        // solution could fill it and fail on ENOSPC. An undrained pipe would instead
        // block the child and read as a false time_limit_exceeded.
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
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
        // EAGAIN: the pids limit is exhausted, by processes this submission started
        // that escaped the group kill. That's the submission's doing; anything else
        // (noexec /tmp, a missing binary) is the judge's.
        Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
            return CaseResult::error_row(id, "could not start the program: too many processes".into());
        }
        Err(e) => judge_fault(format!("could not start the program: {}", e)),
    };
    let stdout_rx = drain(child.stdout.take(), TRUNC + 1);
    let stderr_rx = drain(child.stderr.take(), STDERR_KEEP);
    // Feed the input from a thread, so the deadline below is already running
    // while we write. A write larger than the pipe buffer (64KB) blocks until
    // the child reads, and a child that never reads (e.g. one stalled in a static
    // constructor before `main`) would otherwise block the harness with no
    // deadline at all. Killing the child breaks the pipe, which ends the write
    // with EPIPE (Rust ignores SIGPIPE), so the thread always finishes.
    let input = tc.get("input").dump();
    let writer = child.stdin.take().map(|mut stdin| {
        std::thread::spawn(move || {
            let _ = stdin.write_all(input.as_bytes());
        }) // dropping stdin at the end closes it, so the child's read sees EOF
    });
    let pid = child.id() as i32;
    let exit = supervise(child, start, time_limit);
    end_case(pid);
    if let Some(w) = writer {
        let _ = w.join();
    }
    let stdout = stdout_rx.recv_timeout(DRAIN_GRACE).unwrap_or_default();

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

    // The per-case program's result file:
    // {"ok": v} | {"panic": msg} | {"decode": msg} | {"malformed": msg}.
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
            if let Json::Str(msg) = r.get("malformed") {
                return fail("runtime_error", Some(msg.clone()));
            }
            if let Json::Str(msg) = r.get("decode") {
                return fail("runtime_error", Some(format!("could not decode the test case: {}", msg)));
            }
        }
        None => {}
    }

    // No result: the process died without returning. Tell the user how.
    let stderr = stderr_rx.recv_timeout(DRAIN_GRACE).unwrap_or_default();
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
        // Safe Rust can't fault on memory any other way, so a SIGSEGV is the stack
        // guard page: recursion ran past RLIMIT_STACK. (The entry point is a C
        // `main`, so std's own "has overflowed its stack" handler isn't installed.)
        (_, Some(SIGSEGV)) => fail("runtime_error", Some("stack overflow (recursion too deep?)".into())),
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
    let nodes = node_structs(payload);
    let (generated, operations) = match kind {
        "function" => {
            let function_name = match payload.get("function_name") {
                Json::Str(f) if is_identifier(f) => f.clone(),
                // The name is pasted into generated source, so anything but a plain
                // identifier is refused rather than compiled.
                other => judge_fault(format!("invalid function_name {}", other.dump())),
            };
            // Arity comes from the problem's declared params (the types are display-only;
            // see prelude.rs). With none declared, fall back to the first case's argument
            // count.
            let arity = match payload.get("params").as_arr().len() {
                0 => cases.first().map_or(0, |c| c.get("input").as_arr().len()),
                n => n,
            };
            (glue(&function_name, arity, &nodes), None)
        }
        "operations" => {
            let class_name = match payload.get("class_name") {
                Json::Str(c) if is_identifier(c) => c.clone(),
                other => judge_fault(format!("invalid class_name {}", other.dump())),
            };
            // Op names are pasted into generated source too. ProblemIn refuses a bad
            // one at seed time; this is the backstop.
            let ops = case_ops(cases).unwrap_or_else(|e| judge_fault(e));
            (operations_glue(&class_name, &ops, &nodes), Some((class_name, ops)))
        }
        // Scope (DESIGN.md §13): no sql, which is MariaDB's. ProblemIn rejects anything
        // else for Rust at seed time; this is the backstop.
        _ => {
            return vec![CaseResult::error_row(first_id, format!("kind '{}' is not supported by the Rust harness", kind))];
        }
    };
    let user_code = match payload.get("user_code") {
        Json::Str(s) => s.as_str(),
        _ => "",
    };

    if let Err(e) = fs::create_dir_all(WORK)
        .and_then(|_| fs::write(Path::new(WORK).join("solution.rs"), source(user_code, &generated)))
    {
        judge_fault(format!("could not write the source: {}", e));
    }
    let compile_timeout = Duration::from_secs(match payload.get("compile_timeout_s") {
        Json::Int(n) if *n > 0 => *n as u64,
        _ => DEFAULT_COMPILE_TIMEOUT_S,
    });
    let hint = |diag: &str| match &operations {
        Some((class_name, ops)) => ops_hint(diag, class_name, ops),
        None => String::new(),
    };
    if let Err(e) = compile(compile_timeout, hint) {
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
    // Non-dumpable: the kernel then refuses same-uid, non-root access to this
    // process's /proc/<pid>/environ, /mem and ptrace. A case's process runs as the
    // same uid 1000 as this one, which holds the whole payload. Measured without
    // it: /proc/1/environ was readable, while /proc/1/mem was already refused
    // under Docker's defaults. This makes the refusal ours rather than the
    // runtime's (defense in depth).
    unsafe { prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) };
    let raw = match std::env::var("JUDGE_PAYLOAD_FILE") {
        Ok(path) => {
            // Read the payload, then immediately unlink it. On k8s it's a file at
            // a fixed path in a volume the case's process shares (same uid), so a
            // submission could otherwise hardcode that path and read every hidden
            // case's `expected` back out of it — not being in the case's env
            // (CASE_ENV) doesn't hide the path. The parent has the payload in
            // memory by now and the per-case children get their input over stdin,
            // never this file, so deleting it costs nothing. This mirrors what
            // harness.py/js do on their k8s path (ADR-0006). On the Docker path
            // the payload arrives on stdin, so there's no file to remove.
            let contents = match fs::read_to_string(&path) {
                Ok(c) => c,
                Err(e) => {
                    // A judge-side I/O fault, not a malformed payload — say so.
                    eprintln!("harness: could not read payload file: {}", e);
                    std::process::exit(3);
                }
            };
            // Fail closed: if we read a real payload but can't delete it, we can't
            // guarantee `expected` is out of a case's reach, so refuse the run
            // rather than grade every case with it still readable at the fixed
            // path. The k8s emptyDir is world-writable and we own the file, so this
            // never fires in practice; it guards against a future misconfiguration.
            if let Err(e) = fs::remove_file(&path) {
                eprintln!("harness: could not remove payload file: {}", e);
                std::process::exit(3);
            }
            Ok(contents)
        }
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
