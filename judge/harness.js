#!/usr/bin/env node
/**
 * Judge harness (JavaScript) — runs untrusted user code against test cases
 * inside the sandbox. Protocol mirrors judge/harness.py exactly (DESIGN.md
 * §5.3): stdin (or JUDGE_PAYLOAD_FILE) carries the payload JSON, stdout
 * carries exactly one result JSON document `{"results": [...]}` with the same
 * per-case shape (test_case_id/status/runtime_ms/output/stdout/error) — the
 * aggregator downstream doesn't know or care which language judged a
 * submission.
 *
 * Scope (DESIGN.md §13): function-mode only. No `kind: "operations"`
 * (class-replay) and no ListNode/TreeNode codecs. `kind` values other than
 * "function" are rejected as a single runtime_error rather than crashing.
 * Note the cost: a JS problem that is really about a class has to hand-roll a
 * `*Trace` driver function that replays calls through function mode. Adding
 * operations support here would retire those drivers.
 *
 * Node core modules only (`require` is bound into the sandboxed context, but
 * nothing beyond the runtime is installed — judge/Dockerfile.js) — the
 * container is the actual security boundary (no network, read-only FS,
 * dropped capabilities, non-root), not the JS runtime, matching harness.py's
 * exec() giving Python code full stdlib access under the same container model.
 *
 * Async support: a returned Promise is awaited, raced against the case's
 * remaining time budget using a real timer (`setTimeout`/`clearTimeout`/
 * `setInterval`/`clearInterval` are bound into the sandbox for exactly this —
 * see `awaitIfThenable`). Real host timers run callbacks outside of any
 * try/catch this file controls, so two more safeguards go with them: every
 * timer a submission schedules only actually runs its callback if the test
 * case that scheduled it is still the one being graded (`scopedSetTimeout`/
 * `scopedSetInterval`), and a process-level `uncaughtException`/
 * `unhandledRejection` handler fails just the in-flight case rather than
 * letting Node's default (crash the whole process, losing every case's
 * results) happen. `main()` waits for `process.stdout.write`'s own callback —
 * not a fixed delay — before calling `process.exit()`, so a large result set
 * can't be truncated by exiting before the write actually reaches the OS,
 * while a leaked `setInterval`/unresolved timer still can't hold the process
 * (and the sandbox slot) open indefinitely.
 *
 * None of this catches a submission that starves the event loop with an
 * unyielding microtask chain (e.g. recursive `Promise.resolve().then(loop)`)
 * — macrotask timers, ours included, never get a turn — so the worker's outer
 * wall-clock kill (docker_runner's `asyncio.wait_for` / k8s's
 * `activeDeadlineSeconds`) stays the real backstop for that one pathological
 * case.
 */
'use strict';

const vm = require('vm');
const fs = require('fs');

const TRUNC = 4096; // per-field cap for output/stdout/error (DESIGN.md §5.3)
const USER_FILENAME = '<user_code>'; // vm.Script filename; stack frames naming this are the user's code

function truncate(text) {
  if (text === null || text === undefined) return null;
  if (text.length <= TRUNC) return text;
  return text.slice(0, TRUNC) + '…(truncated)';
}

/** Render the actual return value. Compact JSON — JSON.stringify has no
 * whitespace by default, matching harness.py's `separators=(",", ":")` so
 * Output/Expected read identically regardless of which language judged. */
function formatOutput(value) {
  try {
    const s = JSON.stringify(value);
    return s === undefined ? String(value) : s; // undefined isn't JSON-representable
  } catch (e) {
    try {
      return String(value);
    } catch (e2) {
      return '<unrepresentable>';
    }
  }
}

function consoleArgToString(v) {
  if (typeof v === 'string') return v;
  try {
    return JSON.stringify(v);
  } catch (e) {
    return String(v);
  }
}

// --- comparison modes (mirrors harness.py's compare(), DESIGN.md §5.4) -----

function deepEqual(a, b) {
  if (a === b) return true;
  if (typeof a === 'number' && typeof b === 'number') {
    return Number.isNaN(a) && Number.isNaN(b);
  }
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) {
      if (!deepEqual(a[i], b[i])) return false;
    }
    return true;
  }
  if (a && b && typeof a === 'object' && typeof b === 'object') {
    const ak = Object.keys(a).sort();
    const bk = Object.keys(b).sort();
    if (ak.length !== bk.length) return false;
    for (let i = 0; i < ak.length; i++) {
      if (ak[i] !== bk[i] || !deepEqual(a[ak[i]], b[bk[i]])) return false;
    }
    return true;
  }
  return false;
}

function floatEqual(a, b, eps) {
  if (typeof a === 'number' && typeof b === 'number') {
    if (Number.isNaN(a) && Number.isNaN(b)) return true;
    return Math.abs(a - b) <= eps;
  }
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((x, i) => floatEqual(x, b[i], eps));
  }
  return deepEqual(a, b);
}

function multisetEqual(a, b) {
  if (!Array.isArray(a) || !Array.isArray(b)) return deepEqual(a, b);
  if (a.length !== b.length) return false;
  const remaining = b.slice();
  for (const item of a) {
    const idx = remaining.findIndex((candidate) => deepEqual(candidate, item));
    if (idx === -1) return false;
    remaining.splice(idx, 1);
  }
  return true;
}

/** Decide whether `actual` matches `expected` under the problem's compare
 * mode — same four modes as harness.py's `compare()`, same fallback
 * ("exact"/unknown -> deep equality) for parity across languages. */
function compare(actual, expected, comparison) {
  const mode = (comparison || {}).mode || 'exact';
  if (mode === 'unordered') return multisetEqual(actual, expected);
  if (mode === 'float_tolerance') {
    const eps = (comparison || {}).epsilon;
    return floatEqual(actual, expected, eps === undefined ? 1e-6 : eps);
  }
  if (mode === 'any_of') {
    const candidates = Array.isArray(expected) ? expected : [expected];
    return candidates.some((c) => deepEqual(actual, c));
  }
  return deepEqual(actual, expected);
}

// --- error formatting --------------------------------------------------------

function isTimeoutError(err) {
  // Node's vm timeout has no dedicated error class/code — it throws a plain
  // Error with this message (checked against the Node 20 vm implementation).
  return !!err && typeof err.message === 'string' && /script execution timed out/i.test(err.message);
}

/** Format an error showing only frames from the user's code, so a runtime
 * error shows the user *their* stack — not harness internals — mirroring
 * harness.py's `_format_user_traceback`. Best-effort: a syntax error caught
 * before any frame runs may have no `<user_code>` frame to filter to, in
 * which case the bare message is reported. */
function formatUserError(err) {
  const name = (err && err.name) || 'Error';
  const message = (err && err.message) !== undefined ? err.message : String(err);
  const header = `${name}: ${message}`;
  if (err && typeof err.stack === 'string') {
    const userLines = err.stack.split('\n').filter((l) => l.includes(USER_FILENAME));
    if (userLines.length) return [header, ...userLines].join('\n');
  }
  return header;
}

function msSince(startNs) {
  const ms = Number(process.hrtime.bigint() - startNs) / 1e6;
  return Math.round(ms * 1000) / 1000; // 3 decimals, matching harness.py's round(...,3)
}

// A per-case time_limit_exceeded builder, shared by the synchronous vm timeout
// path and the async awaitIfThenable path below so the two don't drift out of
// sync with each other on the result shape. (A failing case's runtime_error is
// built inline where it's raised, in the child; see finalizeCase.)
function timeoutResult(testCaseId, timeLimitMs, stdout) {
  return {
    test_case_id: testCaseId,
    status: 'time_limit_exceeded',
    runtime_ms: timeLimitMs,
    output: null,
    stdout: truncate(stdout || ''),
    error: null,
  };
}

// --- async result handling ---------------------------------------------------

// Distinguishes "the race's timer won" from any real value a submission could
// legitimately resolve with (unlike, say, `undefined`, which a Promise can
// resolve to on purpose).
const ASYNC_TIMEOUT = Symbol('async_timeout');

// Wraps an error that reached us via a rejected promise *or* one of the two
// process-level handlers below, so run() can treat every async failure mode
// identically to a thrown synchronous error. A class, not a tagged plain
// object: user code runs in a separate vm realm and never sees this
// constructor, so there's no way a legitimate resolved value could ever
// satisfy `instanceof AsyncError` by accident.
class AsyncError {
  constructor(err) {
    this.err = err;
  }
}

// setTimeout/setInterval bound into the sandbox (below) are the *real* host
// timers — a callback they fire runs outside of any try/catch we control, so
// a synchronous throw inside one becomes a process-wide 'uncaughtException'
// (not a rejected promise) and an abandoned rejection inside one becomes
// 'unhandledRejection'. Node's default for both is to crash the process,
// which would destroy every other test case's already-computed results, not
// just the offending one. `settleCurrentCase` — set only while a case's
// awaitIfThenable is in flight, cleared the moment it settles — lets these
// handlers fail *that* case cleanly instead. A stray callback from an
// already-finished case's leaked timer finds this null and is dropped: the
// scoped-timer wrappers below are the primary defense against that (they stop
// a stale callback from running user code at all), this is the last-resort
// backstop for the case where one still gets through.
let settleCurrentCase = null;

process.on('uncaughtException', (err) => {
  if (settleCurrentCase) settleCurrentCase(new AsyncError(err));
});
process.on('unhandledRejection', (err) => {
  if (settleCurrentCase) settleCurrentCase(new AsyncError(err));
});

// Every real timer a submission schedules is tagged with the test case active
// at scheduling time; a callback only actually runs if that case is *still*
// the active one when it fires. This is what stops a leaked setTimeout/
// setInterval from one case from mutating shared state (the current case's
// stdout buffer, `settleCurrentCase`) once grading has moved past it — a
// no-op is the correct outcome for a stale callback, not a crash or
// contamination. `activeCaseToken` changes every iteration of the loop in
// run() (a fresh value per case, or null once the loop is done), so an exact
// `===` is enough; the wrapped functions still return the *real* timer handle
// so the sandbox's own clearTimeout/clearInterval (bound in unwrapped) work
// on them unchanged.
let activeCaseToken = null;

function scopedSetTimeout(fn, delay, ...args) {
  const token = activeCaseToken;
  return setTimeout((...cbArgs) => {
    if (token !== activeCaseToken) return;
    fn(...cbArgs);
  }, delay, ...args);
}

function scopedSetInterval(fn, delay, ...args) {
  const token = activeCaseToken;
  return setInterval((...cbArgs) => {
    if (token !== activeCaseToken) return;
    fn(...cbArgs);
  }, delay, ...args);
}

/** Await `value` if it's thenable (duck-typed — a Promise from the vm sandbox's
 * own realm has a different constructor than the host's, so `instanceof
 * Promise` would miss it), racing it against `remainingMs` on a real timer.
 * Passing a non-thenable back unchanged means every caller can treat this as
 * a no-op for ordinary synchronous returns.
 *
 * Always resolves, never rejects: a rejected `value`, a negative/zero
 * `remainingMs`, and an escape caught by the process-level handlers above are
 * all folded into the same three possible outcomes (the real value,
 * `ASYNC_TIMEOUT`, or an `AsyncError`) so callers need exactly one code path,
 * not a try/catch plus a sentinel check. Critically, `value` always gets a
 * `.then()` attached — even when `remainingMs <= 0` and we're not going to
 * wait for it — because *not* doing so left `value`'s eventual rejection
 * completely unobserved, which is itself an `unhandledRejection` waiting to
 * happen. */
function awaitIfThenable(value, remainingMs) {
  if (!value || typeof value.then !== 'function') return value;
  return new Promise((resolve) => {
    let done = false;
    const finish = (result) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      settleCurrentCase = null;
      resolve(result);
    };
    const timer = setTimeout(() => finish(ASYNC_TIMEOUT), Math.max(remainingMs, 0));
    settleCurrentCase = finish;
    Promise.resolve(value).then(
      (v) => finish(v),
      (err) => finish(new AsyncError(err)),
    );
  });
}

// --- execution: a trusted parent and an untrusted child ----------------------
// Like the Python harness (DESIGN.md §5.3), the submission runs in a separate
// **child** process, never here. `vm` is not a security boundary — user code
// can reach the host realm through it — so the child, not the context, is what
// isolates the submission. The parent holds every case's `expected`, decides
// pass/fail with `compare()`, and alone writes the `{"results": [...]}` report.
// The child receives only the submission and each case's *input* (fd 0), returns
// the value it produced on a private pipe (fd 3), and has its stdout/stderr
// pointed at /dev/null. So a fabricated return value can't pass without knowing
// an `expected` the child never sees, and the child can't reach the report
// stream. The parent keeps one child for the run and respawns it only after a
// timeout or crash.

const CHILD_FLAG = '--child';
const RESULT_FD = 3;         // the private child->parent pipe (stdio index 3)
const SETUP_GRACE_MS = 10000; // ceiling for the one-time compile in the child
const KILL_GRACE_MS = 500;    // extra wall time past the case limit before the parent kills

// --- the child: runs the submission, never sees `expected` -------------------

function childSetup(setup) {
  // Build the vm context and compile the submission once. Returns {ctx} or
  // {error} where error is the message the parent turns into a single
  // runtime_error, exactly as the pre-split harness did at load time.
  const ctx = { buffer: [], functionName: setup.function_name, timeLimitMs: Math.max(parseInt(setup.time_limit_ms || 2000, 10), 1) };
  const sandbox = {
    console: { log: (...args) => ctx.buffer.push(args.map(consoleArgToString).join(' ')) },
    require,
    setTimeout: scopedSetTimeout,
    clearTimeout,
    setInterval: scopedSetInterval,
    clearInterval,
  };
  vm.createContext(sandbox);
  ctx.sandbox = sandbox;
  try {
    new vm.Script(setup.user_code, { filename: USER_FILENAME }).runInContext(sandbox);
  } catch (err) {
    return { error: formatUserError(err) };
  }
  let exists;
  try {
    exists = vm.runInContext(`typeof ${ctx.functionName} === 'function'`, sandbox);
  } catch (err) {
    exists = false;
  }
  if (!exists) return { error: `Function '${ctx.functionName}' not found` };
  return { ctx };
}

async function executeCase(ctx, input) {
  // Run one case in the child; return its outcome without deciding pass/fail.
  // On the ok path it returns the raw `actual` for the parent to compare.
  const { sandbox, functionName, timeLimitMs } = ctx;
  ctx.buffer = [];
  sandbox.console = { log: (...args) => ctx.buffer.push(args.map(consoleArgToString).join(' ')) };
  sandbox.__args = JSON.parse(JSON.stringify(input || []));
  activeCaseToken = (activeCaseToken || 0) + 1;
  const start = process.hrtime.bigint();
  let actual;
  try {
    vm.runInContext(`__result = ${functionName}.apply(null, __args);`, sandbox,
      { timeout: timeLimitMs, filename: 'harness_call.js' });
    actual = sandbox.__result;
  } catch (err) {
    activeCaseToken = null;
    if (isTimeoutError(err)) return { status: 'time_limit_exceeded', stdout: truncate(ctx.buffer.join('\n')) };
    return { status: 'runtime_error', runtime_ms: msSince(start), stdout: truncate(ctx.buffer.join('\n')), error: formatUserError(err) };
  }
  actual = await awaitIfThenable(actual, timeLimitMs - msSince(start));
  activeCaseToken = null;
  if (actual === ASYNC_TIMEOUT) return { status: 'time_limit_exceeded', stdout: truncate(ctx.buffer.join('\n')) };
  if (actual instanceof AsyncError) {
    return { status: 'runtime_error', runtime_ms: msSince(start), stdout: truncate(ctx.buffer.join('\n')), error: formatUserError(actual.err) };
  }
  return { status: 'ok', actual, runtime_ms: msSince(start), stdout: truncate(ctx.buffer.join('\n')) };
}

async function childMain() {
  // fd 1/2 are already /dev/null (the parent spawns us with stdio 'ignore'), so
  // a submission writing to a raw descriptor reaches nothing the parent reads.
  const emit = (message) => { fs.writeSync(RESULT_FD, JSON.stringify(message) + '\n'); };
  const lines = readLines(process.stdin);

  const first = await lines.next();
  if (first.done) return;
  const setup = childSetup(JSON.parse(first.value));
  if (setup.error !== undefined) { emit({ setup: 'user_error', error: setup.error }); return; }
  emit({ setup: 'ok' });

  for await (const line of lines) {
    if (!line) continue;
    emit(await executeCase(setup.ctx, JSON.parse(line).input));
  }
}

// An async line iterator over a stream (the child's control channel). One JSON
// object per line, which is how the parent frames every message.
async function* readLines(stream) {
  let buf = '';
  for await (const chunk of stream) {
    buf += chunk;
    let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      yield buf.slice(0, i);
      buf = buf.slice(i + 1);
    }
  }
  if (buf.length) yield buf;
}

// --- the parent: orchestrates the child and writes the report ----------------

const { spawn } = require('child_process');

class ChildProc {
  constructor(proc) {
    this.proc = proc;
    this.out = proc.stdio[RESULT_FD];
    this.lines = [];
    this.buf = '';
    this.waiter = null;
    this.dead = false;
    this.out.on('data', (d) => {
      this.buf += d;
      let i;
      while ((i = this.buf.indexOf('\n')) >= 0) {
        this.lines.push(this.buf.slice(0, i));
        this.buf = this.buf.slice(i + 1);
      }
      this._wake();
    });
    const onDead = () => { this.dead = true; this._wake(); };
    proc.on('exit', onDead);
    this.out.on('close', onDead);
  }

  _wake() { if (this.waiter) { const w = this.waiter; this.waiter = null; w(); } }

  send(message) { this.proc.stdin.write(JSON.stringify(message) + '\n'); }

  // Read one framed message. Resolves to {kind:'msg', msg} | {kind:'hang'}
  // (nothing within deadlineMs) | {kind:'crash'} (child died first).
  async read(deadlineMs) {
    const start = Date.now();
    for (;;) {
      if (this.lines.length) {
        const line = this.lines.shift();
        // The child's `require` is the real one, so a submission can write raw
        // bytes to fd 3 itself. A malformed frame must not throw out of here and
        // abort the whole run (losing every case as a judge_error); treat it as
        // a dead, desynced child so the case fails and the parent respawns. It
        // still can't forge a pass — the parent alone holds `expected`.
        let msg;
        try { msg = JSON.parse(line); }
        catch (e) { return { kind: 'crash' }; }
        return { kind: 'msg', msg };
      }
      if (this.dead) return { kind: 'crash' };
      const remaining = deadlineMs - (Date.now() - start);
      if (remaining <= 0) return { kind: 'hang' };
      await new Promise((resolve) => {
        this.waiter = resolve;
        const t = setTimeout(() => { if (this.waiter === resolve) { this.waiter = null; resolve(); } }, remaining);
        if (t.unref) t.unref();
      });
    }
  }

  kill() {
    // The child is its own process-group leader (spawned `detached`), so signal
    // the whole group: this also reaps any grandchildren the submission spawned
    // (its `require` reaches child_process), mirroring the Python harness's
    // os.killpg. Fall back to the direct child if the group send fails — e.g.
    // it already exited, so the group no longer exists.
    const pid = this.proc.pid;
    try {
      if (pid !== undefined) process.kill(-pid, 'SIGKILL');
      else this.proc.kill('SIGKILL');
    } catch (e) {
      try { this.proc.kill('SIGKILL'); } catch (e2) { /* already gone */ }
    }
  }
}

function spawnChildProc() {
  // stdio: control on stdin, stdout+stderr to /dev/null (so raw writes vanish),
  // and a fourth 'pipe' → fd 3 in the child, the private result channel.
  const env = {};
  for (const [k, v] of Object.entries(process.env)) if (!k.startsWith('JUDGE_')) env[k] = v;
  // `detached: true` puts the child in its own process group (setsid) so a kill
  // can reap the whole group, not just the direct child; see ChildProc.kill.
  // We keep it referenced (no unref) — the parent manages its lifecycle.
  const proc = spawn(process.execPath, [__filename, CHILD_FLAG],
    { stdio: ['pipe', 'ignore', 'ignore', 'pipe'], env, detached: true });
  return new ChildProc(proc);
}

async function startChild(setup) {
  // Launch a child and hand it the setup. Returns {child} once it acks, or
  // {error} (a compile failure) for the parent to report as a single row.
  const child = spawnChildProc();
  child.send(setup);
  const ack = await child.read(SETUP_GRACE_MS);
  if (ack.kind !== 'msg' || ack.msg.setup !== 'ok') {
    child.kill();
    return { error: ack.kind === 'msg' ? (ack.msg.error || 'the submission could not be started')
      : 'the submission could not be started' };
  }
  return { child };
}

function finalizeCase(tc, reply, comparison, timeLimitMs) {
  // Build the report row from the child's reply, computing pass/fail here.
  // Returns {result, dead} — dead means the child must be respawned next case.
  const tcId = tc.id !== undefined ? tc.id : 0;
  if (reply.kind === 'crash') {
    return { dead: true, result: { test_case_id: tcId, status: 'runtime_error', runtime_ms: 0,
      output: null, stdout: '', error: 'the submission exited before returning a value' } };
  }
  if (reply.kind === 'hang') {
    return { dead: true, result: timeoutResult(tcId, timeLimitMs, '') };
  }
  const msg = reply.msg;
  if (msg.status === 'time_limit_exceeded') {
    return { dead: true, result: timeoutResult(tcId, timeLimitMs, msg.stdout || '') };
  }
  if (msg.status === 'runtime_error') {
    return { dead: false, result: { test_case_id: tcId, status: 'runtime_error',
      runtime_ms: msg.runtime_ms || 0, output: null, stdout: truncate(msg.stdout || ''),
      error: truncate(msg.error || '') } };
  }
  const expected = tc.expected !== undefined ? tc.expected : null;
  const passed = compare(msg.actual, expected, comparison);
  return { dead: false, result: { test_case_id: tcId, status: passed ? 'passed' : 'wrong_answer',
    runtime_ms: msg.runtime_ms || 0, output: truncate(formatOutput(msg.actual)),
    stdout: truncate(msg.stdout || ''), error: null } };
}

async function run(payload) {
  const kind = payload.kind || 'function';
  const testCases = payload.test_cases || [];
  const firstId = testCases.length ? (testCases[0].id !== undefined ? testCases[0].id : 0) : 0;

  if (kind !== 'function') {
    return [{ test_case_id: firstId, status: 'runtime_error', runtime_ms: 0, output: null,
      stdout: '', error: `kind '${kind}' is not supported by the JavaScript harness` }];
  }

  const comparison = payload.comparison || { mode: 'exact' };
  const timeLimitMs = Math.max(parseInt(payload.time_limit_ms || 2000, 10), 1);
  const stopOnFirstFailure = !!payload.stop_on_first_failure;
  const setup = { user_code: payload.user_code, function_name: payload.function_name, time_limit_ms: timeLimitMs };
  const deadlineMs = timeLimitMs + KILL_GRACE_MS;

  let started = await startChild(setup);
  if (started.error !== undefined) {
    return [{ test_case_id: firstId, status: 'runtime_error', runtime_ms: 0, output: null,
      stdout: '', error: truncate(started.error) }];
  }
  let child = started.child;

  const results = [];
  try {
    for (const tc of testCases) {
      if (child === null) {
        started = await startChild(setup);
        if (started.error !== undefined) {  // compiled once already; if it fails now, blame the case
          results.push({ test_case_id: tc.id !== undefined ? tc.id : 0, status: 'runtime_error',
            runtime_ms: 0, output: null, stdout: '', error: truncate(started.error) });
          if (stopOnFirstFailure) break;
          continue;
        }
        child = started.child;
      }
      child.send({ input: tc.input || [] });
      const reply = await child.read(deadlineMs);
      const { result, dead } = finalizeCase(tc, reply, comparison, timeLimitMs);
      results.push(result);
      if (dead) { child.kill(); child = null; }
      if (result.status !== 'passed' && stopOnFirstFailure) break;
    }
  } finally {
    if (child !== null) child.kill();
  }
  return results;
}

async function main() {
  if (process.argv[2] === CHILD_FLAG) {
    await childMain();
    return;
  }

  // Payload channel: stdin by default (the `docker run -i` path). Under
  // Kubernetes there's no stdin pipe, so the runner mounts the payload as a file
  // and points JUDGE_PAYLOAD_FILE at it. Either way the trusted parent reads and
  // parses it, and removes the file before any user code runs, so the child
  // (which never gets the path) can't read the expected answers back out of it.
  const payloadFile = process.env.JUDGE_PAYLOAD_FILE;
  const raw = payloadFile ? fs.readFileSync(payloadFile, 'utf8') : fs.readFileSync(0, 'utf8');
  if (payloadFile) { try { fs.unlinkSync(payloadFile); } catch (e) { /* best effort */ } }
  let payload;
  try {
    payload = JSON.parse(raw);
  } catch (e) {
    process.stderr.write(`harness: invalid payload JSON: ${e.message}\n`);
    process.exit(2);
  }
  const results = await run(payload);
  // Wait for the write to reach the OS before exiting (a large report can exceed
  // the pipe buffer in one call); the callback, not a fixed delay, proves it.
  process.stdout.write(JSON.stringify({ results }), () => process.exit(0));
}

if (require.main === module) {
  main().catch((err) => {
    process.stderr.write(`harness: ${err && err.stack ? err.stack : err}\n`);
    process.exit(1);
  });
}

module.exports = { run, compare, formatOutput };
