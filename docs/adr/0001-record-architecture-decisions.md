# ADR-0001: Record architecture decisions

- **Status:** Accepted

## Context

[DESIGN.md](../../DESIGN.md) covers the architecture as designed, and [AGENTS.md](../../AGENTS.md)
covers conventions and known gotchas. Neither is the right place for a decision made
mid-implementation — a library choice, a workaround, a tradeoff — that isn't part of the
up-front design and isn't a recurring convention either. Those decisions currently just
happen, in a commit, and the reasoning behind them is unfindable a month later.

## Decision

Keep an ADR log in `docs/adr/`, numbered sequentially, using `adr-template.md`.

- **DESIGN.md** — the architecture and reasoning decided up front.
- **AGENTS.md** — conventions, gotchas, the TODO/roadmap.
- **ADRs** — decisions made *during* implementation that the above don't cover, or that
  contradict DESIGN.md (which then also gets updated, with a pointer in both places).

Write one when a decision is hard to reverse, non-obvious to the next reader (including future
you), or was reached by rejecting a plausible alternative. Routine choices don't need one.
ADRs are immutable once accepted — to change a decision, write a new ADR and mark the old one
`Superseded by ADR-XXXX`.

## Alternatives considered

| Option | Why not |
|---|---|
| Put it in DESIGN.md | DESIGN.md is edited in dedicated PRs and reads as a single coherent spec; an implementation-decision log would erode that. |
| Put it in AGENTS.md | AGENTS.md is conventions and gotchas — flatter and more skimmable than a decision record needs to be. |
| Nothing, rely on git history | Shows *what* changed, never *what else was considered and why it lost*. |

## Consequences

A decision worth writing down gets reviewed in the PR that makes it, while it's still cheap to
revisit. Small tax on non-obvious changes; the alternative is losing the reasoning entirely.
