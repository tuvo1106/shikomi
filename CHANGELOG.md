# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] — Unreleased

Initial open-source release.

- **Judge:** Python, JavaScript and SQL (MariaDB) sandboxes; function, class-replay
  (`operations`) and SQL problem kinds; codecs for linked lists, trees, cyclic and
  random-pointer lists, graphs and iterators; exact / unordered / float-tolerance /
  any-of comparison; per-problem time and memory limits.
- **Bring your own problems:** problems are JSON files loaded with
  `python -m app.cli seed --dir <path>`, all-or-nothing: every file is validated
  (schema, at least one test case, the judge-time budget) before any is written; `SEED_DIR=<path> pytest
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
