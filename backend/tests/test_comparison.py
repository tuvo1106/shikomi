"""`app.comparison.for_language`: a problem's shared `comparison`, resolved for the
language a submission is judged in (docs/adr/0007-custom-validators-in-every-language.md).
"""
import pytest

from app.comparison import for_language, validator_codes


def test_a_fixed_answer_mode_passes_through_for_any_language():
    comparison = {"mode": "float_tolerance", "epsilon": 1e-3}
    assert for_language(comparison, "rust") == comparison


def test_no_comparison_is_exact():
    assert for_language(None, "python") == {"mode": "exact"}


def test_the_languages_validator_becomes_the_string_the_harness_expects():
    comparison = {"mode": "custom_validator", "validator_code": {"python": "p", "rust": "r"}}
    assert for_language(comparison, "rust") == {"mode": "custom_validator", "validator_code": "r"}
    # The problem row's own dict is left alone.
    assert comparison["validator_code"] == {"python": "p", "rust": "r"}


def test_a_row_seeded_before_the_map_is_pythons_validator():
    """Rows keep the bare string until they're re-seeded."""
    legacy = {"mode": "custom_validator", "validator_code": "p"}
    assert for_language(legacy, "python")["validator_code"] == "p"
    assert validator_codes(legacy) == {"python": "p"}
    with pytest.raises(ValueError, match="no validator for language 'rust'"):
        for_language(legacy, "rust")


def test_no_validator_for_the_language_is_an_error_never_a_fallback():
    """Handing Python source to the Rust harness can't judge anything."""
    with pytest.raises(ValueError, match="'rust'"):
        for_language({"mode": "custom_validator", "validator_code": {"python": "p"}}, "rust")
    with pytest.raises(ValueError):
        for_language({"mode": "custom_validator", "validator_code": {"python": " "}}, "python")
