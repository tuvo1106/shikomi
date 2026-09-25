# CLAUDE.md

Guidance for Claude Code working in this repo. **[AGENTS.md](AGENTS.md) is the
source of truth** for environment, commands, architecture decisions, gotchas, and
the live TODO/roadmap — read it. This file exists to keep one thing always in
context: **documentation is part of every change, not a follow-up.**

## Documentation is part of "done"

This is a learning project — the docs are a deliverable, not overhead. A change
isn't finished until the code **and** its documentation land together:

1. **Docstrings teach the _why_.** Every module gets a docstring framing its
   purpose and mental model; every public function/class explains reasoning
   (trade-offs, invariants, gotchas), with `Args:`/`Returns:`/`Raises:` where
   non-obvious; tricky lines get an inline `# why` comment. `backend/app/security.py`
   is the reference depth. Frontend: the same standard as JSDoc (`/** … */`) on
   modules, exported functions/components, and non-obvious hooks.

2. **Keep the long-form docs in sync — in the same commit as the change:**

   | Doc | Update it when you change… |
   | --- | --- |
   | **[DESIGN.md](DESIGN.md)** | architecture, a data model / API / judge / auth mechanism, the tech stack (new tool → add the why/tradeoff/alternative to §2.1), config vars, or the status & roadmap (§11) |
   | **[README.md](README.md)** | how to run/build/test/use the project |
   | **[AGENTS.md](AGENTS.md)** | conventions, a new gotcha/lesson, or the open TODO list (add follow-ups; when a slice ships, **delete its TODO entry** rather than marking it done) |
   | **[CHANGELOG.md](CHANGELOG.md)** | anything user-visible shipped — this is where completed work is recorded, so AGENTS.md's TODO list stays open work only |
   | **Diagrams (`docs/*.mmd`)** | a flow they depict changes — submit lifecycle, judge runners, auth/token lifecycle, KEDA autoscaling, or deployment topology. Update the `.mmd` **and** its copy embedded in DESIGN.md (they're kept identical) |

   When you finish a slice, do a quick pass: does any statement in these docs now
   read as stale? If so, fix it in the same change. Don't let a doc — prose or
   diagram — describe code that no longer exists (a renamed file, a removed flag,
   a superseded design).

3. **Explain choices, not just facts.** When adding a tool or making a non-obvious
   decision, record _why_, the tradeoff accepted, and the alternative rejected —
   in DESIGN.md §2.1 (tools) or §12 (design decisions), or the AGENTS.md TODO entry.

## Conventions (see AGENTS.md for the full list)

- **This repo is public.** Never commit secrets, credentials, tokens or real user data —
  config comes from environment variables (`.env`, gitignored).
- **Never commit directly to `main`**: branch (`feat/`, `fix/`, `chore/`, `docs/`) and open a PR.
- Commit messages: **no `Co-Authored-By` trailer** (maintainer preference).
- PR descriptions: fill the [template](.github/PULL_REQUEST_TEMPLATE.md) and keep it
  brief — what and why, what was tested, which docs changed.
- `ruff` (backend) / `oxlint` (frontend) are the linters of record; match
  surrounding style.
- Verify behavior end-to-end before claiming done (there's a running app / cluster
  to drive), and report outcomes honestly — failing tests get said so.
