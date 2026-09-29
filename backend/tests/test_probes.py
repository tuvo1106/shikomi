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


def test_probes_need_a_validator_that_takes_probe_results():
    """The harness sends probes only to the probe form, so an older-form validator
    would never see them."""
    older = "def validate(actual, expected, args, instance=None):\n    return True\n"
    assert "take 'probe_results'" in _error(_file([DECODE], validator=older))


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


def test_the_last_def_validate_is_the_one_checked_as_it_is_the_one_that_runs():
    older_then_probe = "def validate(actual, expected, args, instance=None):\n    return 1\n" + PROBE_VALIDATOR
    ProblemFile.model_validate(_file([DECODE], validator=older_then_probe))
    probe_then_older = PROBE_VALIDATOR + "def validate(actual, expected, args, instance=None):\n    return 1\n"
    assert "take 'probe_results'" in _error(_file([DECODE], validator=probe_then_older))


def test_a_positional_only_probe_results_doesnt_count():
    """The harness passes it by keyword, which a positional-only parameter refuses."""
    positional = "def validate(actual, expected, args, probe_results, /):\n    return True\n"
    assert "take 'probe_results'" in _error(_file([DECODE], validator=positional))


def test_an_older_form_operations_validator_loads_with_a_warning():
    """It still runs (in the child), so it isn't refused until problem sets that
    use it are converted, but the author hears why it's weaker."""
    older = "def validate(actual, expected, args, instance=None):\n    return True\n"
    warned = ProblemFile.model_validate(_file(None, validator=older)).authoring_warnings()
    assert len(warned) == 1 and "can read `expected`" in warned[0]
    assert ProblemFile.model_validate(_file(None)).authoring_warnings() == []


def test_the_seed_cli_prints_authoring_warnings(tmp_path, capsys):
    from app.cli import validate
    older = "def validate(actual, expected, args, instance=None):\n    return True\n"
    (tmp_path / "p.json").write_text(json.dumps(_file(None, validator=older)))
    assert validate(tmp_path) == 0
    assert "p.json: its operations validator uses the older `instance` form" in capsys.readouterr().err
