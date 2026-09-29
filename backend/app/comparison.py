"""A problem's `comparison`, resolved for the one language a submission is judged in.

`comparison` (DESIGN.md §5.4) is shared by every language a problem is offered in, and
for every fixed-answer mode (`exact`, `unordered`, ...) that's the whole story: the harness
compares JSON values, whatever language produced them. A `custom_validator` is different,
because it's *code*, and each harness can only run code in its own language. So its
`validator_code` is a map from language to source (docs/adr/0007-custom-validators-in-
every-language.md):

    {"mode": "custom_validator", "validator_code": {"python": "def validate(...): ...",
                                                    "rust": "fn validate(...) -> bool { ... }"}}

Every harness still receives a plain string, the way it always has. `for_language` is the
one place that picks it, and every path that builds a judge payload calls it: the worker
(`worker/judge.py`) and the seed-solution tests (`judge/tests/test_seed_solutions.py`), so
the two can't disagree about which validator judges a language.

Stdlib-only on purpose, like `app.sandbox`: the judge tests import it with only pytest
installed (AGENTS.md "Test suites").
"""

# Before validators were per language, `validator_code` was one Python string, and a
# problem *file* may still say it that way (it's the terse form for a Python-only
# problem). The database only ever holds the map: `ProblemIn` stores it on seed, and
# migration e5a8c2f41d93 converted the rows seeded earlier. The string form is read
# here for files, which the seed-solution tests hand to `for_language` unvalidated.
LEGACY_VALIDATOR_LANGUAGE = "python"


def validator_codes(comparison: dict | None) -> dict:
    """The comparison's per-language validator map, reading a bare string as Python's.

    The one definition of that rule: `ProblemIn` (seed time) and `for_language` (judge
    time) both call this, so they can't disagree about which language a string belongs to.

    Returns `{}` when there's no `validator_code` at all (every fixed-answer mode).

    Raises:
        ValueError: `validator_code` is neither a string nor a map.
    """
    code = (comparison or {}).get("validator_code")
    if code is None:
        return {}
    if isinstance(code, str):
        return {LEGACY_VALIDATOR_LANGUAGE: code}
    if isinstance(code, dict):
        return code
    raise ValueError(
        "'validator_code' must be a map of language to validator source (or one Python "
        f"source string), not {type(code).__name__}")


def for_language(comparison: dict | None, language: str) -> dict:
    """`comparison` as the harness for `language` expects it.

    A fixed-answer mode passes through unchanged. For `custom_validator`, `validator_code`
    becomes that language's source string. A copy is returned, so the problem row's own
    dict is never mutated.

    Raises:
        ValueError: a `custom_validator` with no validator for `language`, or a malformed
            `validator_code`. `ProblemIn` refuses both at seed time, so reaching it means
            the row was written some other way. It's a judge fault (the worker records `judge_error`), never a guess:
            falling back to another language's validator would hand code to a harness that
            can't run it.
    """
    comparison = dict(comparison or {"mode": "exact"})
    if comparison.get("mode") != "custom_validator":
        return comparison
    code = validator_codes(comparison).get(language)
    if not isinstance(code, str) or not code.strip():
        raise ValueError(f"custom_validator has no validator for language {language!r}")
    comparison["validator_code"] = code
    return comparison
