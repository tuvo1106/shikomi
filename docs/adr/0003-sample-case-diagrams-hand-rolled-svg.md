# ADR-0003: Rendering `TreeNode`/`ListNode`/`GraphNode` sample cases — react-d3-tree, Mermaid, or hand-rolled SVG

- **Status:** Accepted — spike run 2026-09-13, all three approaches prototyped against the problem set of the time
- **Date:** 2026-09-13

## Context

The workspace's Description panel shows every sample case's input and expected output as the judge's **wire format** — the null-padded level-order array, the `[values, pos]` cyclic pair, the `[[val, random_index], …]` list, the 1-based adjacency list. Those encodings exist so `judge/harness.py` can rebuild a real object graph; they are not a shape a reader can see. `root = [8,4,12,2,6,10,14]` says nothing about which node is whose child until you decode it in your head, and `head = [[4,null],[9,0],[2,4],[6,2],[3,0]]` is worse.

There was a long-standing TODO to compare `react-d3-tree` against Mermaid before committing to either, noting that a hand-rolled flexbox/SVG component might beat both for linked lists. This ADR records that comparison and the decision.

### Evidence — verified in-session

All numbers below come from the problem set in use when the decision was made (529 problems) and from real production builds against one baseline commit, not from estimates.

**What the problem set actually contained.** Decoding every sample case of every problem that declares a node-typed param or return type:

| | |
|---|---|
| Problems with a node-typed shape | **85** |
| Diagrams those problems' sample cases produce | **358** |
| Largest single diagram | **15 nodes** |
| Longest node label | **7 characters** (`9999999`) |
| Edges by kind | `left` 437, `right` 507, `next` 234, `random` 14, `neighbor` 8, cycle-back `next` 4 |

Two facts fall out of that table and drive everything else:

1. **Half the shapes aren't trees.** 26 edges across the problem set (14 `random`, 8 `neighbor`, 4 cycle-back `next`) cannot exist in a nested parent→children JSON model, because they point sideways or backwards. They belong to `CyclicListNode`, `RandomListNode`, and `GraphNode` problems.
2. **226 lone-child parents, across 118 of the 358 diagrams**, have exactly one child. A `children: [x]` array cannot say whether `x` is the left child or the right child — and for binary-tree problems that distinction *is* the problem.

**The structures are small.** 15 nodes and 7 characters is the whole envelope. Nothing here needs panning, zooming, collapsing, virtualized rendering, or a force simulation. That is the single most important sizing fact in this ADR: it is what makes the cheapest option viable rather than a compromise.

**Bundle cost, measured.** Each option was built into the app at the baseline commit and compared against that commit's own build (878.00 kB raw / 262.40 kB gzip, one chunk):

| | main chunk | Δ main chunk | total JS shipped | Δ total | chunks |
|---|---|---|---|---|---|
| Baseline (`61a3f4d`) | 878.00 kB / 262.40 kB gz | — | 878.00 kB | — | 1 |
| **Hand-rolled SVG (shipped)** | 883.81 kB / 264.66 kB gz | **+5.81 kB / +2.26 kB gz** | 883.81 kB | **+5.81 kB** | 1 |
| react-d3-tree | 968.09 kB / 290.96 kB gz | +90.09 kB / +28.56 kB gz | 968.09 kB | +90.09 kB | 1 |
| Mermaid | 1,012.82 kB / 302.03 kB gz | +134.82 kB / +39.63 kB gz | 5,758.82 kB / 1,683.46 kB gz | **+4.88 MB / +1.42 MB gz** | **92** |

Context for those deltas: the main chunk **already** trips Vite's 500 kB warning at baseline. Mermaid's +134.82 kB is only its eager entry cost; the other ~4.7 MB arrives as 91 lazily-fetched diagram-renderer chunks (sequence, gantt, C4, git-graph, …), essentially all of which this app will never ask for. A dynamic `import('mermaid')` moves the eager cost off the main chunk but does not change the ~4.9 MB of bytes added to the deployed artifact.

**Fidelity, measured.** All three were prototyped against the same 12 fixtures covering `TreeNode`, `ListNode`, `List[TreeNode]`, `List[ListNode]`, `CyclicListNode`, `RandomListNode`, and `GraphNode`:

- **react-d3-tree** takes a nested `{name, children}` object, so the 26 non-tree edges have nowhere to go — it renders them by **silently dropping them**. A 4-node cyclic graph comes out as a straight `1 → 2 → 3 → 4` chain, which is not a degraded diagram but an actively wrong one. It also cannot distinguish a lone left child from a lone right child without inserting a visible placeholder node for the missing side.
- **Mermaid** is correct on all seven shapes — arbitrary edges are its native model — at the bundle cost above, plus an async `render()` per diagram (358 of them across the problem set, up to 6 on a single page).
- **Hand-rolled SVG** is correct on all seven shapes, in ~5.8 kB.

## Options

| | **A: react-d3-tree** | **B: Mermaid** | **C: Hand-rolled SVG** |
|---|---|---|---|
| Renders trees | Yes | Yes | Yes |
| Distinguishes lone left vs. lone right child | No (needs a placeholder node) | Yes | Yes |
| Renders cycles / random pointers / graphs | **No — drops the edges** | Yes | Yes |
| Bundle Δ (total shipped JS) | +90.09 kB | +4.88 MB across 92 chunks | **+5.81 kB** |
| Bundle Δ (eager, main chunk) | +90.09 kB | +134.82 kB | **+5.81 kB** |
| Render model | React components | async `render()` → SVG string → `innerHTML` | React components |
| Theme (light/dark) | Config object per theme | Theme config / CSS var overrides | **Free** — Tailwind `fill-zinc-*` classes flip with the existing ramp inversion |
| Testable in jsdom | Partially (measures the DOM) | Poorly (async, string output) | **Fully** — pure layout math, asserted on `<text>` coordinates |
| Code we own | ~40 lines of adapter | ~60 lines of adapter + generator | **442 lines** (decoders + renderer + wrapper) |

## Decision

**Option C — a hand-rolled SVG renderer.** Three files under `frontend/src/pages/workspace/`:

- `nodeGraph.ts` — decoders mirroring `judge/harness.py`'s wire codecs, producing one generic `VizGraph { shape, nodes, edges, root }` for every node type. Deliberately a **graph**, not a nested tree, because option A's failure above is exactly what a nested model buys you.
- `NodeDiagram.tsx` — slot-based tidy tree layout, left-to-right rows for lists, a ring for graphs; quadratic Bézier arcs below the row for cycles and (dashed) above it for random pointers.
- `SampleDiagrams.tsx` — the problem-aware wrapper the Description panel renders beneath each Example's Input/Output lines.

Rationale: A is disqualified on correctness, not cost — it renders cyclic graphs wrong. That leaves B versus C, and B is a ~4.9 MB dependency buying nothing that ~440 lines of owned code doesn't already do correctly, against a main chunk that is already over budget. The usual argument for the dependency — that hand-rolled layout is where you drown — doesn't apply at 15 nodes and 7 characters: there is no zoom, no pan, no collapse, no overlap resolution, no text measurement. The layout is arithmetic.

### Kill-criteria (pre-committed before building C)

C was to be abandoned for B if any held. None did:

| Criterion | Result |
|---|---|
| Any of the seven shapes can't be laid out legibly | All seven render correctly; verified in the running app against a binary tree, a cyclic list, a cyclic graph, a random-pointer list, and a list of lists |
| Needs text measurement or a layout pass in the DOM | No — labels are ≤7 chars, so a fixed per-character width sizes the pills |
| Grows past a bounded, reviewable amount of owned layout code | 442 lines across three files, 301 of them non-comment — the rest is the docstrings this repo requires |
| Needs its own theme plumbing | None — `fill-zinc-800` / `stroke-zinc-600` flip with `index.css`'s existing `:root.light` ramp inversion; verified in both themes |

### Two data-model traps found while building it

Both are guarded in code, commented, and covered by tests — recorded here because neither is visible from the type signatures:

1. **Operations-kind problems can declare node params that don't address their input.** An operations problem with a `TreeNode`/`ListNode` constructor param (a tree iterator, say) has test-case `input` shaped `[ops, args]` — so a param index points at the operations list, not at a tree. `nodeParams()` returns `[]` for any non-`function` kind; without that gate such a problem draws garbage.
2. **The output codec isn't the mirror of the input codec for two types.** `_encode_cyclic_node` returns a bare *index* (node identity, via an `_idx` stamp), not a `[values, pos]` pair, and `List[ListNode]` has no output codec at all. `decodeExpected()` refuses both: the expected answer there is an identity, not a shape.

## Alternatives considered

| Option | Why not |
|---|---|
| react-d3-tree | Its nested `{name, children}` model cannot represent the 26 sideways/backward edges and silently drops them — a cyclic graph renders as a straight chain. Also can't distinguish a lone left from a lone right child, which is the substance of many of the 85 node-shaped problems. Correctness, not size, is the disqualifier. |
| Mermaid | Correct on every shape, but +4.88 MB / +1.42 MB gzip across 92 chunks (+134.82 kB eager) on a main chunk already past Vite's 500 kB warning, plus an async render per diagram and jsdom-hostile string output. Reconsider only if the app later needs *general* diagramming (sequence/state/ER) for its own content, at which point the marginal cost of using it here too is zero. |
| Mermaid behind a dynamic `import()` | Moves the eager 134.82 kB off the main chunk but still ships ~4.9 MB into the deployed artifact, and adds a loading state to a panel that should render synchronously with the problem statement. |
| A general graph library (d3-force, cytoscape, elk) | Solves a layout problem these diagrams don't have. At ≤15 nodes there is nothing to simulate or route around; a force layout would also render the *same* tree differently on every mount, which is worse for a reference diagram than a deterministic one. |
| Rendering with flexbox/HTML instead of SVG | Fine for the 234 `next` edges (a horizontal chain), but there's no honest way to draw the 26 arcs and 944 tree edges without absolute positioning and pseudo-element hacks. SVG is the right primitive once *any* edge is diagonal or curved. |
| Server-side rendering of the diagram | The wire format is already on the wire; decoding it client-side costs nothing and keeps `judge/harness.py`'s codecs as the single source of truth for what an encoding means. |

## Consequences

**Easier.** The Description panel now shows shapes instead of encodings, for 85 problems and 358 sample diagrams, with no new dependency and no change to the API, the data model, or the seed format. The layout is pure arithmetic over a plain data structure, so it's unit-testable in jsdom by asserting `<text>` coordinates — including the case that motivates the whole thing (`[1,2]` and `[1,null,2]` must not render identically).

**Harder.** Layout quality is now ours to maintain. The current renderer assumes the sizing envelope measured above: ≤7-character labels and structures small enough that the panel's width is never the binding constraint. A problem seeded with, say, 40 nodes or a long string label would render too wide to read, and the fix would be ours to write. That is a deliberate, bounded bet — and `seed/` is authored in this repo, so the envelope is enforceable at authoring time rather than hoped for.

**Where a new node type lands.** `judge/harness.py` gains a codec; `nodeGraph.ts` gains the mirror decoder and an `EdgeKind`; `NodeDiagram.tsx` gains a case only if the new shape doesn't fit tree / row / ring. The three-way split exists so the first two steps don't touch layout code.
