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

function errorResult(testCaseId, err, stdout) {
  return {
    test_case_id: testCaseId,
    status: 'runtime_error',
    runtime_ms: 0,
    output: null,
    stdout: truncate(stdout || ''),
    error: truncate(formatUserError(err)),
  };
}

// Per-case (not top-level, unlike errorResult above) time_limit_exceeded /
// runtime_error builders — shared by the synchronous vm timeout path and the
// async awaitIfThenable path below so the two don't drift out of sync with
// each other on the result shape.
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

function runtimeErrorResult(testCaseId, err, runtimeMs, stdout) {
  return {
    test_case_id: testCaseId,
    status: 'runtime_error',
    runtime_ms: runtimeMs,
    output: null,
    stdout: truncate(stdout || ''),
    error: truncate(formatUserError(err)),
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

// --- execution ---------------------------------------------------------------

/**
 * Compile the user's code once, then run it against each test case.
 *
 * Mirrors harness.py's `run()`: `vm.Script`/`runInContext` the submission into
 * a fresh context (a top-level failure — syntax error, throw at def time —
 * becomes a single runtime_error), look up `function_name` in that context,
 * then loop the cases. Per case we JSON-round-trip the input (so one case's
 * mutation can't leak into the next, like Python's `copy.deepcopy`), call the
 * function inside a timed `vm.runInContext` (Node's closest analogue to the
 * Python harness's per-case SIGALRM), capture `console.log` output, and
 * compare the result.
 *
 * If the call returns a thenable, it's awaited (`awaitIfThenable`) against
 * whatever's left of the case's `time_limit_ms` after the synchronous portion
 * — `vm`'s own `timeout` option only ever covers the synchronous call itself,
 * never a Promise it hands back, so this is a second, separate timeout on the
 * host's real event loop rather than an extension of the first.
 */
async function run(payload) {
  const kind = payload.kind || 'function';
  const testCases = payload.test_cases || [];
  const firstId = testCases.length ? (testCases[0].id !== undefined ? testCases[0].id : 0) : 0;

  if (kind !== 'function') {
    return [{
      test_case_id: firstId,
      status: 'runtime_error',
      runtime_ms: 0,
      output: null,
      stdout: '',
      error: `kind '${kind}' is not supported by the JavaScript harness`,
    }];
  }

  const functionName = payload.function_name;
  const userCode = payload.user_code;
  const comparison = payload.comparison || { mode: 'exact' };
  const timeLimitMs = Math.max(parseInt(payload.time_limit_ms || 2000, 10), 1);
  const stopOnFirstFailure = !!payload.stop_on_first_failure;

  let buffer = [];
  const sandbox = {
    console: { log: (...args) => buffer.push(args.map(consoleArgToString).join(' ')) },
    // Bound in deliberately, matching harness.py's exec() giving user code full
    // stdlib access (`import socket`/`import os`) — the container, not the
    // language, is the actual security boundary (DESIGN.md §5.8: --network=none,
    // read-only root FS, cap-drop, pids-limit). Only core modules resolve since
    // nothing else is installed in the image (judge/Dockerfile.js).
    require,
    // Real (host) timers, not vm-scoped ones — a Sleep/Debounce-style solution
    // needs setTimeout to actually fire on the real event loop for its Promise
    // to ever settle. Bounding how long that's allowed to take is
    // awaitIfThenable's job (via the case's time_limit_ms), not the sandbox's.
    // setTimeout/setInterval are the *scoped* wrappers (above), not the raw
    // globals — clearTimeout/clearInterval stay raw since they only ever need
    // to cancel a real timer handle, never to decide whether to run anything.
    setTimeout: scopedSetTimeout,
    clearTimeout,
    setInterval: scopedSetInterval,
    clearInterval,
  };
  vm.createContext(sandbox);

  try {
    const script = new vm.Script(userCode, { filename: USER_FILENAME });
    script.runInContext(sandbox);
  } catch (err) { // any top-level failure -> single runtime_error, matching harness.py
    return [errorResult(firstId, err, '')];
  }

  // `sandbox[functionName]` would miss this: only top-level `var`/`function`
  // declarations become properties of the contextified global object — a
  // `const`/`let` (including an arrow function, e.g. modern-style starter
  // code) is a lexical binding the object-property lookup can't see, even
  // though it resolves fine from further code run in the same context (which
  // is how the actual per-case call below invokes it). Checking via `typeof`
  // through runInContext resolves the same lexical environment the call does,
  // so it doesn't falsely report a real, callable function as missing.
  let exists;
  try {
    exists = vm.runInContext(`typeof ${functionName} === 'function'`, sandbox);
  } catch (err) {
    exists = false;
  }
  if (!exists) {
    return [{
      test_case_id: firstId,
      status: 'runtime_error',
      runtime_ms: 0,
      output: null,
      stdout: '',
      error: `Function '${functionName}' not found`,
    }];
  }

  const results = [];
  let caseCounter = 0;
  for (const tc of testCases) {
    const tcId = tc.id !== undefined ? tc.id : 0;
    const args = JSON.parse(JSON.stringify(tc.input || []));
    const expected = tc.expected !== undefined ? tc.expected : null;

    buffer = [];
    sandbox.__args = args;
    // A fresh token per case (an incrementing counter is enough — uniqueness,
    // not unguessability, is all scopedSetTimeout/scopedSetInterval need) so
    // any timer callback scheduled during a *previous* case is recognizably
    // stale by the time this one starts.
    activeCaseToken = ++caseCounter;
    const start = process.hrtime.bigint();
    let actual;
    try {
      vm.runInContext(`__result = ${functionName}.apply(null, __args);`, sandbox,
        { timeout: timeLimitMs, filename: 'harness_call.js' });
      actual = sandbox.__result;
    } catch (err) {
      if (isTimeoutError(err)) {
        results.push(timeoutResult(tcId, timeLimitMs, buffer.join('\n')));
      } else {
        results.push(runtimeErrorResult(tcId, err, msSince(start), buffer.join('\n')));
      }
      if (stopOnFirstFailure) break;
      continue;
    }

    // awaitIfThenable is a no-op for an ordinary synchronous return (its own
    // thenable check), so it's always safe to call rather than duplicating
    // that check here too.
    actual = await awaitIfThenable(actual, timeLimitMs - msSince(start));
    if (actual === ASYNC_TIMEOUT) {
      results.push(timeoutResult(tcId, timeLimitMs, buffer.join('\n')));
      if (stopOnFirstFailure) break;
      continue;
    }
    if (actual instanceof AsyncError) {
      results.push(runtimeErrorResult(tcId, actual.err, msSince(start), buffer.join('\n')));
      if (stopOnFirstFailure) break;
      continue;
    }

    const elapsedMs = msSince(start);
    const passed = compare(actual, expected, comparison);
    results.push({
      test_case_id: tcId,
      status: passed ? 'passed' : 'wrong_answer',
      runtime_ms: elapsedMs,
      output: truncate(formatOutput(actual)),
      stdout: truncate(buffer.join('\n')),
      error: null,
    });
    if (!passed && stopOnFirstFailure) break;
  }

  // Nothing scheduled from here on can be "the active case" — any callback
  // that still fires (a leaked timer from the last case) is now
  // unconditionally stale and a no-op in scopedSetTimeout/scopedSetInterval.
  activeCaseToken = null;
  return results;
}

async function main() {
  // Payload channel: stdin by default (the `docker run -i` path). Under
  // Kubernetes there's no stdin pipe, so the runner mounts the payload as a
  // file and points JUDGE_PAYLOAD_FILE at it — same duality as harness.py.
  const payloadFile = process.env.JUDGE_PAYLOAD_FILE;
  const raw = payloadFile ? fs.readFileSync(payloadFile, 'utf8') : fs.readFileSync(0, 'utf8');
  let payload;
  try {
    payload = JSON.parse(raw);
  } catch (e) {
    process.stderr.write(`harness: invalid payload JSON: ${e.message}\n`);
    process.exit(2);
  }
  const results = await run(payload);
  // Explicit exit, not a fall-off-the-end: a submission that leaked a
  // setInterval/unresolved timer (awaitIfThenable clears its own, but nothing
  // clears the user's — scopedSetTimeout/scopedSetInterval only stop it from
  // running further user code, not from existing) would otherwise keep the
  // event loop — and this container's sandbox slot — alive until the outer
  // wall-clock kill. But exiting is only safe *after* the write actually
  // reaches the OS: a pipe write is asynchronous, and a large result set can
  // exceed the pipe buffer in one call, so calling exit() synchronously right
  // after write() can truncate the very output this whole harness exists to
  // produce. The write's own callback — not a fixed delay — is what proves
  // it's safe to exit.
  process.stdout.write(JSON.stringify({ results }), () => process.exit(0));
}

if (require.main === module) {
  main().catch((err) => {
    process.stderr.write(`harness: ${err && err.stack ? err.stack : err}\n`);
    process.exit(1);
  });
}

module.exports = { run, compare, formatOutput };
