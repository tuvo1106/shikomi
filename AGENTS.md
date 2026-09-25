# AGENTS.md

Working notes for anyone (human or AI) contributing to this repo. Keep this file
current as decisions are made. [DESIGN.md](DESIGN.md) is the source of truth for
*what* we're building; this file is *how to work in the code* and *why things are
the way they are*.

## Environment

- **Python**: 3.12, managed by `uv` (it owns the interpreter and the venv). The
  judge `harness.py` is deliberately kept 3.9-compatible so it also runs under an
  old system Python during local harness testing.
- **Docker engine**: required for the sandbox and for Postgres/Redis in tests. On
  macOS the `docker` CLI alone is not enough — you need an engine
  (`colima start` or Docker Desktop). The worker talks to the host Docker socket.
- **Local service containers** (started once, reused across sessions):
  - `shikomi-pg` — Postgres 16 on `localhost:5432`, creds `app` / `app`, db `shikomi`
  - `shikomi-redis` — Redis 7 on `localhost:6379`
  - Test database `shikomi_test` must exist on `shikomi-pg`.

## Commands

Run from `backend/` unless noted.

| Task | Command |
|------|---------|
| Bring up whole dev env (idempotent) | `scripts/dev-up.sh` (repo root) — reuses running containers, restarts servers |
| Stop dev app servers | `scripts/dev-down.sh` (repo root) |
| Backend tests + coverage gate | `uv run pytest` |
| Lint | `ruff check backend judge` (from repo root) |
| Migrate dev DB | `uv run alembic upgrade head` |
| New migration (autogenerate) | `uv run alembic revision --autogenerate -m "msg"` |
| Seed the bundled starter problems | `uv run python -m app.cli seed` |
| Seed your own problems | `uv run python -m app.cli seed --dir /path/to/problems` |
| Validate a problem directory against the real harness | `SEED_DIR=/path/to/problems pytest judge/tests/test_seed_solutions.py` (repo root) |
| Verify an account by hand | `uv run python -m app.cli verify-email <email>` |
| Regenerate schema diagram | `uv run python -m app.cli schema-docs` → `docs/schema.md` |
| Run CI's jobs locally (fast: lint, harness, backend, frontend) | `scripts/ci-local.sh` (repo root); `--changed` = only what your diff touches (the pre-push hook); `full` = also sandbox/e2e/playwright; or name jobs: `scripts/ci-local.sh backend lint`. `e2e`/`playwright` stop a running dev stack (same ports) and use a throwaway per-checkout `shikomi_ci_<hash>` DB (all its DBs are suffixed with a hash of the checkout path, so concurrent runs in different worktrees can't collide on the shared test DB) — run `scripts/dev-up.sh` afterwards |
| Install git hooks (commit format + pre-push CI) | `lefthook install` (once per clone — hooks aren't active until you do) |
| Run API (dev) | `uv run uvicorn app.main:app --reload` |
| Run judge worker | `uv run arq worker.main.WorkerSettings` |
| Run accounts worker | `uv run arq worker.main.AccountsWorkerSettings` — **required for account email**: register/reset enqueue onto `arq:accounts`, so without it mail silently never sends |
| E2E pipeline check | `uv run python ../scripts/e2e_submit.py` (needs API + worker + Docker) |
| Judge one payload (no Docker) | `uv run python -m worker.judge_local --subprocess payload.json` |
| Judge one payload (real sandbox) | `uv run python -m worker.judge_local payload.json` |
| Backend real-container test | `uv run pytest -m docker` (from `backend/`, needs judge image) |
| Harness protocol tests | `pytest -m "not docker"` (repo root) |
| Sandbox isolation tests | `docker build -t shikomi-judge:latest judge/ && docker build -f judge/Dockerfile.js -t shikomi-judge-js:latest judge/ && docker build -f judge/Dockerfile.sql-mysql -t shikomi-judge-sql:latest judge/ && pytest -m docker` (root) |
| Frontend unit/component tests | `pnpm test` (from `frontend/`, Vitest) |
| Browser E2E | `pnpm e2e` (from `frontend/`, Playwright — needs `scripts/dev-up.sh` running; the 2FA spec shells out to `uv run python -m app.cli verify-email`, and the dev API must run with `AUTH_RATE_LIMIT_PER_MINUTE` raised, which `dev-up.sh` does — a hand-started API at the default 10/min makes auth specs fail with "Too many attempts") |

## Test suites (there are two pytest configs)

- **Root `pyproject.toml`** drives `judge/tests/` — the harness protocol tests
  (`-m "not docker"`, stdlib + pytest only), seed-solution validation
  (`test_seed_solutions.py`: every solution in `seed/problems/` — or
  `$SEED_DIR` — run through `harness.py` against its own test cases; CI only
  runs it when the diff touches `seed/problems/` or `judge/`, since large-N
  reference solutions make it the slow part of the job — always run it locally
  when touching either), and the sandbox isolation
  tests (`-m docker`, real containers). These need no backend deps.
  - **The root `.venv` must be 3.12**, because this suite executes seed
    solutions with whatever interpreter runs pytest — unlike `harness.py`
    itself, solution code is only ever run by the judge container
    (`judge/Dockerfile`: 3.12) and by CI (`setup-python` 3.12), so it is
    written for 3.12 and nothing keeps it 3.9-compatible. A root `.venv` built
    from an old system Python instead reports phantom failures: a solution
    annotated `list[int] | None` dies with `unsupported operand type(s) for |`
    locally while passing CI and production. Rebuild with
    `uv venv --python 3.12 && uv pip install pytest` rather than changing the
    seed file — a green CI run on a test that fails locally is the tell. Keep
    the root venv pytest-only: `ruff` belongs to `backend`'s lockfile, and CI
    lints with `uv run ruff check` from there precisely so the version is
    pinned, so a floating `ruff` installed here just disagrees with it.
- **`backend/pyproject.toml`** drives `backend/tests/` — API + service tests
  against a **real Postgres** (only `get_session` is overridden; nothing is
  mocked), plus pure-unit tests for verdict aggregation and the judge CLI.

CI (`.github/workflows/ci.yml`) runs these as separate jobs: `lint`, `harness`,
`sandbox`, `backend`, plus `frontend` (Vitest + build), `e2e` (`scripts/e2e_submit.py`)
and `playwright`.

### Databases (three of them)

- **`shikomi`** — local dev DB (the `shikomi-pg` container). Seed a known, verified user
  with `python -m app.cli seed-dev-user` (`dev@example.com` / `devpassword`, dev-only,
  idempotent) so you don't have to register after recreating the container.
- **`shikomi_test`** — pytest DB; `conftest.py` drops+recreates the schema per test,
  so the suite is fully isolated and safe to run repeatedly.
- **CI e2e** — an ephemeral Postgres service per run. `scripts/e2e_submit.py`
  registers with a *random* email/username each run, so re-running never collides;
  locally it writes throwaway rows into `shikomi` (harmless, not isolated).

### Test characteristics (important context)

- The API tests (auth/problems/submissions) are **integration tests**, not unit tests:
  full request → router → service → real DB. Services are covered *through* the
  API. Direct service unit tests exist only where the API can't reach a branch
  (e.g. expired refresh tokens in `test_auth_service.py`).
- **Isolation**: each test gets a freshly dropped+created schema (`conftest.py`),
  so there is zero cross-test bleed. Simple and correct, slightly slower than
  transaction rollback.
- **Coverage gate is 80%** (`--cov-fail-under=80`); currently ~92%. The remaining
  gap is `worker/docker_runner.py` / `worker/judge_local.py`, which are exercised
  by the Docker-marked sandbox job rather than the backend unit job.

## Architectural decisions (and the reasoning)

- **The API never runs user code.** It writes the submission, enqueues a job, and
  returns; the worker judges in a throwaway container. This queue seam is the
  scaling point. (DESIGN.md §1)
- **Function-call judging**, not stdin/stdout: the harness imports the user's
  module and calls the named function with JSON args. (DESIGN.md §5, §12)
- **Test-case authoring**: each problem should have ~10 small edge-case tests
  plus **1–2 large hidden cases near the constraint max**, so runtimes are
  meaningful/stable and O(n²) solutions are caught. Submission runtime is the
  **sum** of per-case times, so the large cases dominate.
- **Run-all judging** (default): every case runs so the verdict includes a
  `passed`/`total` count; a problem may set `stop_on_first_failure`. Bounded by
  the per-case SIGALRM limit and the worker's wall-clock kill
  (`len(cases) × time_limit_ms + 10s`).
- **Non-compiling / load-failing code is contained**: `SyntaxError` and top-level
  exceptions are caught at `compile`/`exec` (once, before the per-case loop, so it
  fails fast) and returned as a single `runtime_error` result with the error
  message — no crash/hang. The count still reads `0/N` against the real case count
  (`aggregate(..., total_cases=N)`), not `0/1`. Compile errors are not
  distinguished from runtime errors (both `runtime_error`); a separate
  `compile_error` status is a possible future nicety. See DESIGN §5.3.
- **Verdict precedence**: container-level signals (wall-clock kill→`time_limit_exceeded`,
  OOM→`memory_limit_exceeded`, stdout>1MB→`output_limit_exceeded`, unparseable
  output→`judge_error`) are decided before per-case results — and among those,
  `timed_out` is checked *before* `oom_killed`: a `docker kill` (our own
  wall-clock kill) and Docker's own OOM kill share the same exit code (137,
  SIGKILL), so once we've killed the container ourselves its exit code can no
  longer distinguish "we killed it for hanging" from "it actually got
  OOM-killed" — checking OOM first would misreport every wall-clock kill as
  `memory_limit_exceeded`. See `worker/aggregate.py`.
- **Hidden test cases never leave the server** — enforced in the Pydantic response
  schemas, not ad hoc. Sample cases only in `ProblemDetail`; no response schema can
  serialize a hidden case.
- **Problems enter only through `app.cli seed`** — there is no write API, admin role,
  or admin UI (DESIGN.md §4.4, §12). Shell access to the api container is the trust
  boundary for every operator action. A seed is validate-everything-then-write-once:
  `ProblemFile` carries every authoring rule, and `problem_service.upsert_problem`
  never commits on its own — keep new rules in the schema so they're checked before
  any write.
- **Security model = contain, don't filter**: no content blocklisting of
  submissions; safety comes from a locked-down, secretless, ephemeral sandbox.
  Full write-up in DESIGN.md §5.8; residual risk is the host Docker socket /
  shared kernel (→ gVisor/Firecracker before public exposure).
- **`user_status` (solved/attempted/unsolved) is derived** from `submissions` at
  read time, not stored. `is_run` submissions are excluded.
- **Refresh tokens** are opaque, hashed, rotating, with reuse-detection (a replay
  revokes all sessions). Access tokens are stateless JWTs. (DESIGN.md §3.6)
- **Rate limiting**: auth (login/register) uses a **Redis** per-IP window
  (`app/rate_limit.py`, via `Queue.within_rate_limit`) — so the cap survives
  restarts and is shared across API replicas.
  Submissions use the same **Redis** mechanism, keyed per-user
  (`submit` 10/min, `run` 15/min, env-configurable). Both are *rate* throttles;
  the in-flight lock is a separate *concurrency* throttle (submit only).
- **Errors** use `APIError` → `{"detail", "code"}` via one exception handler.
- **One in-flight submission per user per problem**: a Redis `SETNX` lock
  (`inflight:{user}:{problem}`, 120s TTL) acquired by the API on submit, released
  by the worker (or the lock's TTL / the sweeper). `/run` takes no lock.
  Queue + lock live behind `app/queue.py` so tests substitute a `FakeQueue`.
- **Enqueue ordering** (§5.7): write the submission row, *then* enqueue. If the
  queue is unavailable, the row is marked `judge_error` and the API returns 503 —
  never accept a submission that can't be judged. A crash between the two is repaired by
  the sweeper, which re-enqueues young `pending` rows (the submission id is the arq job
  id, so a duplicate is refused).
- **Judge execution lives in one place** (`worker/judging.py::run_judgement`),
  called by the `judge_submission` worker job for both Run and Submit, so the two
  judge identically. It runs only in the worker, never the API process.
- **`verdict_detail` redaction happens in the worker**: hidden (non-sample) cases
  are stored with status + runtime only, so hidden inputs/outputs never reach the
  client even via a submission poll.

## Gotchas / lessons learned

- **pnpm is pinned (`frontend/package.json` `packageManager`), so bump it deliberately.**
  CI, the web image (corepack) and local installs all read that one pin. Some pnpm 11
  releases don't pass Playwright's shutdown signal on to the `pnpm dev` it starts as its
  `webServer`, so every spec passes and then the run hangs. After a bump, check that
  `CI=true pnpm e2e` exits. Every CI job also has `timeout-minutes`, so a hang fails fast.
- **Coverage + async SQLAlchemy**: coroutine bodies run inside SQLAlchemy's
  greenlet, which coverage doesn't trace by default. Without
  `concurrency = ["greenlet"]` in `[tool.coverage.run]`, async service coverage is
  badly under-reported (looked like 42% when it was really ~95%).
- **The backend suite never touches the network for breach screening.**
  `conftest.py`'s autouse `no_breach_network` turns `BREACHED_PASSWORD_CHECK` off
  and installs an `httpx.MockTransport` that raises `AssertionError` on any
  request — deliberately not an httpx error, so the fail-open path can't swallow
  it and a stray real call fails the test instead of silently passing. Tests of
  screening itself opt back in via `test_breached_passwords.py`'s `hibp` fixture.
- **`citext`** requires `CREATE EXTENSION citext` — done at the top of the initial
  migration. Autogenerate won't emit it; add it manually to new migrations if
  needed.
- **FastAPI lazy router inclusion**: `app.routes` shows `_IncludedRouter` wrappers,
  not flat routes. Use `app.openapi()["paths"]` to inspect registered endpoints.
- **The DESC index** (`ix_submissions_user_problem_created`) survives autogenerate
  as a `literal_column('created_at DESC')` — verify it stays on schema changes.
- Two pytest configs means `pytest` from the wrong directory collects the wrong
  suite. Run backend tests from `backend/`, judge tests from the repo root.
- **`docker kill` vs. `docker ps` consistency**: `docker kill` returns once the
  daemon accepts the signal, not once `docker ps` reflects the container as
  stopped — under CI load that state update can lag a beat. A test asserting a
  killed container is gone via a single immediate `docker ps` check (rather than
  a short poll) is a latent flake — hit this in `test_sweep_orphans_kills_leftovers`.
- **Navbar height is load-bearing**: `Navbar.tsx` is `h-14` (3.5rem) and three
  layouts hard-code `calc(100vh-3.5rem)` against it (`Workspace.tsx`, and
  `AuthForm.tsx` twice). Changing the nav height silently breaks them — grep
  `3.5rem` before touching it.
- **SQL seed schemas often have no indexes**: a `kind: "sql"` problem's seed
  script is usually plain `CREATE TABLE` with no `PRIMARY KEY`, so the query
  planner has nothing to join on and falls back to nested-loop scans. A
  join-based solution that looks equivalent to a subquery-based one can be
  orders of magnitude slower (15+ seconds vs ~50ms at 5000 rows has been
  measured). Measure any SQL reference solution against a real container
  before shipping it.
  **When the statement documents a key, the DDL must declare it** — otherwise
  the judge grades a table the statement doesn't describe, and a self-join the
  statement promises is `O(n)` runs quadratically (one measured ~900ms on 5000
  rows against a 3000ms limit, passing locally and timing out on CI; declaring
  the key took it to ~5ms). Declare composite keys too. Only a table the
  statement says has *no* key (because duplicate rows are the point) should
  declare none. A key the seeded data violates fails loudly (`seed error:
  (1048, ...)`), so the suite passing is real evidence the data satisfies it.
- **A statement's complexity claims are a contract the shipped solutions must
  meet.** Nothing catches a mismatch — the judge compares outputs, so a solution
  that is asymptotically wrong still passes. Typical misses: an `O(1)` op that
  re-sums a collection, an `O(1)` count that builds a set, an `O(n)` op that's
  `O(n^2)`, or a bound no implementation can meet (an `O(k)` per-op promise when
  every op returns the whole structure). When writing or reviewing a problem,
  read the statement's complexity paragraph against each solution's actual code,
  and check the `*_reason` text agrees with the `time_complexity`/
  `space_complexity` field next to it — a `*_reason` that contradicts its own
  field is the cheapest tell.
- **A large test case can exercise nothing.** A 4000-op case that constructs its
  object with an unrecognized argument can send every call through a guard
  clause, so the behaviour the problem exists to teach never runs once — and the
  case passes against an implementation consisting only of the guard. Op
  *count* is not coverage: after generating a big case, check the distribution
  of the results it expects. One repeated value means the case is pinning one
  branch.
- **Seed files: keep them in the 100-270KB band, and watch collection-returning
  ops.** The bloat is never the op count, it's an observable that returns a
  whole collection called repeatedly against a large structure (a `history()`
  called ~200 times on a ~1900-deep stack produced a 1.9MB file). Drive the
  large case with the scalar-returning ops and call the collection one a
  handful of times, deep enough to be worth checking.
- **`"${ARR[@]}"` on an empty array + `set -u`**: bash 3.2 (macOS's default
  `/bin/bash` — Apple ships it for licensing reasons and doesn't update it)
  treats expanding an empty array with `[@]` as an unbound variable under
  `set -u`/`nounset`, even though the array itself was initialized (`ARR=()`).
  Bit `scripts/k8s-up.sh`'s default (non-`--keda`) path, where `HELM_ARGS` stays
  empty. Portable fix: `${ARR[@]+"${ARR[@]}"}` instead of `"${ARR[@]}"`.

- **A node param's index doesn't address the input on `kind: "operations"`
  problems.** An operations-kind test case's `input` is `[ops, args]`, so
  `params[i]` indexes a schema that describes the *constructor*, not that array.
  An operations problem with a `TreeNode`/`ListNode` constructor param would be
  read wrong by anything that treats a param index as a position in `input` —
  `format.ts`'s `isOperationsInput` and `nodeGraph.ts`'s `nodeParams` each gate
  on `kind` for exactly this reason.
- **The judge's output codec is not the mirror of its input codec for
  `CyclicListNode`.** `_encode_cyclic_node` returns a bare *index* (node identity,
  via an `_idx` stamp) rather than the `[values, pos]` pair `_build_cyclic_list`
  consumes, so its `expected` can't be decoded with the input decoder — a "where
  does the cycle begin" answer is "which node", not "what shape". Anything reading
  `expected` structurally has to special-case it; every other codec round-trips.

## Deferred / TODO

Open work only. Shipped work is recorded in [CHANGELOG.md](CHANGELOG.md); when a
slice ships, delete its entry here.

- **Single-user mode:** an optional `AUTH_MODE=single` that auto-signs-in one local
  user (created on first start), so a solo self-hoster can run shikomi without SMTP,
  registration, or email verification. Auth stays the default — it's what the rate
  limits, the in-flight lock, and per-user history hang off (DESIGN.md §11). Needs:
  the API to mint a session for that user without a password, the frontend to skip
  the auth pages, and a hard refusal to start under `ENV=prod` unless explicitly
  allowed.

- **User menu hidden behind the workspace pane:** after a submit, the navbar's
  user-menu dropdown (Sign out included) renders *behind* the workspace's top-right
  pane, so it can't be clicked. Likely a stacking-context / `z-index` clash with the
  pane that appears post-submit. Fix it and add a Playwright check that the menu is
  clickable after a verdict.

- **Deploy hardening:** run the backend containers as non-root (the worker needs
  docker-socket group access), pin base-image digests, healthcheck-gated rollout.
  Point an external monitor at `/internal/accounts-queue-depth`
  (`oldest_age_seconds`): the in-app watchdog alerts on the same number but shares
  fate with the judge worker, so it can't tell you the whole stack is down.

- **Kubernetes (remaining):** HPA on the api, managed Postgres + Redis, Ingress +
  cert-manager TLS, a **gVisor/Kata node pool** (`runtimeClassName`) + a
  policy-enforcing CNI before untrusted users, and wiring kind into CI for the
  Playwright job. Problems reach the migrate hook as a ConfigMap (`scripts/k8s-up.sh`,
  `PROBLEMS_DIR`), which caps at 1 MiB — a real problem set needs a PVC, an
  init-container `git clone`, or an image layer instead.

- **Auth roadmap:** revisit session strategy (currently JWT-in-memory access +
  httpOnly refresh cookie — consider server-side sessions / cookie-based access
  tokens); two-factor follow-ups: a "remember this device" option and
  WebAuthn/passkeys. Staying hand-rolled (bcrypt + pyjwt + TOTP primitives), no auth
  framework or hosted provider.

## Conventions

- Commit messages: [Conventional Commits](https://www.conventionalcommits.org/)
  (`type(scope): subject`); **no `Co-Authored-By` trailer** (maintainer preference).
  Enforced by a `commit-msg` hook — run `lefthook install` once after cloning.
  See [CONTRIBUTING.md](CONTRIBUTING.md) for the full commit/PR/ADR process.
- **Never commit directly to `main`.** One PR per feature or slice.
  Branch prefixes: `feat/<slug>`, `fix/<slug>`, `chore/<slug>`, `docs/<slug>`.
- **Build as if this repo were public.** No secrets, credentials, tokens, or real
  user data committed — ever, not "temporarily," not in a branch you plan to
  squash. Config comes from environment variables (`.env`, gitignored). A
  `no-secrets` pre-commit hook backs this up but isn't a substitute for not
  doing it in the first place.
- Match surrounding code style; `ruff` is the formatter/linter of record.
- **Keep page components split** — put a page's parts in a `pages/<page>/` folder
  (one component/concern per file), not one giant file. See `pages/workspace/`.
- Update this file (and the README, if how to run/use it changed) when a slice lands.
- **Docstrings teach (this is a learning project).** Document the *why*, not just
  the *what*. Every module gets a docstring framing what it's for and the mental
  model behind it; every public function/class explains its reasoning (trade-offs,
  invariants, gotchas) with `Args:`/`Returns:`/`Raises:` where non-obvious; the
  non-obvious lines get an inline `# why` comment. `app/security.py` is the
  reference depth. Frontend uses the same standard as JSDoc (`/** … */`) on
  modules, exported functions/components, and tricky hooks.
