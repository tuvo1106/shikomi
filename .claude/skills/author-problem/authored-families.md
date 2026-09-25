# Authored problem families: conventions

Two kinds of problem need conventions beyond the basics in `SKILL.md`:
**design-pattern lessons** (the Gang of Four) and **from-scratch data
structures** (AVL, B-tree, segment tree, …). Both run into the same underlying
limit: the judge grades behaviour, not structure (DESIGN.md §12). Each family
works around that limit in its own way.

The starters include three pattern lessons (`design-vending-machine` for State,
`design-undo-redo-editor` for Command, `design-price-feed` for Observer) and two
structures (`design-b-tree`, `design-lazy-segment-tree`). Use them as templates.

---

## Gang of Four design-pattern problems

**Tag vs. collection.** They mean different things:

- The `gang-of-four` **collection** says the problem is a lesson *about* the
  pattern. It opens with the banner below and ships the two-solution contrast.
- A pattern **tag** (`state`, `command`, `observer`, …) says the problem *is an
  instance of* the pattern.

An algorithm problem that happens to use a pattern gets the tag but not the
collection or the banner. The two filters compose
(`?collection=gang-of-four&tag=state`). There is no `design-patterns` tag.
Pattern names would swamp the flat Tag dropdown, and `collections` already exists
for curated groupings.

**How many problems per pattern.** A pattern tag is worth having only if it
selects more than one problem. A second problem for a pattern has to teach a
*different half* of the pattern, not restage the first one's contrast. Some
patterns, like Singleton, Facade, Bridge, Template Method, Flyweight and Abstract
Factory, rarely support a second problem at all. Apply the tag when each problem
is written. A tag applied to only some of the problems it belongs on hides the
rest behind the filter.

**What the judge can grade.** It compares return values, so it can't see
structure: `design-vending-machine`'s flag-based and State-object solutions both
pass. The two groups of patterns behave differently:

- **Patterns with a behavioural signature** that an op-replay can observe make
  strong problems. These are State, Command, Chain of Responsibility, Strategy,
  Iterator, Memento, Mediator, Visitor, Proxy and Factory Method. Even Proxy and
  Factory Method admit a same-output, different-complexity contrast.
- **Purely organisational patterns** leave no trace in the output. These are
  Facade, Bridge, Template Method, Abstract Factory, Singleton and Flyweight.
  For these, the model solutions do the teaching, not the verdict.

**Per-problem conventions:**

- **Statement:** open `statement_md` with a one-line italic banner:
  `*Gang of Four pattern: **State** (behavioral). …*`. Only collection members
  get the banner.
- **Solutions:** ship two, in reading order. First the straightforward version
  (conditionals, a flag, a dict of lambdas), then the pattern proper. Both are
  usually $O(1)$, so the `*_reason` fields carry the real trade-off (structure,
  extension cost). Say plainly where the pattern *doesn't* pay for itself.
- **The complexity paragraph is a contract.** Check the bound against *every*
  solution, not just the pattern one. The naive solution may miss the bound (that
  is the lesson), but then the paragraph must read as a requirement.
  A bound that *both* solutions miss is simply wrong.
- **Pin every rule the cases grade.** A tie-break, clamp or clock that a
  reasonable implementer could resolve the other way belongs in the statement.
  Say it even when the pattern contrast doesn't depend on it. Those are exactly
  the rules that get left unsaid.

**Vary the mechanism of the contrast.** Five lessons that each reduce to "branch
chain vs. polymorphic dispatch" teach one thing five times. Aim for different
shapes. Some examples:

- eager flatten vs. a lazy cursor;
- entry actions written once vs. recomputed on every incoming edge;
- one record shape branched on at three consumer sites vs. one class answering
  three questions;
- pairwise negotiation vs. one arbiter with a global view.

**Give the pattern more than one question to answer.** A contrast that looks thin
at one consumer is usually obvious at the second, and that second consumer is the
honest reason the pattern pays. `design-undo-redo-editor` has its commands apply,
revert, and name themselves for `history()`.

---

## From-scratch data structures

These ask the learner to *build* the structure, not use one.

**Structure is spec, not introspection.** The judge can't inspect how a
submission is organised, so the shape is promoted into the **public API**. Every
such problem exposes at least one shape-revealing operation:

- `height()`;
- `level_order()` (null-padded, the `TreeNode` convention);
- `serialize()` (a B-tree's level-order list of per-node key arrays);
- `leaves()` (a B+ tree's leaf chain).

A `bisect`-backed sorted list is a fine container, but it can't answer any of
these.

The cost is that **every tie-breaking rule must be pinned in the statement**,
because exact compare now grades the shape:

- which node replaces a deleted two-child node (successor vs. predecessor);
- single vs. double rotation when the taller child's balance factor is 0;
- which side an odd split favours;
- whether a split's separator is *copied* up (B+ leaf) or *pushed* up (B);
- proactive top-down splitting vs. reactive bottom-up splitting;
- bottom-up vs. top-down splaying (over random sequences these almost never
  agree);
- the exact hash family for a probabilistic structure.

An underspecified rule fails a correct implementation for no behavioural reason.
Before shipping, cross-check the two shipped implementations against each other
over hundreds of randomised op sequences, comparing the observable shape after
every op. A divergence means the statement is underspecified, not that the code
has a bug.

**Two solutions, differing in mechanism.** Once the shape is spec'd, a naive
stand-in can't pass. Both solutions are real implementations that differ in
*how* they work: recursive with the subtree root returned vs. iterative with
parent pointers, or an explicit path stack vs. parent pointers. Keep a
naive→optimal ordering only where a real complexity contrast survives. For
example, a range query that re-descends per successor ($O(k \log n)$) vs. one
that scans a leaf chain ($O(\log n + k)$). Size the large case so **both**
solutions still pass.

**Test-case sizing.**

- Large cases drive **scalar-returning** ops (`height`, `search`, `range_sum`).
- Call `level_order()`/`serialize()` only a handful of times, on modest
  structures.
- Recursion depth is $O(\log n)$ for balanced structures. Splay trees are the
  exception: sequential inserts build an $O(n)$-deep path, so splay solutions
  are iterative, and depth-sensitive cases stay at or below about 750.
- Adversarial insert orders (sorted, reverse-sorted, sawtooth) are the point of
  these structures. Use them in the small cases and in the large case.

**Tags, not a collection.** Unlike the pattern lessons, these get no collection
and no banner. The first paragraph names the structure and its invariant.
Discovery is by family tag, used alongside `design` and the structural tag
(`tree`, `array`, `hash-table`):

- `balanced-tree`
- `b-tree`
- `segment-tree`
- `probabilistic`
- `spatial`

A structure that fits none of these gets no family tag rather than a new
one-problem tag.
