"""Editorial-solution schemas.

`SolutionOut` is the read side, built by `problem_service.get_solutions`; `SolutionIn`
is the write side, one entry of a seed file's ordered `solutions` list (loaded
wholesale by `problem_service.upsert_problem`). The `*_md` fields carry Markdown,
the `*_complexity` fields carry LaTeX. `WrongSolutionIn` is a seed file's
`wrong_solutions`: code the judge must reject, checked by the seed tests and never
loaded.
"""
import uuid

from pydantic import BaseModel, Field


class SolutionOut(BaseModel):
    """One editorial solution as returned to the client.

    `code` maps language to reference code. It covers only the languages this
    solution was written in, which may be fewer than the problem offers.
    """


    id: uuid.UUID
    ordinal: int
    title: str
    intuition_md: str
    algorithm_md: str = ""
    code: dict[str, str]
    time_complexity: str
    space_complexity: str
    time_complexity_reason: str = ""
    space_complexity_reason: str = ""


class SolutionsResponse(BaseModel):
    items: list[SolutionOut]


class SolutionIn(BaseModel):
    """One solution in a problem file. `code` is a {language: code} map, or a
    plain string when the problem has one language (`ProblemFile` normalizes it
    to the map, so everything downstream sees a map)."""

    ordinal: int
    title: str
    intuition_md: str
    algorithm_md: str = ""
    code: dict[str, str] | str
    time_complexity: str
    space_complexity: str
    time_complexity_reason: str = ""
    space_complexity_reason: str = ""


class WrongSolutionIn(BaseModel):
    """A known-wrong solution in a problem file: code the judge must *not* accept.

    Reference solutions prove a problem accepts right answers; nothing proves it
    rejects wrong ones. For a fixed `expected` that's the comparison's job and is
    tested once, but a `custom_validator` is per-problem code, and one that returns
    `True` too eagerly accepts everything silently (docs/adr/0007-custom-validators-
    in-every-language.md). So a file lists plausible mistakes (the always-first index
    for a random pick, a shuffle that returns its input) and
    `judge/tests/test_seed_solutions.py` requires each to be judged `wrong_answer` on
    at least one case. They're test data only: never seeded, never shown.

    `title` names the mistake, since it's what a failing test prints. `code` is a
    {language: code} map, or a plain string when the problem has one language, as
    for `SolutionIn`.
    """

    title: str = Field(min_length=1)
    code: dict[str, str] | str
