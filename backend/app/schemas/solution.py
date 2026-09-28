"""Editorial-solution schemas.

`SolutionOut` is the read side, built by `problem_service.get_solutions`; `SolutionIn`
is the write side, one entry of a seed file's ordered `solutions` list (loaded
wholesale by `problem_service.upsert_problem`). The `*_md` fields carry Markdown,
the `*_complexity` fields carry LaTeX.
"""
import uuid

from pydantic import BaseModel


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
