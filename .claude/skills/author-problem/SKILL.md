---
name: author-problem
description: Author an original coding problem as a validated problem JSON file (statement, test cases, reference solutions) that `python -m app.cli seed` can load. Use when the user wants to write a new problem, add test cases or solutions to one, or check that a problem file judges correctly.
---

# Authoring a problem

Produces one `<slug>.json` per problem, matching the schema in DESIGN.md §7.1,
and validated by the real judge harness before it's presented as done. Problems
live in `seed/problems/` (the bundled starters) or in any directory an operator
loads with `python -m app.cli seed --dir <dir>`.

**Write everything yourself.** Shikomi ships no third-party problem content, and
a problem file is only safe to load if its statement, examples and solutions
are original. Don't paraphrase another platform's problem, reuse its examples,
or copy its title. A well-known *technique* (binary search, union-find, a B-tree)
is fine to teach; someone else's *write-up* of it is not.

Default to a **batch of a few problems**, then stop and let the user review.

**Design-pattern lessons and from-scratch data structures** have their own
conventions on top of everything below. Read
[`authored-families.md`](authored-families.md) first. `design-vending-machine`
and `design-b-tree` are the reference problems.

## 0. Check the harness can judge it

The judge compares **return values**. Three problem kinds are supported:

- **`function`** (default). The judge calls one function with JSON-representable
  arguments and compares the JSON result. Node-shaped arguments and returns go
  through a harness codec keyed on the param's `"type"` or the problem's
  `"return_type"`: `ListNode`, `TreeNode` (null-padded level order),
  `CyclicListNode` (`[values, pos]`), `RandomListNode`, `GraphNode`, and the
  `List[ListNode]`/`List[TreeNode]` forms. DESIGN.md §5.3 has each wire encoding.
  `starter_code` declares a same-shaped class (`val`/`next`, `val`/`left`/`right`).
  The harness matches classes by attribute shape, not identity.
- **`operations`** (class replay). The judge instantiates `class_name` and
  replays a sequence of method calls. Constructor args go through the same codecs
  (plus the decode-only `Iterator`). Later method calls take plain JSON.
- **`sql`** (always paired with `"language": "mysql"`). The submission is one raw
  query, run against a database the harness seeds per test case. The engine is
  MariaDB (`docs/adr/0002-sql-judge-engine-mysql-vs-mariadb.md`), so MySQL-8-only
  features are out of scope. The connection runs **one statement**, so a solution
  must not rely on several.

**JavaScript** (`"language": "js"`) is function mode only: no `operations`, no
node codecs. Use it for problems that are genuinely *about* JS (closures, `this`,
higher-order functions). A closure or function value can't cross the JSON
boundary, so implement the real closure, add a **driver** function that exercises
it and returns a JSON-safe result, point `function_name` at the driver, and say
so in `statement_md`. A Promise return is awaited. Give real-timer cases
generous margins (a 50ms debounce window against a 500ms+ `time_limit_ms`).

**In-place mutation isn't observable.** The judge never inspects mutated
arguments. A "modify the array in place" problem returns the resulting array
instead, and the statement says so.

If a problem needs something the harness can't express, **stop and flag it**.
Don't work around it per problem.

## 1. Write the statement

- Terse and example-driven. Worked examples come from the `is_sample: true` test
  cases, which the workspace renders, so don't hand-type examples into the prose.
  For SQL, `statement_md` documents the schema only. The "Show sample data"
  toggle renders the sample case.
- Constraints go in the LaTeX-ish style (`$1 \le \texttt{n} \le 10^4$`), not prose.
  `$…$`/`$$…$$` render in `statement_md` and in solutions' `intuition_md`/`algorithm_md`.
  A `$` inside a code span is left alone.
- **Pin every rule the test cases grade.** Exact compare grades every tie-break,
  clamp, ordering and edge convention. If a reasonable implementer could resolve
  a rule the other way, the statement must say which way.
- **Complexity claims are a contract.** If the statement promises a bound, every
  shipped solution must meet it. The one exception is a deliberately naive
  solution, and then the paragraph must read as a requirement ("`get` **must be**
  $O(1)$"), not as a description.

## 2. Write the metadata

Match an existing file's shape exactly. The five files in `seed/problems/` are
the references.

- `slug`: kebab-case, derived from the title.
- Python `starter_code` uses modern hints (`list[int]`). `params` types are
  `typing`-style display hints (`"List[int]"`), except the codec names above,
  which also drive decoding.
- JS `starter_code` uses `var funcName = function(args) {\n    \n};`, and `params`
  use JS-ish names (`"number[]"`).
- `operations` problems set `"kind": "operations"` and `"class_name"`, omit
  `function_name`/`return_type`, and give `starter_code` a class skeleton.
- `sql` problems omit `function_name`/`class_name`/`params`/`return_type`
  (validation rejects them). `starter_code` is a SQL comment
  (`-- Write your SELECT below\n`).
- `comparison`:
  - `{"mode": "exact"}` by default;
  - `"unordered"` for order-independent results, and the default for SQL unless
    the statement requires an `ORDER BY`;
  - `"float_tolerance"` (+ `"epsilon"`);
  - `"any_of"` when several answers are acceptable.
- `tags` describe the technique the **shipped solutions** use, not the topic.
  Reuse the existing vocabulary before inventing a tag. Regenerate it from the
  problem directory you're adding to:

  ```python
  import json, glob, collections
  c = collections.Counter(t for f in glob.glob('seed/problems/*.json')
                          for t in json.load(open(f)).get('tags', []))
  ```

  Rules:
  - Every `operations` problem gets `design`, plus technique tags for what's
    inside the class.
  - Map jargon onto existing tags: sliding window → `two-pointers`,
    priority queue → `heap`, prefix sum → `array`, bucket sort → `sorting`.
  - `heap` means the solution really pushes/pops a heap. `queue` means FIFO or
    deque use. `union-find` gets its own tag.
  - Drop tags too generic to filter on (`algorithms`, a difficulty restated).
  - A tag that selects only one problem isn't worth adding. Say so explicitly
    when you do add a new one.
- `collections` is an operator-defined grouping (`gang-of-four` in the starters).
  The UI title-cases the slug for its label.

## 3. Write test cases

Aim for about 10 small cases (the worked examples marked `is_sample: true`) plus
**1–2 large hidden cases**. Every case should be able to catch some plausible
wrong solution. Ten variations of the happy path catch nothing. Check each item
below:

- **Boundary sizes**: the smallest legal input, and one near the declared limits.
- **Degenerate values**: all-equal, all-zero, sorted, reverse-sorted.
- **Type extremes**: negatives, zero, values near the stated bounds.
- **Traps for this algorithm**: an off-by-one, an assumed-sorted input, an assumed
  uniqueness, a match at the first vs. last index, intervals that touch vs. overlap.
- **Answer shape**: "no answer", and multiple valid answers. Multiple valid
  answers mean `any_of`.

**Samples are drawn.** For node-typed params and return types, the workspace
draws each `is_sample: true` case as a diagram. Keep samples within the
renderer's envelope: at most 15 nodes and labels of at most 7 characters
(`docs/adr/0003-sample-case-diagrams-hand-rolled-svg.md`). Hidden cases can be
any size.

**`operations` cases**: `input` is `[ops, args]` with `ops[0]` the class name.
`expected` is the parallel result list with `expected[0] = null`. Prefer several
shorter traces, each exercising a different call order or edge condition, over
one long trace.

**`sql` cases**: `input` is a one-element array holding one seed script (DDL plus
`INSERT`s, semicolon-separated). `expected` is a list of rows in the query's column
order. Each case gets its own fresh database, so vary the table state across cases:

- an empty table;
- a join that matches nothing;
- `NULL`s in joined or grouped columns;
- duplicates that should or shouldn't collapse;
- a row an `INNER JOIN` would drop.

Wire types: `NULL` is `null`, dates are ISO strings, and `DECIMAL` values are
strings (`"12.50"`). Check real output via `judge/harness_sql.py`, don't guess.
**Declare every key the statement documents.** A missing `PRIMARY KEY` both
mismatches the statement and can turn an indexed lookup into a nested-loop scan.

**Large hidden cases are calibrated to the slowest shipped solution**, not to the
largest input the constraints allow. Measure it:

```python
import time
t = time.perf_counter(); slowest_solution(*large_input)
print((time.perf_counter() - t) * 1000, "ms")
```

Stay well under half of `time_limit_ms` (default 2000), since CI runners are
slower than a laptop. Shape the input to stress the slow path (put the match
last, so nothing short-circuits). If a case is too slow, shrink the **data**,
not the time limit. A looser limit weakens the judge for real submissions.

**Recursion depth.** The harness keeps Python's default recursion limit (about
1000 frames). If the natural solution recurses and its depth scales with the
input, keep any depth-sensitive case at or below about 750. A case can still
stress time complexity at full scale if its shape keeps depth low (a bushy tree,
not a chain). Test the algorithm in a throwaway subprocess with
`sys.setrecursionlimit` raised. If it genuinely segfaults at the realistic worst
case, ship an iterative solution. If not, keep the recursive one as the primary
solution and optionally add an iterative alternative.

**Check what a large case exercises.** Look at the distribution of results it
expects. A stress case whose every query returns the same value tests nothing.
Call collection-returning observables (`serialize()`, `level_order()`) only a
handful of times, on modest inputs. Calling them repeatedly is how problem files
bloat to megabytes. Aim for roughly 100–300KB at most per file.

## 4. Write solutions

Write 1–2 approaches, usually brute force first and then optimal. Each needs
`intuition_md`, `algorithm_md`, `code`, `time_complexity`, `space_complexity`, and
their `*_reason` fields, written with real prose depth.

- Ship the solution you'd want a learner to copy. A technically-passing answer
  that dodges the problem's actual difficulty doesn't qualify.
- When you replace one algorithm with another, stress-test the new one against a
  slower, obviously-correct reference over hundreds of random inputs.
- `time_complexity`/`space_complexity` render as **KaTeX**. Keep them pure
  notation (`"O(n)"`, `"O(\\log n)"`), and wrap any words in `\text{}`. Put
  nuance in the `*_reason` fields.

## 5. Validate: don't just claim it works

1. **Schema**: every file is validated against `ProblemFile`
   (`backend/app/schemas/problem.py`) before anything is written, and one bad
   file aborts the whole load. Run it against a dev database:
   `cd backend && uv run python -m app.cli seed --dir <dir>`. Warnings about
   unknown keys usually mean a typo.
2. **Real harness**: from the repo root, run
   `SEED_DIR=<dir> .venv/bin/pytest judge/tests/test_seed_solutions.py -k <slug> -v --durations=0`.
   This runs every shipped solution against every case and shows per-case timing.
   SQL problems need the built `shikomi-judge-sql` image and are docker-marked.
3. **Judge budget**: `ProblemFile` rejects a problem whose worst-case run
   (`cases × time_limit_ms` plus slack) wouldn't finish inside the worker's job
   timeout (`app/judge_budget.py`). The error names the maximum case count.
   Drop cases rather than raising the timeout.

## 6. Format the file

Don't write files with `json.dumps(obj, indent=2)`. It puts every element of every
nested array on its own line. Use the repo's formatter:

```python
import sys, pathlib
sys.path.insert(0, "scripts")
from format_seed_problem import dump_problem
pathlib.Path(f"seed/problems/{slug}.json").write_text(dump_problem(problem))
```

To reformat an existing file in place, run
`python scripts/format_seed_problem.py <file>`. It is idempotent. For small edits
to an existing file, make a surgical text edit rather than round-tripping the
whole file.

## 7. Hand it back for review

Don't commit problem files until the user has reviewed them. Report each slug,
title, difficulty, and test/solution counts, then wait.
