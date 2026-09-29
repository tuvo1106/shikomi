"""Every seeded problem's solutions, judged for real (DESIGN.md §7).

`seed/problems/*.json` solutions are hand-authored, and nothing else runs them
through the actual judge — this is what catches a solution that's subtly wrong,
mismatched against its own test cases, or written for the wrong `comparison`
mode. For `python`/`js` problems this drives the harness
directly as a bare subprocess (like the protocol tests — no Docker, no DB), so
a seed-authoring mistake fails `pytest -m "not docker"` instead of surfacing
first as a confused user report. `mysql` and `rust` problems can't take that path — the
harness needs a live database server, which only the built sandbox image
provides (`judge/sql_entrypoint.sh` boots it) — so those run the real
`shikomi-judge-sql:latest` image instead, same as `judge/tests/
test_sql_protocol.py`, and are Docker-marked accordingly (skipped under
`pytest -m "not docker"`, same as every other Docker-dependent test in this
repo). `rust` is the same story for a different reason: its harness compiles
the solution with the rustc and prebuilt prelude that only
`shikomi-judge-rust:latest` carries (judge/tests/rust_runner.py).

Point it at your own problems with `SEED_DIR=/path/to/problems` (default:
this repo's `seed/problems/`) — it's the check to run before `app.cli seed`
loads a problem you wrote.
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

from rust_runner import rust_results
from sql_runner import run_sql_container

# The same per-language validator resolution the worker applies (stdlib-only, so
# importable here with only pytest installed, like rust_runner's app.sandbox).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "backend"))
from app.comparison import for_language as comparison_for_language  # noqa: E402

HARNESS_PY = pathlib.Path(__file__).resolve().parents[1] / "harness.py"
HARNESS_JS = pathlib.Path(__file__).resolve().parents[1] / "harness.js"
# Overridable so an operator can validate their own problem directory, not just
# the bundled starters.
SEED_DIR = pathlib.Path(
    os.environ.get("SEED_DIR") or pathlib.Path(__file__).resolve().parents[2] / "seed" / "problems")

# A seed problem's "language" (DESIGN.md §13) picks which harness judges its
# solutions for real — same guarantee this file gives Python problems, extended
# to JS/SQL ones instead of assuming Python for everything.
_HARNESS_COMMAND = {
    "python": [sys.executable, str(HARNESS_PY)],
    "js": ["node", str(HARNESS_JS)],
}


def _run_harness_subprocess(payload, language, timeout=15):
    proc = subprocess.run(
        _HARNESS_COMMAND[language],
        input=json.dumps(payload), capture_output=True, text=True, timeout=timeout,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["results"]


def _run_harness_sql(payload, timeout=30):
    proc = run_sql_container(json.dumps(payload), container_name="judge-seed-validate-sql",
                             timeout=timeout)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["results"]


# Languages judged in their real sandbox image rather than as a local subprocess.
_CONTAINER_LANGUAGES = ("mysql", "rust")


def _run_harness(payload, language="python", timeout=15):
    if language == "mysql":
        return _run_harness_sql(payload, timeout=max(timeout, 30))
    if language == "rust":
        return rust_results(payload, container_name="judge-seed-validate-rust",
                            memory_mb=payload["memory_limit_mb"])
    return _run_harness_subprocess(payload, language, timeout=timeout)


def _cases(problem):
    # Seed JSON keys test cases `ordinal`; the harness protocol keys them `id` —
    # same field, different name at each layer (DESIGN.md §3.3 vs §5.3).
    return [
        {"id": tc["ordinal"], "input": tc["input"], "expected": tc.get("expected")}
        for tc in problem["test_cases"]
    ]


def _seed_problems():
    return [json.loads(path.read_text()) for path in sorted(SEED_DIR.glob("*.json"))]


def _variants(problem):
    """The problem's languages, default first (DESIGN.md §7.1). A file in the
    single-language form keeps its fields at the top level; read it the way
    `ProblemIn` lifts it, as one variant."""
    if "languages" in problem:
        return problem["languages"]
    return [{"language": problem.get("language", "python"),
             **{k: problem[k] for k in ("function_name", "class_name", "params", "return_type")
                if k in problem}}]


def _solution_cases():
    # One parametrization per (solution, language it has code for), so every
    # language of every problem is proven by a real harness run. Languages judged
    # in their real sandbox image need Docker, so just those parametrizations are
    # marked `docker` and the fast `pytest -m "not docker"` path stays Docker-free,
    # the same convention as the rest of this repo's Docker-gated tests.
    params = []
    for problem in _seed_problems():
        variants = {v["language"]: v for v in _variants(problem)}
        only = next(iter(variants))
        for i, solution in enumerate(problem.get("solutions", [])):
            codes = solution["code"] if isinstance(solution["code"], dict) else {only: solution["code"]}
            for language, code in codes.items():
                params.append(pytest.param(
                    problem, variants[language], solution["title"], code,
                    id=f"{problem['slug']}-{solution.get('ordinal', i)}-{language}",
                    marks=pytest.mark.docker if language in _CONTAINER_LANGUAGES else ()))
    return params


@pytest.mark.parametrize("problem,variant,title,code", _solution_cases())
def test_seed_solution_passes_its_own_test_cases(problem, variant, title, code):
    payload = {
        "kind": problem.get("kind", "function"),
        "function_name": variant.get("function_name"),
        "class_name": variant.get("class_name"),
        "user_code": code,
        "test_cases": _cases(problem),
        "comparison": comparison_for_language(problem.get("comparison"), variant["language"]),
        "time_limit_ms": problem.get("time_limit_ms", 2000),
        "memory_limit_mb": problem.get("memory_limit_mb", 256),
        "params": variant.get("params", []),
        "return_type": variant.get("return_type", ""),
    }
    results = _run_harness(payload, language=variant["language"])
    failures = [r for r in results if r["status"] != "passed"]
    assert not failures, (
        f"{len(failures)}/{len(results)} case(s) failed for "
        f"{problem['slug']} ({title}, {variant['language']}):\n"
        + "\n".join(
            f"  case {r['test_case_id']}: {r['status']} "
            f"(output={r['output']!r}, error={r['error']!r})"
            for r in failures
        )
    )


@pytest.mark.parametrize("problem", _seed_problems(), ids=lambda p: p["slug"])
def test_seed_problem_has_at_least_one_solution(problem):
    # Not the harness's job, but the same authoring mistake this file exists to
    # catch: a problem with zero solutions has nothing here to validate it.
    assert problem.get("solutions"), f"{problem['slug']} has no solutions to validate"
