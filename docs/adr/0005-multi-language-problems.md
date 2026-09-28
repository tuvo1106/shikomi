# ADR-0005: One problem, several languages

- **Status:** Accepted: implemented in `backend/app/models/problem_language.py`, migration `d41f7a2b9c06`, and the workspace's language switcher
- **Date:** 2026-09-27

## Context

With four judges (Python, JavaScript, Rust, SQL; DESIGN.md §13), a problem still had exactly one
`problems.language`, one `starter_code`, one signature and one reference implementation per
solution. Offering the same problem in Python *and* Rust meant authoring it twice, as two
problems with separate solved status, history and editorials.

Most of a function-mode problem is already language-neutral: the test cases are JSON values,
compared as values, and every harness speaks the same protocol. What actually varies by
language is small:

- the **signature**: `function_name` (`merge_bookings` vs `mergeBookings`), the param display
  types (`list[int]`, `number[]`, `Vec<i64>`), and for Python the node codecs;
- the **starter code**;
- the occasional **statement line** that only makes sense in one language ("times don't fit in
  an `i32`"), and the **reference code** of each solution.

A clickable mockup (Merge Booking Windows with a Python ↔ Rust switcher) showed the workspace
UX holding up, and surfaced the questions this ADR settles: where drafts live, how the
statement handles language-specific lines, and whether runtime stats can be shared. The goal
was N languages per problem, not two.

## Decision

1. **A `problem_languages` table**, one row per language a problem is offered in:
   `language`, `ordinal` (0 is the default), `starter_code`, `function_name`, `class_name`,
   `params`, `return_type`, `note_md`. Unique per `(problem, language)` and `(problem, ordinal)`.
   Those columns leave `problems`, which keeps only what is shared: statement, `kind`,
   `comparison`, limits, tags, and (via `test_cases`) the cases.
2. **Solution code per language** in `solution_codes` (`solution_id`, `language`, `code`). A
   solution's title, prose and complexity stay shared, since the idea is the same. A solution
   may cover some languages only, but every language must be covered by *some* solution, so
   the seed-solution tests prove each language's signature against the shared cases.
3. **`submissions.language`**, required. The API fills it in when a client omits it on a
   one-language problem, and otherwise answers 400 `LANGUAGE_REQUIRED` (a review found that
   defaulting to the first language judged an old client's Rust as Python once a problem's
   default changed). It rejects a language the problem doesn't offer (400
   `UNSUPPORTED_LANGUAGE`). The worker judges with the matching variant, and a variant that has
   disappeared since (a re-seed) is a `judge_error`, never a silent fallback.
4. **Stats are per language.** The runtime percentile and histogram filter on the
   submission's language, because a 1 ms Rust run and a 40 ms Python run of the same algorithm
   aren't comparable. Solved status counts an accepted submission in any language.
5. **Authoring.** A problem file lists `languages`, and a solution's `code` becomes a
   `{language: code}` map. The old top-level single-language fields still load: `ProblemIn`
   lifts them into a one-element `languages` list and a string `code` into a map. The
   kind/language rules (function-mode-only languages, `sql` ⇔ `mysql`, `custom_validator` ⇒
   Python only, the memory floor, the judge budget) are checked **per variant**. Every variant
   must declare the same number of params, since cases are positional.
6. **Workspace.** A segmented switcher replaces the language label when there's more than one
   language. Drafts are stored per slug and language (`code:<slug>:<language>`), so switching
   never loses work; the old `code:<slug>` draft migrates to the default language. The
   selected language's `note_md` renders under the shared statement, and Solutions,
   Submissions and the verdict all show code or results in their own language.

## Alternatives considered

| Option | Why not |
|---|---|
| One problem per language (`pair-sum-py`, `pair-sum-rs`) | No schema change, but solved status, history, editorial prose and the catalog all split across copies of the same problem, and the test cases would be duplicated and drift. |
| A JSONB `languages` column on `problems` | Fewer tables, but no per-language uniqueness, and the worker and the stats queries would reach into JSON. The row count is tiny either way. |
| Keep `language`/`starter_code` on `problems` as the default, add a side table for extras | Two places to look for the same information, and every reader has to merge them. |
| Per-language statements | The statement is most of an author's effort; duplicating it invites drift. A short per-language `note_md` covers the real exceptions. |
| Pooled runtime stats | Would rank languages rather than solutions: every compiled Rust submission would "beat" every Python one. |
| Keep the kind/language `CHECK` constraints | They can't span two tables. The seed CLI is the only writer and runs `ProblemIn` first, so application-level validation is where these rules already had to live. |

## Consequences

- Adding a language to an existing problem is a data change: a `languages` entry, starter
  code, and at least one solution in that language. No migration.
- The database no longer backstops the kind/language rules. A hand-written insert could
  create a JavaScript operations variant that fails on every submission. Acceptable because no API
  writes problems.
- Authors must write statements and constraints in language-neutral terms (`n bookings`, not
  `bookings.len()`), and must return values in each language's natural JSON shape. In Python,
  `(1, 6) != [1, 6]`, so a Python variant returns lists where the JSON has arrays.
- Per-language time or memory limits aren't supported: the limits are shared, so they must be
  calibrated to the slowest language's reference solution (recorded as a follow-up).
- The downgrade migration is lossy only for data the old schema can't express: extra
  languages, submissions in them, and solutions without code in the default language.
