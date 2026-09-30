# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Known-wrong solutions in problem files.** A problem file can list `wrong_solutions`,
  code the judge must reject. The seed-solution tests run each one and fail unless some
  case is judged `wrong_answer`, which proves a custom validator rejects a plausible
  mistake instead of accepting everything. They're never loaded or shown, and seeding
  warns about a custom-validator problem with none in some language.
- **`Json` in Rust.** A Rust submission can take or return the judge's own `Json` value
  (`Json::Int`, `Json::Arr`, `Json::Str`, `Json::Obj`, ...) wherever no single Rust type
  fits: a nested list that mixes integers and lists, an object tree, or a method that
  returns a list in one case and a string in another. Every submission can name `Json`
  without a `use`, and a submission's own `Json` type still takes precedence. Rebuild the
  Rust judge image to pick this up.
- **Rust custom validators.** A `custom_validator` problem can be offered in Rust, with a
  Rust validator, `fn validate(actual: &Json, expected: &Json, args: &Json, probe_results:
  &[Json]) -> bool`, next to the Python one in `validator_code`. The judge compiles it
  into its own program and runs it once per case, after the submission's code has
  finished. The submission can't replace it, because it runs from a sealed in-memory
  copy rather than a file, and it can't read the validator's source. A validator that
  doesn't compile or panics is a `judge_error`, and neither its compiler output nor its
  panic message (which could quote the expected answer) is shown to the user. Rebuild the
  Rust judge image to pick this up.
- **Probes in Rust**, so an operations problem with probes (a round trip, a distribution)
  can be offered in Rust too. A panic in a probe call is labelled as a call the judge
  added. Rebuild the Rust judge image to pick this up.
- **Random numbers for Rust solutions:** `use shikomi_prelude::Rng;` gives `Rng::new()`,
  `Rng::seeded(seed)`, `gen_range(0..n)` (exactly uniform, over any integer type up to 64
  bits, or `f64`), `gen_f64`, `gen_bool`, `shuffle` and `choose`, since the sandbox has no `rand`
  crate.
- **Probes** for operations problems with a `custom_validator`: a test case can list
  extra calls the judge makes on the instance after the replay, such as
  `{"op": "pickIndex", "repeat": 4000}` or a round trip, `{"op": "decode", "args": [null],
  "refs": {"0": 1}}`. Their results reach the validator as `probe_results`, in
  `def validate(actual, expected, args, probe_results)`. A validator in that form runs in
  the judge's trusted parent process, so an operations problem no longer has to expose
  `expected` to the submission or trust a verdict the submission could forge, which the
  older `instance` form still does (ADR-0007). A probe call that raises is the
  submission's `runtime_error`, labelled as a call the judge added. Existing deployments
  need `alembic upgrade head` (it adds `test_cases.probes`) and a rebuilt Python judge
  image; the old image reports every probe-form problem as a `judge_error`.
- A `custom_validator` problem's `validator_code` can be a map of language to validator,
  `{"python": "..."}`, so each language a problem is offered in can have its own
  (ADR-0007). A single string still works and means Python's; it's stored as the map
  when seeded. Existing deployments need `alembic upgrade head`, which converts problems
  seeded earlier (its downgrade converts them back).
- An operations problem's `Iterator` constructor argument now works in Rust: the
  starter takes an `IntIter` (an `Iterator<Item = i32>`), e.g. `fn new(nums: IntIter)`.
  Rebuild the Rust judge image to pick it up. A new starter, *Design a Run Cursor*,
  takes one, in Python and Rust.
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
- Seven starter problems, each in Python and Rust, that exercise the Rust judge's main
  value types and every fixed comparison mode: *Matched Markers*,
  *Trailing Average*, *Carry Forward*, *Restock Ledger*, *Pairs to Target*, *Any Peak*
  and *Count Lakes*.
- **Linked lists, trees and graphs in Rust.** A Rust problem can take and return linked
  lists (`Option<Box<ListNode>>`) and binary trees (`Option<Rc<RefCell<TreeNode>>>`),
  including a list of them (`Vec<…>`), plus cyclic lists, lists with random pointers and
  graphs (`CyclicListNode`, `RandomListNode`, `GraphNode`, all `Rc<RefCell<…>>`). They use
  the same wire format as Python, so one problem's cases serve both languages. The judge
  defines the node structs in the submission's own crate, so a solution can still add
  methods or trait impls to them (`impl Ord for ListNode`), and a submission that pastes
  a struct back in gets a hint instead of a bare compile error. Six starters, each in Python and Rust, show them: *Deal from Both
  Ends* (a list), *Trim to Price Band* (a binary search tree), *Merge Sorted Feeds*
  (several lists), *Find the Loop's Entrance* (a cyclic list), *Copy the Referral Chain*
  (random pointers) and *Copy the Station Map* (a graph).
- **Problems in several languages.** A problem can be offered in any number of
  languages, with one shared statement and one shared set of test cases. The workspace
  gets a language switcher (shown only when there's more than one), keeps a separate
  draft per language, and shows a short language-specific note under the statement.
  Solutions show code in the editor's language, and label an approach that exists in
  only some languages ("Rust only"). The Submissions tab and the verdict show each
  submission's language, and "Beats X%" compares only against accepted submissions in
  the same language. *Merge Booking Windows* is now offered in Python, JavaScript and
  Rust. Problem files list `languages` and give solution `code` per language; the
  single-language form still loads unchanged. `POST /submissions` and `/run` take a
  `language`, which may be omitted only on a one-language problem (a multi-language
  one answers 400 `LANGUAGE_REQUIRED`, rather than judging an old client's code in a
  default it wasn't written for). Existing deployments need `alembic upgrade head`,
  which moves each problem's language fields into the new `problem_languages` table
  and backfills every submission's language. **That migration isn't compatible with
  the previous release's code**, so stop the api and worker before upgrading (on
  Kubernetes, scale both Deployments to 0, `helm upgrade`, then scale back up), or
  expect errors from the old pods until the rollout finishes.

- **Design (operations) problems in Rust.** A Rust problem can be `kind: "operations"`:
  the judge builds the object with `new` and calls each method, inferring its arguments
  and return type from the user's own `impl`, so a problem file declares no method
  signatures. Rust methods are snake_case, so the cases' `getState` calls `get_state`, and
  a method named as the cases spell it gets a hint in the compile error. The five design
  starters (*Vending Machine*, *Undo/Redo Editor*, *Price Feed*, *B-Tree*, *Lazy Segment
  Tree*) are now offered in Rust too, and a new one, *Design a Sorted Tree Cursor*
  (the Iterator pattern), takes a `TreeNode` in its constructor, in Python and Rust.

### Changed

- **A custom validator's error no longer reaches the user.** A validator that fails
  to load or raises shows "the problem's custom validator failed (a problem bug, not
  your code)", as the Rust judge already did, and its detail goes to the judge's log,
  since it could quote the expected answer. The judge worker now logs that output,
  and on Kubernetes it no longer turns such a run into a whole-submission
  `judge_error` (the Pod log mixes it with the verdict, which is now picked out of
  the log by its content).
- **The accounts worker names stored problems whose custom validator the judge refuses**
  when it starts, so an upgrade that ran before the problem set was re-seeded shows up
  in the log at once.

### Removed

- **The older custom-validator form.** `def validate(actual, expected, args,
  instance=None)` is refused: the judge reports it as a `judge_error`, and seeding
  refuses the file, as it does any validator that can't be called with exactly those
  four arguments by keyword. Write `def validate(actual, expected, args, probe_results)`, with
  the cases' probes for any extra calls on an operations instance (ADR-0007). Convert
  and re-seed any problem that still uses it before upgrading.

### Security

- **The judge now runs a submission in a separate process from the grader.** The Python
  and JavaScript harnesses used to run the submission in the harness process itself, which
  held every test case's expected answer and wrote the verdict. A submission could read the
  expected answers out of that process, or write a forged "all passed" report, and be marked
  accepted without solving the problem. Now the harness parent holds the expected answers and
  writes the report, while the submission runs in a child process that receives only the
  inputs and whose output goes to a private channel — so pass/fail is decided from an expected
  the submission never sees, and it can't reach the report. Rust already isolated each case in
  its own process; SQL is unaffected. The worker also refuses a report that doesn't have one
  row per test case, and on Kubernetes the payload is copied into a volume that every harness
  — Python, JavaScript and Rust — reads and then deletes before running the submission, so it
  can't be read back off disk at the fixed path. See
  [ADR-0006](docs/adr/0006-harness-process-isolation.md).
- **Function-mode `custom_validator` problems no longer leak the expected answer or accept a
  forged verdict.** Every custom validator ran inside the submission's child process, which
  was sent each case's `expected`, and a submission could write its own "passed" message to
  the channel the verdict came back on. A function-mode validator now runs in the harness
  parent, like every other comparison mode, and the child receives only the inputs. It gets
  what's left of the case's time limit, so a runaway validator is still
  `time_limit_exceeded`. Its time no longer counts toward the case's `runtime_ms`, and it
  sees the returned value after JSON encoding (a tuple arrives as a list), as the other modes
  do. Operations-mode validators still run in the child for now. See
  [ADR-0007](docs/adr/0007-custom-validators-in-every-language.md).

### Fixed

- The Helm chart now migrates the database *before* an upgrade's new pods start
  (a `pre-upgrade` hook). It used to run after them, so the new code briefly met the
  old schema.
- A draft saved before problems had several languages is no longer moved into a
  multi-language problem's default language, which could put Rust code in the Python
  editor (it stays where it was; one-language problems still migrate it).
- Each language in the editor keeps its own undo history, so Ctrl+Z after switching
  languages can't restore the other language's code into the draft.
- The Solutions tab and the submission view can't switch the editor's language while
  a verdict is pending (the language switcher already couldn't).
- Loading a Rust or JavaScript solution keeps the starter's reference comment (the
  judge-defined node struct), as Python's always did.

- A problem file whose cyclic-list position, random-pointer index or graph neighbour
  doesn't point at a real node is now refused when it's loaded. The Python judge used to
  wrap such an index silently (`-1` became the last node).
- A returned linked list (including a random-pointer list) or tree with a cycle is now a
  runtime error ("the returned list has a cycle") in the Python judge, instead of being
  judged on its first pass. A
  solution that forgot to end its list could be accepted, because that first pass
  matched the expected answer exactly. A tree that shares a subtree between two parents
  (no cycle) is still a valid answer.
- The Python judge no longer counts a bool as equal to a number (`True` against an
  expected `1`, in any comparison mode). The JavaScript and Rust judges already
  rejected it, so the same answer could be judged differently depending on the language.
- The Kubernetes deploy no longer has a size limit on the problem set. The bundled
  starters (~2.6 MB) had outgrown the 1 MiB ConfigMap that carried them. `scripts/k8s-up.sh`
  now builds `PROBLEMS_DIR` into a small seed image, which the chart's migrate hook copies
  the problems out of. On a real cluster, build and push your own seed image and set
  `images.seed`.
- The navbar's page links now line up with the *shikomi* brand beside them. They used to
  sit about 2px higher.
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
