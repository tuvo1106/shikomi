"""Per-language sandbox profiles: everything that differs between the judge images, in one table.

Each problem `language` (DESIGN.md §13) is judged by its own harness in its own image, and
those images differ in more than the image tag: SQL needs a bigger /tmp for MariaDB's data
directory, Rust needs /tmp mounted `exec` (it runs the binary it compiles there), and both
boot or compile something before the first test case, which the wall-clock budget has to
reserve time for.

Those facts used to live in five parallel tables across three modules (image, tmpfs size,
tmpfs exec, startup slack, function-mode-only), and every caller had to consult all of them.
One that didn't, the `worker.judge_local` CLI, ran Rust with a noexec /tmp, so every case
failed. Now a caller asks `profile_for(language)` and gets all of it, and adding a language
is one entry here.

Stdlib-only on purpose: the API (`app.judge_budget`, `app.schemas.problem`), the worker, and
the judge tests (which run with only pytest installed, see AGENTS.md "Test suites") all read
this table. The image tag here is only the *default*. `app.config.Settings` exposes each one
as an overridable setting (`JUDGE_IMAGE_RUST`, ...) whose default comes from this table, and
the worker resolves the configured value by `image_setting`.
"""
import dataclasses
import re

# rustc's hard stop inside the Rust harness. The worker sends it in the payload
# (`compile_timeout_s`) and the wall budget reserves it through the Rust profile's
# `startup_slack_s`, so the harness's deadline and the budget come from this one number.
# ADR-0004: a normal submission compiles in ~150ms; rustc gives up on a const-eval bomb by
# itself in ~1.7s.
RUST_COMPILE_TIMEOUT_S = 10


@dataclasses.dataclass(frozen=True)
class SandboxProfile:
    """How to run one language's judge sandbox.

    Attributes:
        image_setting: the `Settings` field naming this language's image, so an operator can
            point it elsewhere (`JUDGE_IMAGE_RUST=...`). `default_image` is that field's default.
        tmpfs_size_mb: the writable /tmp (`docker_runner.build_run_args`, and the k8s
            emptyDir's `size_limit`).
        tmpfs_exec: mount /tmp `exec`. Docker's default is noexec, and only a harness that
            writes a binary there and runs it needs this lifted (ADR-0004).
        startup_slack_s: one-time work before the first case (a database cold start, a
            compile), added to the worker's wall-clock budget (`app.judge_budget`).
        function_mode_only: the harness has no `operations` kind, and `ProblemIn` refuses
            that combination at seed time.
        custom_validator: the harness can run a `custom_validator` written in this language
            (DESIGN.md §5.4, ADR-0007). `ProblemIn` refuses a validator problem offered in a
            language without it.
        node_types: the node codecs (`ListNode`, `TreeNode`, ...) the harness implements, as
            the names a problem declares in `params[].type`/`return_type`. `ProblemIn`
            refuses any other node type for this language.
        min_memory_limit_mb: the smallest `memory_limit_mb` a problem may declare. Rust needs
            room for rustc, which compiles inside the same limit (~75MB peak).
        node_value_range: the inclusive range a node's `val` must fit, or None for any JSON
            value. Rust's node structs hold an `i32` (the conventional shape), while
            harness.py's nodes hold anything; `ProblemFile` checks the shared test cases
            against it, so a value Rust can't decode fails at seed time, not on a submission.
    """

    image_setting: str
    default_image: str
    tmpfs_size_mb: int = 16
    tmpfs_exec: bool = False
    startup_slack_s: float = 0
    function_mode_only: bool = False
    custom_validator: bool = False
    min_memory_limit_mb: int = 1
    node_types: frozenset[str] = frozenset()
    node_value_range: tuple[int, int] | None = None


# Every node codec name there is: the one list each profile's `node_types` is a subset
# of. judge/harness.py implements all of them (its `_CODECS`, plus the decode-only
# "Iterator" for an operations constructor); `ReturnType` in app/schemas/problem.py is
# this minus "Iterator", which a test pins.
ALL_NODE_TYPES = frozenset({
    "ListNode", "TreeNode", "List[ListNode]", "List[TreeNode]",
    "CyclicListNode", "RandomListNode", "GraphNode", "Iterator",
})
# judge/harness_rs/prelude.rs: every node codec (`nodes`), plus `IntIter`, the Rust
# counterpart of the decode-only "Iterator" (an `impl Iterator<Item = i32>`).
# Spelled out (not an alias of ALL_NODE_TYPES) so a codec added for Python only doesn't
# become valid for Rust by accident; a test pins this against harness.rs and prelude.rs.
RUST_NODE_TYPES = frozenset({
    "ListNode", "TreeNode", "List[ListNode]", "List[TreeNode]",
    "CyclicListNode", "RandomListNode", "GraphNode", "Iterator",
})
I32_RANGE = (-(2**31), 2**31 - 1)


PROFILES = {
    "python": SandboxProfile("judge_image", "shikomi-judge:latest", custom_validator=True,
                             node_types=ALL_NODE_TYPES),
    "js": SandboxProfile("judge_image_js", "shikomi-judge-js:latest", function_mode_only=True),
    # The margin over the compile timeout covers the harness's own startup and payload parsing.
    "rust": SandboxProfile("judge_image_rust", "shikomi-judge-rust:latest", tmpfs_size_mb=32,
                           tmpfs_exec=True, startup_slack_s=RUST_COMPILE_TIMEOUT_S + 2,
                           min_memory_limit_mb=128,
                           node_types=RUST_NODE_TYPES, node_value_range=I32_RANGE),
    # ADR-0002: the tuned MariaDB datadir needs ~22MB of tmpfs and boots in ~0.05s; 2s is a
    # generous multiple.
    "mysql": SandboxProfile("judge_image_sql", "shikomi-judge-sql:latest", tmpfs_size_mb=32,
                            startup_slack_s=2),
}


def profile_for(language: str) -> SandboxProfile:
    """`language`'s profile, falling back to Python's for an unknown value.

    The fallback is defensive only: `ProblemIn`'s `Language` literal and the
    `ck_problem_languages_language` constraint already reject anything else.
    """
    return PROFILES.get(language, PROFILES["python"])


# --- Rust operations mode ---------------------------------------------------------------

_RUST_KEYWORDS = frozenset({
    "as", "break", "const", "continue", "else", "enum", "extern", "false", "fn", "for", "if",
    "impl", "in", "let", "loop", "match", "mod", "move", "mut", "pub", "ref", "return",
    "static", "struct", "trait", "true", "type", "unsafe", "use", "where", "while", "async",
    "await", "dyn", "abstract", "become", "box", "do", "final", "macro", "override", "priv",
    "typeof", "unsized", "virtual", "yield", "try", "gen",
})
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def rust_method_name(op: str) -> str | None:
    """The Rust method an operations case's op name calls, or None if there can't
    be one. The name goes to snake_case (`getState` -> `get_state`, `toJSON` ->
    `to_json`), since the cases are shared and Rust methods are snake_case; a
    reserved word gets the raw-identifier prefix (`type` -> `r#type`). `new` is
    refused: it's the constructor the glue calls, and one `impl` can't also have a
    method of that name.

    A mirror of judge/harness_rs/harness.rs `method_name`, which is what the judge
    actually runs. Seed validation (`ProblemFile._ops_are_rust_methods`) uses it to
    refuse an op the Rust glue couldn't dispatch, or two ops that would land on one
    method, before a submission ever compiles. It lives here rather than in
    `app.schemas` so judge/tests/test_rust_protocol.py can import it without
    pydantic. Both implementations are tested against one table,
    judge/tests/rust_method_names.json, and backend tests check that the keyword
    list matches harness.rs `KEYWORDS`.
    """
    if not _IDENTIFIER.fullmatch(op):
        return None
    out = []
    for i, c in enumerate(op):
        if c.isupper():
            prev = op[i - 1] if i else ""
            next_lower = i + 1 < len(op) and op[i + 1].islower()
            boundary = prev.islower() or prev.isdigit() or (prev.isupper() and next_lower)
            if boundary and out and out[-1] != "_":
                out.append("_")
            out.append(c.lower())
        else:
            out.append(c)
    name = "".join(out)
    if name in {"self", "super", "crate", "Self", "_", "new"}:
        return None
    return f"r#{name}" if name in _RUST_KEYWORDS else name
