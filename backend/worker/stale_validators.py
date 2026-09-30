"""A startup check for custom validators stored in a form the judge refuses.

The judge refuses a Python validator its keyword call can't bind to, including
the older `validate(actual, expected, args, instance=None)` form (ADR-0007 step
6). Problems are stored rows, loaded by `app.cli seed`, so a problem set seeded
before its validators were converted keeps the old form until it's re-seeded,
and then every submission to it is a `judge_error`. Code can't be rewritten by a
data migration, so instead the judge worker names those problems in its log when
it starts: the operator sees the list the moment the upgrade is running, rather
than learning it from users one problem at a time.
"""
import logging

from sqlalchemy import select

from app.comparison import validator_codes
from app.db import SessionLocal
from app.models import Problem
from app.schemas.problem import binds_validator_call

logger = logging.getLogger(__name__)


async def stale_validator_slugs(session) -> list[str]:
    """Slugs of stored problems whose Python validator the judge would refuse."""
    rows = await session.execute(select(Problem.slug, Problem.comparison))
    return sorted(
        slug for slug, comparison in rows
        if (comparison or {}).get("mode") == "custom_validator"
        and binds_validator_call(validator_codes(comparison).get("python", "")) is False)


async def warn_about_stale_validators() -> None:
    """Log an error naming every stored problem whose validator the judge refuses.

    Best-effort: a failure here must not stop the worker from judging.
    """
    try:
        async with SessionLocal() as session:
            slugs = await stale_validator_slugs(session)
    except Exception:  # noqa: BLE001 - a startup check, never fatal
        logger.exception("could not check stored custom validators")
        return
    if slugs:
        logger.error(
            "%d problem(s) have a custom validator in the older `instance` form, which the "
            "judge now refuses (every submission is a judge_error): %s. Convert them to "
            "`validate(actual, expected, args, probe_results)` and re-seed "
            "(docs/adr/0007-custom-validators-in-every-language.md).", len(slugs), ", ".join(slugs))
