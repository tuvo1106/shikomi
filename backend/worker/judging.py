"""Shared judge execution: build payload → run sandbox → aggregate verdict.

Called by the worker's `judge_submission` job (worker/judge.py) for both Run
and Submit, so the two always judge identically. See DESIGN.md §5.2, §5.3.
"""
import json

from app.config import get_settings
from app.judge_budget import wall_budget_s
from worker import runner
from worker.aggregate import Verdict, aggregate, parse_harness_output

settings = get_settings()

CPUS = "1"
PIDS_LIMIT = 64
# tmpfs size per problem `language` (docker_runner.build_run_args'/k8s_runner's
# `tmpfs_size_mb`, default 16 there). Only "mysql" needs more: a database
# server's data directory has to live entirely in this scratch space
# (ADR-0002's Phase 0 spike: MariaDB's tuned template needs ~22MB, fits
# comfortably in 32).
TMPFS_SIZE_MB_BY_LANGUAGE = {"mysql": 32}
MAX_REVEAL_CHARS = 2000  # cap embedded input/expected so verdict_detail stays bounded

# Sandbox image per problem `language` (DESIGN.md §13).
IMAGE_BY_LANGUAGE = {
    "python": settings.judge_image,
    "js": settings.judge_image_js,
    "mysql": settings.judge_image_sql,
}


def _image_for(language: str) -> str:
    """Look up `language`'s sandbox image, defaulting to Python for an unknown
    value (defensive only — `ProblemIn`'s `Language` literal already rejects
    anything else at the authoring boundary)."""
    return IMAGE_BY_LANGUAGE.get(language, settings.judge_image)


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

    `language` (DESIGN.md §13) picks which harness/sandbox image judges the
    code — `_image_for` resolves it, so callers never hardcode an image.
    """
    payload = json.dumps({
        "function_name": function_name,
        "user_code": code,
        "test_cases": test_cases,
        "comparison": comparison,
        "time_limit_ms": time_limit_ms,
        "params": params or [],
        "return_type": return_type,
        "kind": kind,
        "class_name": class_name,
    })
    # The same number the authoring path checks against arq's job timeout (app/judge_budget.py).
    wall_timeout = wall_budget_s(len(test_cases), time_limit_ms, language)
    result = await runner.run_in_container(
        payload, image=_image_for(language), container_name=container_name,
        memory_mb=memory_limit_mb, cpus=CPUS, pids_limit=PIDS_LIMIT,
        tmpfs_size_mb=TMPFS_SIZE_MB_BY_LANGUAGE.get(language, 16),
        wall_timeout_s=wall_timeout)
    return aggregate(result, parse_harness_output(result.stdout), total_cases=len(test_cases))


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
