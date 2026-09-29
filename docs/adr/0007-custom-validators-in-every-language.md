# ADR-0007: Custom validators in every language, judged in the trusted parent

- **Status:** Accepted: step 1 (function-mode validators in the parent) implemented in
  `judge/harness.py`, step 2 (the per-language `validator_code` map) in `app/comparison.py`
  and `ProblemIn`; steps 3–6 are open work (AGENTS.md TODO)
- **Date:** 2026-09-28

## Context

`custom_validator` (DESIGN.md §5.4) judges "any output satisfying property P" problems that no
fixed `expected` can express. It is Python-only: `ProblemIn` refuses it unless Python is the
problem's only language, and neither `harness.js` nor `harness_rs/` can run it. Nineteen
problems in an external problem set use it, and we want to offer them in Rust (and later in
other languages).

Reading all nineteen validators sorts them into three groups:

| Group | Needs | Count |
|---|---|---|
| A. check the output (function mode): wiggle-sort, reorganize-string, alien-dictionary, … | `actual`, `expected`, `args` | 6 |
| B. replay the ops against a model (operations): insert-delete-getrandom, all-oone, shuffle-an-array, … | `actual`, `args` | 5 |
| C. call back into the live `instance`: encode/decode round trips, random-pick distributions | `instance` | 8 |

Every group C call is one of two shapes: call method M a fixed number of times with arguments
that depend only on `args` (e.g. 4000 × `pickIndex()`), or call `decode` on a string an earlier
op returned. None depends on a result in any other way.

The review of that plan also found a live hole. ADR-0006 moved every submission into an
untrusted child and let the parent alone decide pass/fail, with one residual exception it
described as operations-only: "an operations `custom_validator` runs in the child." In fact
**every** `custom_validator` ran in the child, function mode included, and was sent `expected`.
Because the submission can write its own frame to the child's result pipe, it could forge a
`passed` verdict, not just read the answer.

## Decision

1. **Validators run in the trusted parent.** A validator is a pure function of values the
   parent already has, exactly like `compare()`. Function-mode validators moved there in
   step 1. The validator gets whatever is left of the case's `time_limit_ms` after the
   submission's run, so a case's total stays inside the budget `app/judge_budget.py` plans for,
   and a runaway validator is still `time_limit_exceeded`.
2. **Probes replace `instance`.** A test case may carry extra ops for the harness to append to
   the replay. A probe's argument may reference an earlier op's result, which covers
   `decode(encode(x))`. The child returns the full result list; the parent splits it into
   `actual` (the case's own ops: stored and shown to the user) and `probe_results`, and the
   validator becomes `validate(actual, expected, args, probe_results)`. Probes are **test-case
   data**, generated once per problem, not code: op names are already shared across languages,
   so every harness reads the same list, and seed-time checks (such as Rust's op-name mapping)
   can see them.
3. **`validator_code` becomes a per-language map** inside `comparison`:
   `{"python": "...", "rust": "..."}`, where a bare string lifts to `{"python": s}` (the same
   precedent as a solution's `code` map, ADR-0005). It stays in the `comparison` JSONB, which no
   API response exposes, so no migration is needed. One shared resolver picks the variant's
   string for the payload, so every harness keeps receiving `validator_code: str`. A language
   may later fall back to the Python validator where its judge image ships Python.
4. **Rust** compiles the validator into a separate binary, never linked with the submission.
   The parent runs it, and it is marked non-dumpable. Its compile shares the compile deadline
   and runs after the submission's, never alongside it, since rustc's peak memory sits inside
   the problem's limit. A validator that fails to compile is one `judge_error`, and its
   diagnostics are never shown to the user.
5. **A small RNG in the Rust prelude.** std has no random numbers and the sandbox has no
   crates, and 8+ of these problems need randomness. The RNG is seeded from `/dev/urandom`, and
   seed tests can pin the seed.

Order of work (progress is on the Status line): (1) function-mode validators in the parent;
(2) the `validator_code` map, resolver and `ProblemIn` rule, with a data migration so stored
rows are always the map and a rollback converts them back to the string the previous release
can run; (3) probes in both harnesses, keeping the legacy `instance` path
for any validator that doesn't take `probe_results`, since the problem set lives in a separate
repo and migrates on its own schedule; (4) the Rust validator binary and prelude RNG; (5) port
the problems; (6) remove `instance`.

## Alternatives considered

| Option | Why not |
|---|---|
| Keep validators in the child, ported to each language | The child's verdict is forgeable in every language. In Rust, the result file is a fixed path the submission can write, and it can `process::exit` before any validator runs. |
| One Python validator for every language (Python in each judge image) | Single-source, with no ports and no drift, but it puts Python in every judge image and makes each harness depend on another runtime. Kept as a per-language fallback option rather than the rule. |
| Probes as a `probes(args)` function in each validator | A second copy of the logic per language, and in Rust it forces "compile the validator, run it, then generate the glue", because probe op names need dispatch arms. |
| Magic `{"$result": i}` values inside probe args | Could collide with real argument data. References go in a separate field on the probe. |
| Give the parent-side validator its own full time limit | Doubles a case's worst case past what `wall_budget_s` plans for. Sharing the case's limit keeps today's budget exactly. |

## Consequences

- Function-mode `custom_validator` problems can no longer leak `expected` or forge a verdict.
  Regression tests pin both (`judge/tests/test_protocol.py`).
- A function-mode validator sees `actual` after the result pipe's JSON round trip (a tuple
  arrives as a list), the same value `compare()` judges. Its time is no longer counted in
  `runtime_ms`, which now measures the submission alone.
- Until step 3, operations validators still run in the child (DESIGN.md §5.3).
- With probes, an exception in a probe call becomes the submission's `runtime_error`, not
  `judge_error`. That is right, since it is the submission's method that raised, but the
  message must say the call was added by the judge.
- Each language's validators must be kept in step: seed tests run every language's solutions,
  and a known-wrong solution per problem proves each validator rejects.
