"""Authoring rules for test case probes (`ProbeIn`; judge/harness.py "probes";
docs/adr/0007-custom-validators-in-every-language.md).

Probes only feed a probe-form custom validator, and the harness quietly drops them
anywhere else, so every misuse is refused here, at seed time.
"""
import json

import pytest
from pydantic import ValidationError

from app.schemas.problem import MAX_PROBE_CALLS, ProblemFile
from worker.judge import _case_payload

PROBE_VALIDATOR = "def validate(actual, expected, args, probe_results):\n    return True\n"
DECODE = {"op": "decode", "args": [None], "refs": {"0": 1}}
WRONG = {"title": "Encodes nothing", "code": "class Codec:\n    def encode(self, s):\n        return ''\n"}


def _file(probes, *, kind="operations", validator=PROBE_VALIDATOR, comparison=None,
          ops=("Codec", "encode"), languages=None):
    return {
        "title": "T", "difficulty": "easy", "statement_md": "s", "kind": kind,
        "languages": languages or [{"language": "python", "class_name": "Codec", "starter_code": "c",
                                    "params": []}],
        "comparison": comparison or {"mode": "custom_validator", "validator_code": validator},
        "test_cases": [{"ordinal": 0, "input": [list(ops), [[] for _ in ops]], "expected": None, "is_sample": True,
                        "probes": probes}],
    }


def _error(data) -> str:
    with pytest.raises(ValidationError) as exc:
        ProblemFile.model_validate(data)
    return str(exc.value)


def test_probes_load_with_defaults_filled_in():
    p = ProblemFile.model_validate(_file([DECODE, {"op": "encode", "repeat": 3}]))
    probes = [probe.model_dump() for probe in p.test_cases[0].probes]
    assert probes == [{"op": "decode", "args": [None], "refs": {0: 1}, "repeat": 1},
                      {"op": "encode", "args": [], "refs": {}, "repeat": 3}]


def test_a_case_without_probes_has_none():
    assert ProblemFile.model_validate(_file(None)).test_cases[0].probes is None


def test_probes_need_an_operations_problem():
    function = [{"language": "python", "function_name": "f", "starter_code": "c", "params": []}]
    assert "need kind 'operations'" in _error(_file([DECODE], kind="function", languages=function))


def test_probes_need_a_custom_validator():
    assert "need comparison mode 'custom_validator'" in _error(
        _file([DECODE], comparison={"mode": "exact"}))


def test_the_older_instance_form_is_refused_in_every_mode():
    """It ran next to the submission in operations mode (ADR-0007), and the
    harness now refuses it, so seeding does too, in function mode as well."""
    older = "def validate(actual, expected, args, instance=None):\n    return True\n"
    assert "the older `instance` form is gone" in _error(_file(None, validator=older))
    function = [{"language": "python", "function_name": "f", "starter_code": "c", "params": []}]
    assert "the older `instance` form is gone" in _error(
        _file(None, kind="function", languages=function, validator=older))


def test_a_validator_that_cant_be_parsed_is_left_to_the_harness():
    """Parsed, never run, and a script that doesn't load is the harness's to
    report (a judge_error on every submission, which the seed tests catch)."""
    ProblemFile.model_validate(_file([DECODE], validator="def validate(:\n"))


def test_a_ref_must_fill_a_real_argument():
    assert "refs names argument 1, but it has 1" in _error(
        _file([{"op": "decode", "args": [None], "refs": {"1": 1}}]))


@pytest.mark.parametrize("source", [0, 2])
def test_a_ref_must_name_one_of_the_cases_own_ops(source):
    """0 is the constructor (it returns nothing); 2 is past the one op."""
    assert f"refs op {source}, but the case's ops are 1..1" in _error(
        _file([{"op": "decode", "args": [None], "refs": {"0": source}}]))


def test_repeat_is_bounded_per_probe_and_per_case():
    assert "greater than or equal to 1" in _error(_file([{"op": "encode", "repeat": 0}]))
    half = MAX_PROBE_CALLS // 2 + 1
    assert "over the" in _error(_file([{"op": "encode", "repeat": half}] * 2))


RUST_TOO = [{"language": "python", "class_name": "Codec", "starter_code": "c", "params": []},
            {"language": "rust", "class_name": "Codec", "starter_code": "c", "params": []}]
RUST_COMPARISON = {"mode": "custom_validator",
                   "validator_code": {"python": PROBE_VALIDATOR, "rust": "fn validate() {}"}}


def test_probes_load_for_a_problem_offered_in_rust():
    """harness.rs makes probe calls too (prelude.rs `ops::replay`)."""
    ProblemFile.model_validate(_file([DECODE], comparison=RUST_COMPARISON, languages=RUST_TOO))


def test_a_probe_op_must_map_to_a_rust_method():
    """A probe's op is dispatched like a case's (harness.rs `case_ops`), so the
    same op-name rules apply to it, although no case calls it."""
    assert "op 'has-dash' can't be a Rust method name" in _error(
        _file([{"op": "has-dash"}], comparison=RUST_COMPARISON, languages=RUST_TOO))
    assert "ops 'encode' and 'Encode' both map to the Rust method `encode`" in _error(
        _file([{"op": "Encode"}], comparison=RUST_COMPARISON, languages=RUST_TOO))


class _Case:
    def __init__(self, probes):
        self.ordinal, self.input, self.expected, self.probes = 0, [["C"], [[]]], None, probes


def test_the_worker_sends_probes_only_when_a_case_has_them():
    """Every other case's payload is exactly what it was before probes."""
    assert _case_payload(_Case(None)) == {"id": 0, "input": [["C"], [[]]], "expected": None}
    assert _case_payload(_Case([DECODE]))["probes"] == [DECODE]


@pytest.mark.parametrize("typo", [{"op": "pickIndex", "repeats": 4000},
                                  {"op": "decode", "args": [None], "ref": {"0": 1}}])
def test_an_unknown_probe_key_is_refused_not_dropped(typo):
    """Dropped, `repeats` would make one call and `ref` would pass a literal None."""
    assert "Extra inputs are not permitted" in _error(_file([typo]))


def test_refs_and_repeat_dont_combine():
    """So a repeated call's arguments are always small literals (cheap to copy
    per call, inside the time limit)."""
    assert "refs and repeat can't be combined" in _error(
        _file([{"op": "decode", "args": [None], "refs": {"0": 1}, "repeat": 2}]))


def test_of_two_top_level_defs_of_validate_the_last_is_checked():
    """Top-level statements run in order, so the last def is what the harness loads."""
    older_then_probe = "def validate(actual, expected, args, instance=None):\n    return 1\n" + PROBE_VALIDATOR
    ProblemFile.model_validate(_file([DECODE], validator=older_then_probe))
    probe_then_older = PROBE_VALIDATOR + "def validate(actual, expected, args, instance=None):\n    return 1\n"
    assert "the older `instance` form is gone" in _error(_file([DECODE], validator=probe_then_older))


def test_a_validator_the_harness_call_cant_bind_to_is_refused():
    """The harness calls `validate(actual=, expected=, args=, probe_results=)`;
    a leftover required `instance` would fail that on every case."""
    for sig in ("actual, expected, args, instance, probe_results",
                "actual, expected, args, probe_results, instance",
                "actual, expected, args, *, probe_results, instance"):
        bad = f"def validate({sig}):\n    return True\n"
        assert "the older `instance` form is gone" in _error(_file(None, validator=bad)), sig
    assert "is `async def`" in _error(_file(None, validator=(
        "async def validate(actual, expected, args, probe_results):\n    return False\n")))
    # Rebound after its def: what runs can't be told without running it.
    ProblemFile.model_validate(_file(None, validator=(
        "def validate(a):\n    return True\nvalidate = staticmethod(validate)\n")))
    assert "is a generator" in _error(_file(None, validator=(
        "def validate(actual, expected, args, probe_results):\n    yield False\n")))
    # A nested function's yield is its own.
    ProblemFile.model_validate(_file(None, validator=(
        "def validate(actual, expected, args, probe_results):\n"
        "    def g():\n        yield 1\n    return True\n")))
    # Unless every binding of `validate` is a top-level statement and the last is an
    # undecorated def, what runs can't be told without running it: seeding leaves it
    # to the harness, which judges the real object.
    older = "def validate(actual, expected, args, instance=None):\n    return True\n"
    good = "def validate(actual, expected, args, probe_results):\n    return True\n"
    for unclear in ("@adapt\n" + older,
                    older + "validate, _ = wrap(validate), None\n",
                    older + "if True:\n    validate = wrap(validate)\n",
                    older + "try:\n    " + good.replace("\n    ", "\n        ") + "finally:\n    pass\n",
                    older + "from helpers import check as validate\n",
                    older + "def rebind():\n    global validate\n    validate = wrap(validate)\nrebind()\n",
                    older + "X = [(validate := wrap(f)) for f in [validate]]\n",
                    older + "def g(h=(validate := wrap(validate))):\n    pass\n",
                    older + "try:\n    pass\nexcept Exception as validate:\n    pass\n",
                    older + "match wrap(validate):\n    case validate:\n        pass\n",
                    older + "from helpers import *\n"):
        ProblemFile.model_validate(_file(None, validator=unclear))
    # Names bound in nested scopes, or a bare annotation, don't rebind it.
    for clear in (older + "def helper():\n    validate = 1\n",
                  older + "X = [validate for validate in range(3)]\n",
                  older + "class K:\n    validate = 1\n",
                  older + "validate: object\n",
                  "validate = None\n" + older):
        assert "the older `instance` form is gone" in _error(_file(None, validator=clear))
    for sig in ("actual, expected, args, probe_results, instance=None",
                "actual, **rest", "actual, expected, args, probe_results, *extra"):
        ProblemFile.model_validate(_file(None, validator=f"def validate({sig}):\n    return True\n"))


def test_a_validator_too_complex_to_compile_is_refused():
    """The harness's compile() fails on it too, so no case could be judged."""
    deep = "x = " + "1+" * 200000 + "1\n" + PROBE_VALIDATOR
    assert "too complex" in _error(_file(None, validator=deep))


def test_a_positional_only_probe_results_doesnt_count():
    """The harness passes it by keyword, which a positional-only parameter refuses."""
    positional = "def validate(actual, expected, args, probe_results, /):\n    return True\n"
    assert "the older `instance` form is gone" in _error(_file([DECODE], validator=positional))


def test_a_validator_with_a_wrong_solution_has_no_warnings():
    assert ProblemFile.model_validate(
        {**_file(None), "wrong_solutions": [WRONG]}).authoring_warnings() == []


def test_a_validator_with_no_wrong_solution_warns_per_language():
    """Only a wrong solution proves a validator rejects anything, so a language
    without one is named."""
    data = _file(None, languages=RUST_TOO, validator={"python": PROBE_VALIDATOR, "rust": "fn validate() {}"})
    data["wrong_solutions"] = [{"title": "W", "code": {"python": "class Codec: ..."}}]
    [warned] = ProblemFile.model_validate(data).authoring_warnings()
    assert "no wrong solution for ['rust']" in warned


def test_the_seed_cli_prints_authoring_warnings(tmp_path, capsys):
    from app.cli import validate
    (tmp_path / "p.json").write_text(json.dumps(_file(None)))
    assert validate(tmp_path) == 0
    assert "p.json: its custom validator has no wrong solution for ['python']" in capsys.readouterr().err
