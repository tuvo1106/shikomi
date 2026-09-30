"""A startup check for custom validators stored in a form the judge refuses.

The judge refuses a Python validator its keyword call can't bind to, including
the older `validate(actual, expected, args, instance=None)` form (ADR-0007 step
6). Problems are stored rows, loaded by `app.cli seed`, so a problem set seeded
before its validators were converted keeps the old form until it's re-seeded,
and then every submission to it is a `judge_error`. Code can't be rewritten by a
data migration, so instead the accounts worker (always on, one replica) names
those problems in its log when it starts: the operator sees the list the moment the upgrade is running, rather
than learning it from users one problem at a time.
"""
import logging

from sqlalchemy import select

from app.comparison import validator_codes
from app.db import SessionLocal
from app.models import Problem
from app.schemas.problem import validator_call_problem

logger = logging.getLogger(__name__)


async def stale_validator_slugs(session) -> list[str]:
    """Slugs of stored problems whose Python validator the judge would refuse.

    A row whose `validator_code` is neither a string nor a map is listed too (the
    judge can't use it either), rather than aborting the scan and hiding the rest.
    A row the check itself fails on is logged and skipped: the check is unsure,
    so it doesn't claim the judge refuses it.
    """
    rows = await session.execute(
        select(Problem.slug, Problem.comparison)
        .where(Problem.comparison["mode"].astext == "custom_validator"))
    stale = []
    for slug, comparison in rows:
        try:
            code = validator_codes(comparison).get("python", "")
        except ValueError:  # neither a string nor a map
            stale.append(slug)
            continue
        try:
            if validator_call_problem(code) is not None:
                stale.append(slug)
        except Exception:  # noqa: BLE001 - a bug in the check, never fatal
            logger.exception("could not check the custom validator of %s", slug)
    return sorted(stale)


async def warn_about_stale_validators() -> None:
    """Log an error naming every stored problem whose validator the judge refuses.

    Best-effort: a failure here must not stop the worker.
    """
    try:
        async with SessionLocal() as session:
            slugs = await stale_validator_slugs(session)
    except Exception:  # noqa: BLE001 - a startup check, never fatal
        logger.exception("could not check stored custom validators")
        return
    if slugs:
        logger.error(
            "%d problem(s) have a custom validator the judge refuses, so every submission to "
            "them is a judge_error: %s. It must be `def validate(actual, expected, args, "
            "probe_results)`, callable with just those four by keyword (the older `instance` "
            "form is the usual cause); convert and re-seed "
            "(docs/adr/0007-custom-validators-in-every-language.md).", len(slugs), ", ".join(slugs))
