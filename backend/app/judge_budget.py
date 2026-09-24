"""One definition of "how long may a judge run take", shared by everything that depends on it.

Three separate things must agree, or a judge job dies in a way nothing handles:

* the **runner's wall clock** (`worker/judging.py`): how long a sandbox may run before we kill
  it from outside, `cases x time limit + slack`;
* arq's **`job_timeout`** (`worker/main.py`): the backstop that cancels the whole job;
* the **seed-time rules** (`services/problem_service.py`): what `app.cli seed` will load.

If a problem's wall clock ever exceeded the job timeout, arq would cancel the job before our own
kill fired, skipping the verdict write (`CancelledError` is not an `Exception`) and leaving the
sandbox running. Rather than hope nobody authors such a problem, the seed path refuses it
using the same numbers the worker uses. The same bound also tells the orphan sweep how old a
*live* sandbox can possibly be (`MAX_LIVE_SANDBOX_AGE_S`), which is what makes that sweep safe
across worker replicas.

Kept free of imports from `app.config` / the worker so both the API and the worker can use it.
"""
import math

JUDGE_JOB_TIMEOUT_SECONDS = 300  # arq's job_timeout for the judge worker (worker/main.py)

WALL_CLOCK_SLACK_S = 10  # backstop above the harness's own per-case SIGALRM (§5.2 step 4)
# One-time server cold-start absorbed on top of WALL_CLOCK_SLACK_S for languages whose sandbox
# boots more than an interpreter (today: "mysql" — judge/sql_entrypoint.sh copies the pre-baked
# datadir into tmpfs and starts mariadbd before the harness even begins). ADR-0002's Phase 0
# spike measured ~0.05-0.06s in practice; this is a generous multiple of that; python/js pay 0.
STARTUP_SLACK_S_BY_LANGUAGE = {"mysql": 2}

# After the wall-clock kill the runner drains output (up to 5s) and the job writes the verdict
# to Postgres; that has to fit inside the job timeout too.
KILL_AND_WRITE_MARGIN_S = 10

# A live sandbox belongs to a job that arq cancels at JUDGE_JOB_TIMEOUT_SECONDS, so it can't be
# older than that (plus scheduling slop). Anything older is an orphan, whichever worker replica
# started it. This is the rule the orphan sweep uses instead of "is it in *my* active set".
MAX_LIVE_SANDBOX_AGE_S = JUDGE_JOB_TIMEOUT_SECONDS + 60


def wall_budget_s(case_count: int, time_limit_ms: int, language: str = "python") -> float:
    """The runner's wall-clock kill for a run of `case_count` cases at `time_limit_ms` each."""
    startup = STARTUP_SLACK_S_BY_LANGUAGE.get(language, 0)
    return case_count * (time_limit_ms / 1000.0) + WALL_CLOCK_SLACK_S + startup


def fits_job_timeout(case_count: int, time_limit_ms: int, language: str = "python") -> bool:
    """Whether a full run (all cases) finishes, verdict written, inside arq's job timeout."""
    return (wall_budget_s(case_count, time_limit_ms, language) + KILL_AND_WRITE_MARGIN_S
            < JUDGE_JOB_TIMEOUT_SECONDS)


def max_cases_within_job_timeout(time_limit_ms: int, language: str = "python") -> int:
    """The most cases a problem with this time limit may have (for an actionable error)."""
    headroom = (JUDGE_JOB_TIMEOUT_SECONDS - KILL_AND_WRITE_MARGIN_S - WALL_CLOCK_SLACK_S
                - STARTUP_SLACK_S_BY_LANGUAGE.get(language, 0))
    # strictly less than the timeout, hence the epsilon before flooring
    return max(0, math.ceil(headroom / (time_limit_ms / 1000.0)) - 1)
