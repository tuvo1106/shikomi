"""The accounts worker's startup check for stored validators it would refuse
(worker/stale_validators.py; ADR-0007 step 6)."""
from sqlalchemy import update

from app.models import Problem
from worker import stale_validators

OLDER = "def validate(actual, expected, args, instance=None):\n    return True\n"
PROBE = "def validate(actual, expected, args, probe_results):\n    return True\n"


async def _set_validator(session_factory, slug, code):
    async with session_factory() as s:
        await s.execute(update(Problem).where(Problem.slug == slug).values(
            comparison={"mode": "custom_validator", "validator_code": code}))
        await s.commit()


async def test_names_only_the_problems_the_judge_would_refuse(session_factory, make_problem,
                                                              monkeypatch, caplog):
    await make_problem(slug="older")
    await make_problem(slug="converted")
    await make_problem(slug="fixed-answer")
    await make_problem(slug="rust-only")
    await _set_validator(session_factory, "older", {"python": OLDER})
    await _set_validator(session_factory, "converted", {"python": PROBE})
    await _set_validator(session_factory, "rust-only", {"rust": "fn validate() {}"})
    await make_problem(slug="unreadable")
    await _set_validator(session_factory, "unreadable", 5)

    async with session_factory() as s:
        assert await stale_validators.stale_validator_slugs(s) == ["older", "unreadable"]

    monkeypatch.setattr(stale_validators, "SessionLocal", session_factory)
    with caplog.at_level("ERROR", logger="worker.stale_validators"):
        await stale_validators.warn_about_stale_validators()
    assert "2 problem(s)" in caplog.text and "older, unreadable" in caplog.text


async def test_a_failed_check_never_stops_the_worker(monkeypatch, caplog):
    def broken():
        raise RuntimeError("no database")
    monkeypatch.setattr(stale_validators, "SessionLocal", broken)
    await stale_validators.warn_about_stale_validators()
    assert "could not check stored custom validators" in caplog.text
