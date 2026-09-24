# Contributing

Contributions are welcome — bug reports, fixes, and features. Shikomi is also a learning
project, so the docs are a deliverable: a change lands with its docstrings and doc updates
(see [CLAUDE.md](CLAUDE.md)).

## Problems

Shikomi is bring-your-own-problem: the repo ships only five original starter problems, and
operators keep their own catalogs outside it. A pull request adding a problem to
`seed/problems/` is only accepted if the problem is **entirely your own work** — statement,
test cases, and solutions — and not adapted from another site, book, or course. A new
starter should also teach something the existing five don't (a judge feature, a `kind`, a
language). See [DESIGN.md §7.1](DESIGN.md) for the file format.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/): `type(scope): subject`, imperative, ≤72 chars.

Types: `feat` `fix` `docs` `refactor` `perf` `test` `build` `ci` `chore` `revert`.

No `Co-Authored-By` trailer (see [AGENTS.md](AGENTS.md)).

## Before opening a PR

- Run `lefthook install` once: it enforces the commit format and runs the CI checks your diff
  touches before each push. `scripts/ci-local.sh full` runs *everything* CI does (Docker + the dev
  stack), so run it before merging. It is not CI: it uses your OS and dev database, so CI is
  still the final word.

- Docs stay in sync with the change — see "Documentation is part of 'done'" in [CLAUDE.md](CLAUDE.md).
- Fill out the [PR template](.github/PULL_REQUEST_TEMPLATE.md).
- A decision that's hard to reverse, non-obvious to the next reader, or reached by rejecting a
  plausible alternative gets an ADR in [docs/adr/](docs/adr/) — see ADR-0001.

## Where things live

- [DESIGN.md](DESIGN.md) — architecture and the reasoning behind it.
- [AGENTS.md](AGENTS.md) — environment, commands, conventions, gotchas, TODO/roadmap.
- [docs/adr/](docs/adr/) — decisions DESIGN.md doesn't cover, or that contradict it.
- [CHANGELOG.md](CHANGELOG.md) — what shipped and why.
