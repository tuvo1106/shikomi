"""Every seeded problem's solutions, judged for real (DESIGN.md §7).

`seed/problems/*.json` solutions are hand-authored, and nothing else runs them
through the actual judge — this is what catches a solution that's subtly wrong,
mismatched against its own test cases, or written for the wrong `comparison`
mode. For `python`/`js` problems this drives the harness
directly as a bare subprocess (like the protocol tests — no Docker, no DB), so
a seed-authoring mistake fails `pytest -m "not docker"` instead of surfacing
first as a confused user report. `mysql` problems can't take that path — the
harness needs a live database server, which only the built sandbox image
provides (`judge/sql_entrypoint.sh` boots it) — so those run the real
`shikomi-judge-sql:latest` image instead, same as `judge/tests/
test_sql_protocol.py`, and are Docker-marked accordingly (skipped under
`pytest -m "not docker"`, same as every other Docker-dependent test in this
repo).

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

from sql_runner import run_sql_container

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


def _run_harness(payload, language="python", timeout=15):
    if language == "mysql":
        return _run_harness_sql(payload, timeout=max(timeout, 30))
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


def _solution_cases():
    # mysql problems need the real SQL sandbox image + a live database (see
    # module docstring) — mark just those parametrizations `docker` so the
    # fast `pytest -m "not docker"` path stays Docker-free for every other
    # problem, same convention as the rest of this repo's Docker-gated tests.
    return [
        pytest.param(problem, solution, id=f"{problem['slug']}-{solution.get('ordinal', i)}",
                     marks=pytest.mark.docker if problem.get("language") == "mysql" else ())
        for problem in _seed_problems()
        for i, solution in enumerate(problem.get("solutions", []))
    ]


@pytest.mark.parametrize("problem,solution", _solution_cases())
def test_seed_solution_passes_its_own_test_cases(problem, solution):
    payload = {
        "kind": problem.get("kind", "function"),
        "function_name": problem.get("function_name"),
        "class_name": problem.get("class_name"),
        "user_code": solution["code"],
        "test_cases": _cases(problem),
        "comparison": problem.get("comparison", {"mode": "exact"}),
        "time_limit_ms": problem.get("time_limit_ms", 2000),
        "params": problem.get("params", []),
        "return_type": problem.get("return_type", ""),
    }
    results = _run_harness(payload, language=problem.get("language", "python"))
    failures = [r for r in results if r["status"] != "passed"]
    assert not failures, (
        f"{len(failures)}/{len(results)} case(s) failed for "
        f"{problem['slug']} ({solution['title']}):\n"
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
