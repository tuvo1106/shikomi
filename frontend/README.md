# Frontend

React + TypeScript + Vite + Monaco. This is the SPA half of the app; the API it
talks to lives in `../backend`.

Start here: the repo [README](../README.md) for running the whole stack
(`scripts/dev-up.sh` brings up Postgres, Redis, the API, the worker, and this),
and [DESIGN.md §6](../DESIGN.md) for the UI architecture and visual spec.

## Commands

Run from this directory. `pnpm dev` expects the API to already be up.

| Command | What |
| --- | --- |
| `pnpm dev` | Vite dev server on :5173, proxying `/api` to the backend |
| `pnpm build` | `tsc -b` then a production build |
| `pnpm lint` | oxlint (the linter of record for this half of the repo) |
| `pnpm test` | Vitest unit/component tests |
| `pnpm e2e` | Playwright, against the full running stack |

**The two test runners are not interchangeable.** Vitest is scoped to `src/`
and Playwright to `e2e/`, deliberately, so the two don't try to collect each
other's files (Vitest's `include` in `vite.config.ts`).

## Layout

- `api/` — typed client and the request/response types shared with the backend
- `pages/` — one file per simple page; a page with parts gets its own folder
  (`workspace/`, `settings/`) rather than a global components bin (see AGENTS.md's
  "keep page components split")
- `components/` — only genuinely shared UI
- `auth/` — session state and route guards
- `lib/`, `test/` — helpers and test setup
