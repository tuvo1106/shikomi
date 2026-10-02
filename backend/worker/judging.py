"""Shared judge execution: build payload → run sandbox → aggregate verdict.

Called by the worker's `judge_submission` job (worker/judge.py) for both Run
and Submit, so the two always judge identically. See DESIGN.md §5.2, §5.3.
"""
import json
import logging

from app import telemetry
from app.config import get_settings
from app.judge_budget import wall_budget_s
from app.sandbox import RUST_COMPILE_TIMEOUT_S, profile_for
from worker import runner
from worker.aggregate import Verdict, aggregate, parse_harness_output

logger = logging.getLogger(__name__)

# How much of a harness's stderr reaches the worker log per run.
MAX_LOGGED_STDERR = 4000

settings = get_settings()

CPUS = "1"
PIDS_LIMIT = 64
MAX_REVEAL_CHARS = 2000  # cap embedded input/expected so verdict_detail stays bounded


def _image_for(profile) -> str:
    """The configured image for `profile`: its `Settings` field (e.g. `JUDGE_IMAGE_RUST`),
    whose default is the profile's own tag (app/sandbox.py)."""
    return getattr(settings, profile.image_setting)


def _capped(value) -> dict:
    """Wrap `value` for embedding in verdict_detail as `{"value", "truncated"}`.

    Keeps a revealed large test case from bloating verdict_detail (and the API
    response) by truncating its JSON encoding instead of embedding it whole. The
    shape is the same either way — `value` is never sometimes-the-original-object
    and sometimes-a-string — so callers don't have to guess which they got;
    `truncated` says which case applies.
    """
    encoded = json.dumps(value)
    if len(encoded) <= MAX_REVEAL_CHARS:
        return {"value": value, "truncated": False}
    return {"value": encoded[:MAX_REVEAL_CHARS] + "…(truncated)", "truncated": True}


async def run_judgement(*, code, comparison, time_limit_ms, memory_limit_mb,
                        test_cases, container_name, function_name=None, params=None,
                        return_type="", kind="function", class_name=None,
                        language="python") -> Verdict:
    """test_cases: list of {"id", "input", "expected"}. Runs one sandbox container.

    `params`/`return_type` are only needed when a param or the return value is a
    `ListNode`/`TreeNode` — the harness converts those cases' JSON to/from the
    object graph; every other problem passes `params=None` and gets
    today's plain JSON-in/JSON-out behavior unchanged.

    `kind`/`class_name` select the harness's design/class-replay mode
    ("operations": `class_name` names a class the harness instantiates once per
    test case and replays a method-call sequence against — judge/harness.py's
    `_run_operations`) instead of the default single-`function_name` mode.

    `language` (DESIGN.md §13) picks the sandbox profile (app/sandbox.py):
    image, tmpfs size and `exec`, and startup slack. Callers never hardcode any
    of them.

    `memory_limit_mb` rides in the payload as well as setting the container's
    cgroup limit. The Rust harness applies it per case as `RLIMIT_AS`, so an
    allocation bomb fails that one case as `memory_limit_exceeded` instead of
    drawing the container OOM killer (ADR-0004). The other harnesses ignore it.
    `compile_timeout_s` is likewise Rust-only: the same constant the wall
    budget reserves for compiling (app/judge_budget.py), sent so the harness's
    deadline and the budget can't drift apart.
    """
    payload = json.dumps({
        "function_name": function_name,
        "user_code": code,
        "test_cases": test_cases,
        "comparison": comparison,
        "time_limit_ms": time_limit_ms,
        "memory_limit_mb": memory_limit_mb,
        "compile_timeout_s": RUST_COMPILE_TIMEOUT_S,
        "params": params or [],
        "return_type": return_type,
        "kind": kind,
        "class_name": class_name,
    })
    # The same number the authoring path checks against arq's job timeout (app/judge_budget.py).
    wall_timeout = wall_budget_s(len(test_cases), time_limit_ms, language)
    profile = profile_for(language)
    # `outcome` is the verdict status, so judge.run.count{outcome:time_limit_exceeded} is the
    # rate of sandboxes the judge had to give up on. A fault in the runner itself (the
    # exception path) is outcome:error.
    with telemetry.observe("judge.run", language=language, runner=settings.judge_runner) as obs:
        result = await runner.run_in_container(
            payload, image=_image_for(profile), container_name=container_name,
            memory_mb=memory_limit_mb, cpus=CPUS, pids_limit=PIDS_LIMIT,
            tmpfs_size_mb=profile.tmpfs_size_mb, tmpfs_exec=profile.tmpfs_exec,
            wall_timeout_s=wall_timeout)
        if result.stderr.strip():
            # The harness's own diagnostics (the trusted parent's stderr; a submission's
            # goes to /dev/null): a custom validator's exception, a bad payload. The user
            # sees only a fixed line for those, since the detail could quote `expected`,
            # so this log is where the problem's author finds it.
            logger.warning("judge harness stderr (%s): %s", container_name,
                           result.stderr[:MAX_LOGGED_STDERR])
        case_ids = [tc["id"] for tc in test_cases]
        verdict = aggregate(result, parse_harness_output(result.stdout, case_ids),
                            total_cases=len(test_cases))
        obs.outcome = verdict.status
    return verdict


def build_verdict_results(results: list[dict], cases: list) -> list[dict]:
    """Decide what per-case detail is stored (DESIGN.md §5.3).

    Sample cases keep full detail. The **first failing** case is also revealed —
    with its input/expected embedded so the user can debug the failure — but that's the only hidden case exposed, limiting suite harvesting to
    one case per submission. Every other hidden case is reduced to status + runtime.
    """
    sample_ords = {tc.ordinal for tc in cases if tc.is_sample}
    case_by_ord = {tc.ordinal: tc for tc in cases}
    first_fail = next((r.get("test_case_id") for r in results if r.get("status") != "passed"), None)

    stored = []
    for r in results:
        ordinal = r.get("test_case_id")
        if ordinal not in sample_ords and ordinal != first_fail:
            stored.append({
                "test_case_id": ordinal,
                "status": r.get("status"),
                "runtime_ms": r.get("runtime_ms"),
            })
            continue
        entry = dict(r)
        # For the revealed hidden case the client has no sample data, so embed
        # input/expected. (Samples: the client already has them from the problem.)
        tc = case_by_ord.get(ordinal)
        if tc is not None and ordinal not in sample_ords:
            entry["input"] = _capped(tc.input)
            entry["expected"] = _capped(tc.expected)
        stored.append(entry)
    return stored
