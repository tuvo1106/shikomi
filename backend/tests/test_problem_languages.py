"""Authoring rules for multi-language problem files (`ProblemFile`, DESIGN.md §7.1,
docs/adr/0005-multi-language-problems.md).

Every rule that ties a language to the shared `kind`/`comparison`/limits is
checked per variant, because any variant can be submitted to. These run with no
database: the rules live entirely in the schema.
"""
import pytest
from pydantic import ValidationError

from app.schemas.problem import ProblemFile

CASES = [{"ordinal": 0, "input": [1], "expected": 1, "is_sample": True}]


def _variant(language, **extra):
    return {"language": language, "function_name": "f", "starter_code": f"{language} f",
            "params": [{"name": "x", "type": "int"}], **extra}


def _file(*variants, **extra):
    return {"title": "T", "difficulty": "easy", "statement_md": "s",
            "languages": list(variants) or [_variant("python")], "memory_limit_mb": 256,
            "test_cases": CASES, **extra}


def _error(data) -> str:
    with pytest.raises(ValidationError) as exc:
        ProblemFile.model_validate(data)
    return str(exc.value)


def test_three_languages_load_in_order():
    p = ProblemFile.model_validate(_file(_variant("rust"), _variant("python"), _variant("js")))
    assert [v.language for v in p.languages] == ["rust", "python", "js"]


def test_legacy_top_level_fields_become_one_variant():
    p = ProblemFile.model_validate({
        "title": "T", "difficulty": "easy", "statement_md": "s", "language": "js",
        "function_name": "f", "starter_code": "var f", "test_cases": CASES,
        "solutions": [{"ordinal": 0, "title": "S", "intuition_md": "i", "code": "c",
                       "time_complexity": "O(1)", "space_complexity": "O(1)"}]})
    [v] = p.languages
    assert (v.language, v.function_name, v.starter_code) == ("js", "f", "var f")
    assert p.solutions[0].code == {"js": "c"}


def test_legacy_language_defaults_to_python():
    p = ProblemFile.model_validate({
        "title": "T", "difficulty": "easy", "statement_md": "s", "function_name": "f",
        "starter_code": "def f", "test_cases": CASES})
    assert [v.language for v in p.languages] == ["python"]


def test_mixing_both_forms_is_ambiguous():
    assert "not both" in _error(_file(starter_code="def f"))


def test_at_least_one_language():
    _error({**_file(), "languages": []})


def test_a_language_appears_once():
    assert "only once" in _error(_file(_variant("python"), _variant("python")))


def test_every_variant_needs_the_name_its_kind_calls():
    assert "language 'js': function_name is required" in _error(
        _file(_variant("python"), _variant("js", function_name=None)))


def test_function_mode_only_languages_are_checked_per_variant():
    """Adding Rust to an operations problem must fail even though Python is fine."""
    ops = {"function_name": None, "class_name": "C"}
    assert "language 'rust' only supports kind 'function'" in _error(
        _file(_variant("python", **ops), _variant("rust", **ops), kind="operations"))


def test_every_language_declares_the_same_number_of_params():
    """The cases are shared and positional, so a variant with a different arity
    could never match them."""
    assert "same number of params" in _error(
        _file(_variant("python"), _variant("rust", params=[])))


def test_the_shared_memory_limit_must_fit_every_language():
    assert "language 'rust' needs memory_limit_mb" in _error(
        _file(_variant("python"), _variant("rust"), memory_limit_mb=64))


def test_custom_validator_needs_python_to_be_the_only_language():
    comparison = {"mode": "custom_validator", "validator_code": "def validate(): ..."}
    ProblemFile.model_validate(_file(comparison=comparison))
    assert "only language" in _error(
        _file(_variant("python"), _variant("js"), comparison=comparison))


def test_sql_problems_only_take_mysql_variants():
    sql = {"function_name": None, "params": []}
    assert "must be set together" in _error(
        _file(_variant("mysql", **sql), _variant("python", **sql), kind="sql"))


def _solution(code, title="S"):
    return {"ordinal": 0, "title": title, "intuition_md": "i", "code": code,
            "time_complexity": "O(1)", "space_complexity": "O(1)"}


def test_a_string_solution_is_ambiguous_with_several_languages():
    assert "map of language to code" in _error(
        _file(_variant("python"), _variant("rust"), solutions=[_solution("c")]))


def test_solution_code_only_for_declared_languages():
    assert "doesn't list" in _error(
        _file(_variant("python"), solutions=[_solution({"python": "p", "go": "g"})]))


def test_every_language_needs_some_solution():
    """Each language needs a reference solution, or nothing proves its
    signature and harness can pass the shared cases."""
    assert "no solution has code for ['rust']" in _error(
        _file(_variant("python"), _variant("rust"), solutions=[_solution({"python": "p"})]))


def test_a_solution_may_cover_only_some_languages():
    p = ProblemFile.model_validate(_file(
        _variant("python"), _variant("rust"),
        solutions=[_solution({"python": "p", "rust": "r"}),
                   {**_solution({"rust": "r2"}, title="Rust only"), "ordinal": 1}]))
    assert p.solutions[1].code == {"rust": "r2"}
