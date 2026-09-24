# Shikomi

**A self-hosted coding-practice judge for your own problems.**

Shikomi is the platform half of a coding-practice site. It gives you a browser
editor, a sandboxed judge for Python, JavaScript and SQL, accounts, submission
history, and editorial solutions. It ships **no problem catalog**. You write
problems as JSON files and load them with one command. Five original starter
problems are included to show the format.

*Shikomi* (仕込み) is the kitchen prep done before service: the stock, the cut
vegetables, the part nobody sees. Practice is prep.

## Why bring your own problems?

- **You own the content.** Your problems stay in your own repo (public or
  private), versioned and reviewed like code. The platform never needs them.
- **No licensing question for the project.** Shikomi contains only original
  material, so there's nothing borrowed to defend.
- **Teach what you want.** Interview prep for a team, a course's homework, a
  design-patterns workshop, a SQL onboarding track. The judge doesn't care which.

> **Your responsibility:** only load problems you have the right to use. Writing
> your own is the safe path. Don't copy statements, examples or test data from
> other platforms.

## Features

- **Three languages, three problem shapes.**
  - Python and JavaScript *functions*.
  - Python *classes* replayed against a sequence of method calls.
  - *SQL queries* against a per-test-case MariaDB database.
- **Real data structures as input and output.** The harness builds and compares
  linked lists, binary trees, cyclic lists, random-pointer lists and graphs from
  JSON encodings, and the workspace draws them as diagrams.
- **Sandboxed judging.** Every submission runs in a throwaway, locked-down
  container with no network, a read-only filesystem, dropped capabilities and
  resource limits. The API never executes user code; it enqueues a job for a
  separate worker.
- **Flexible checking.** Exact, unordered, float-tolerance and any-of
  comparison, plus per-problem time and memory limits.
- **Workspace.** Monaco editor, Run (sample cases) and Submit (all cases),
  per-case verdict detail, submission history, a runtime distribution, and
  editorial solutions behind a spoiler guard.
- **Accounts with serious auth.**
  - Email verification, rate limiting and lockout.
  - A password policy with breached-password screening.
  - Optional TOTP two-factor, JWT key rotation, and an audit log.
- **Deploys three ways.**
  - Hot-reload processes for development.
  - A production-shaped Docker Compose stack behind Caddy.
  - A Helm chart with one sandbox Pod per submission and KEDA scale-to-zero.

## Quick start

You need [`uv`](https://docs.astral.sh/uv/), [`pnpm`](https://pnpm.io/) and a
Docker engine. On macOS, the `docker` CLI alone isn't an engine: run
`brew install colima && colima start` or install Docker Desktop.

```bash
git clone https://github.com/tuvo1106/shikomi && cd shikomi
(cd frontend && pnpm install)
docker build -t shikomi-judge:latest judge/     # the Python sandbox image
scripts/dev-up.sh                               # → http://localhost:5173
```

`dev-up.sh` does the whole setup. It is safe to re-run.

1. Starts Postgres and Redis containers.
2. Runs the migrations.
3. Loads the starter problems.
4. Creates a dev login, `dev@example.com` / `devpassword`.
5. Launches the API, the worker and the frontend.

Stop the app processes with `scripts/dev-down.sh`. The containers keep running.

For JavaScript and SQL problems, also build their sandbox images:

```bash
docker build -f judge/Dockerfile.js        -t shikomi-judge-js:latest  judge/
docker build -f judge/Dockerfile.sql-mysql -t shikomi-judge-sql:latest judge/
```

## Adding your own problems

A problem is one JSON file holding the statement, the test cases and the
reference solutions. [DESIGN.md §7.1](DESIGN.md) documents the format, and the
starters in [`seed/problems/`](seed/problems/) are complete examples. Keep your
problems wherever you like; a separate git repo works well.

```bash
# 1. Check every reference solution passes every test case, using the real harness
SEED_DIR=~/my-problems/problems .venv/bin/pytest judge/tests/test_seed_solutions.py -v

# 2. Load them (an idempotent upsert keyed on slug, so re-run after every edit)
cd backend && uv run python -m app.cli seed --dir ~/my-problems/problems
```

Loading validates every file first, against the schema and the judge-time
budget the worker enforces, and writes nothing if any file fails. So a malformed
problem fails here, not on a user's first submission, and never leaves the
catalog half-loaded. Problems are added or updated, never deleted: removing a
file doesn't remove its problem. With the Compose stack, set `PROBLEMS_DIR=~/my-problems/problems`
before `scripts/prod-up.sh`, and the migrate step loads your directory instead
of the starters.

To set up the repo-root test environment used in step 1, run
`uv venv .venv && uv pip install --python .venv pytest` once.

If you use [Claude Code](https://claude.com/claude-code), the bundled
[`author-problem`](.claude/skills/author-problem/SKILL.md) skill walks through
writing a problem end to end. Its checklist is useful even if you write
problems by hand: edge-case coverage, calibrating large cases to the time limit,
and pinning every rule the judge grades.

There's no admin UI. Problems, account recovery (`verify-email`, `disable-2fa`)
and the rest go through `python -m app.cli`, so shell access to the API
container is the only trust boundary.

## Deploying

**Docker Compose** is the production shape: the built frontend and the API
behind Caddy, secure cookies, and one origin.

```bash
scripts/prod-up.sh                                   # → http://localhost:8080
SITE_ADDRESS=judge.example.com WEB_PORT=80 scripts/prod-up.sh   # automatic HTTPS
```

The script builds the judge images and generates a JWT secret and a TOTP
encryption key once, into gitignored files. For real account email, set
`EMAIL_BACKEND=smtp` and `SMTP_*`. Otherwise verification links are printed to
the accounts-worker log. The judge worker mounts the host Docker socket to start
sandboxes; [DESIGN.md §5.5](DESIGN.md) explains that trade-off.

**Kubernetes:** `scripts/k8s-up.sh [--keda]` stands up a local kind cluster with
the Helm chart in [`deploy/helm/shikomi`](deploy/helm/shikomi). Judging runs as
one locked-down Pod per submission through the Kubernetes API, with no Docker
socket. `--keda` autoscales the worker on queue depth, down to zero.

**Before exposing an instance to untrusted users,** add kernel-level sandbox
isolation (gVisor or Kata) and a NetworkPolicy-enforcing CNI.
[DESIGN.md §5.8](DESIGN.md) explains why.

## Development

```bash
scripts/ci-local.sh          # lint + harness + backend + frontend: what CI's fast jobs run
scripts/ci-local.sh full     # ...plus sandbox, end-to-end and Playwright
```

Or run each suite on its own:

```bash
cd backend && uv run pytest                  # API, services, worker (Postgres; 80% coverage gate)
.venv/bin/pytest -m "not docker"             # judge harness protocol + starter solutions
.venv/bin/pytest -m docker                   # sandbox isolation (needs the judge images)
cd frontend && pnpm test && pnpm e2e         # Vitest + Playwright
```

[AGENTS.md](AGENTS.md) has the full command reference, the test-database setup,
and the gotchas worth knowing before you change anything.

## Repository layout

```
backend/     FastAPI app (app/), judge worker (worker/), Alembic migrations, tests
judge/       sandbox harnesses (Python, JS, SQL), their Dockerfiles, harness tests
frontend/    React + Vite + Monaco workspace, Vitest + Playwright tests
seed/        the five starter problems
deploy/      Helm chart
scripts/     dev/prod/k8s bring-up, local CI, problem-file formatter
docs/        architecture diagrams, generated schema reference, ADRs
```

**Stack:**

| Area | Tools |
| --- | --- |
| Backend | Python 3.12, FastAPI, async SQLAlchemy 2.0, PostgreSQL 16, Alembic |
| Queue | Redis and arq |
| Frontend | React, TypeScript, Vite, TanStack Query, Monaco |
| Tooling | uv, ruff, pnpm, oxlint |

## Documentation

- [DESIGN.md](DESIGN.md) covers the architecture, the data model, the judge
  protocol, and the reasoning behind each decision.
- [AGENTS.md](AGENTS.md) covers how to work in the repo: commands, conventions,
  gotchas, and the open roadmap.
- [CONTRIBUTING.md](CONTRIBUTING.md) covers the PR process and commit
  conventions.
- [CHANGELOG.md](CHANGELOG.md) records releases.

## License

[MIT](LICENSE). The starter problems are original works under the same license.
