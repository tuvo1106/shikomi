# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Rust judge.** Problems can set `"language": "rust"` (function mode). Submissions
  are compiled with `rustc` inside the sandbox. Each test case runs in its own
  process, so a panic, stack overflow, allocation failure or `process::exit` fails
  only that case. Compile errors come back as rustc's own diagnostics, and integer
  overflow panics instead of silently wrapping. Build the image with
  `docker build -f judge/Dockerfile.rust -t shikomi-judge-rust:latest judge/`.
  Existing deployments need `alembic upgrade head` (it widens the `language` check
  constraint) and a `JUDGE_IMAGE_RUST` setting if they don't use the default tag.
- A per-case `memory_limit_exceeded` verdict (reported by the Rust judge).
- *Merge Booking Windows*, a Rust starter problem, shows the format for Rust problems.
- Three more starter problems, so every judge path has an example: *Calm Stretch*
  (Python function), *Curry Without Crosstalk* (JavaScript) and *Monthly Top Spender*
  (SQL).
- `python -m worker.judge_local --language <python|js|rust|mysql>` judges a payload in
  that language's sandbox (its image, tmpfs size and `exec` flag).
- **Problems in several languages.** A problem can be offered in any number of
  languages, with one shared statement and one shared set of test cases. The workspace
  gets a language switcher (shown only when there's more than one), keeps a separate
  draft per language, and shows a short language-specific note under the statement.
  Solutions show code in the editor's language, and label an approach that exists in
  only some languages ("Rust only"). The Submissions tab and the verdict show each
  submission's language, and "Beats X%" compares only against accepted submissions in
  the same language. *Merge Booking Windows* is now offered in Python, JavaScript and
  Rust. Problem files list `languages` and give solution `code` per language; the
  single-language form still loads unchanged. `POST /submissions` and `/run` take an
  optional `language` (default: the problem's first). Existing deployments need
  `alembic upgrade head`, which moves each problem's language fields into the new
  `problem_languages` table and backfills every submission's language.

### Fixed

- The Results pane no longer jitters when you Run or Submit. It used to flash its empty
  prompt and re-enable the buttons for a moment before showing "Judging…".
- Python problems using `float_tolerance` now accept an infinite answer that matches an
  infinite expected value (it was graded wrong, because `inf - inf` is NaN).
- The account menu (and its Sign out) no longer opens behind the code editor in the
  workspace.

## [0.1.0] — 2026-09-24

Initial open-source release.

- **Judge:** Python, JavaScript and SQL (MariaDB) sandboxes; function, class-replay
  (`operations`) and SQL problem kinds; codecs for linked lists, trees, cyclic and
  random-pointer lists, graphs and iterators; exact / unordered / float-tolerance /
  any-of comparison; per-problem time and memory limits.
- **Bring your own problems:** problems are JSON files loaded with
  `python -m app.cli seed --dir <path>`, all-or-nothing: every file is validated
  (schema, at least one test case and one sample, the judge-time budget) before any is
  written; `python -m app.cli validate --dir <path>` runs the same checks with no database; `SEED_DIR=<path> pytest
  judge/tests/test_seed_solutions.py` checks every reference solution against the
  real harness; `PROBLEMS_DIR` points the Compose and kind stacks at your own
  directory. Five original starter problems ship as examples.
- **Workspace:** Monaco editor, Run/Submit, per-case verdict detail, submission
  history, runtime distribution, editorial solutions, sample-case diagrams.
- **Accounts:** email verification, rate limiting and lockout, password policy with
  breached-password screening, anti-enumeration, optional TOTP two-factor with
  recovery codes, JWT key rotation, audit log.
- **Deployment:** hot-reload dev stack, production-shaped Docker Compose behind
  Caddy, and a Helm chart with a per-submission sandbox Pod and KEDA
  scale-to-zero autoscaling.

[Unreleased]: https://github.com/tuvo1106/shikomi/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/tuvo1106/shikomi/releases/tag/v0.1.0
