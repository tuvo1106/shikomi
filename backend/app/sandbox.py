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
        function_mode_only: the harness has no `operations` kind and no node codecs, and
            `ProblemIn` refuses those combinations at seed time.
        min_memory_limit_mb: the smallest `memory_limit_mb` a problem may declare. Rust needs
            room for rustc, which compiles inside the same limit (~75MB peak).
    """

    image_setting: str
    default_image: str
    tmpfs_size_mb: int = 16
    tmpfs_exec: bool = False
    startup_slack_s: float = 0
    function_mode_only: bool = False
    min_memory_limit_mb: int = 1


PROFILES = {
    "python": SandboxProfile("judge_image", "shikomi-judge:latest"),
    "js": SandboxProfile("judge_image_js", "shikomi-judge-js:latest", function_mode_only=True),
    # The margin over the compile timeout covers the harness's own startup and payload parsing.
    "rust": SandboxProfile("judge_image_rust", "shikomi-judge-rust:latest", tmpfs_size_mb=32,
                           tmpfs_exec=True, startup_slack_s=RUST_COMPILE_TIMEOUT_S + 2,
                           function_mode_only=True, min_memory_limit_mb=128),
    # ADR-0002: the tuned MariaDB datadir needs ~22MB of tmpfs and boots in ~0.05s; 2s is a
    # generous multiple.
    "mysql": SandboxProfile("judge_image_sql", "shikomi-judge-sql:latest", tmpfs_size_mb=32,
                            startup_slack_s=2),
}


def profile_for(language: str) -> SandboxProfile:
    """`language`'s profile, falling back to Python's for an unknown value.

    The fallback is defensive only: `ProblemIn`'s `Language` literal and the
    `ck_problems_language` constraint already reject anything else.
    """
    return PROFILES.get(language, PROFILES["python"])
