"""Editorial-solution schemas.

`SolutionOut` reads from ORM objects (`from_attributes`) for display; `SolutionIn`
is the write side, one entry of a seed file's ordered `solutions` list (loaded
wholesale by `problem_service.upsert_problem`). The `*_md` fields carry Markdown,
the `*_complexity` fields carry LaTeX.
"""
import uuid

from pydantic import BaseModel, ConfigDict


class SolutionOut(BaseModel):
    """One editorial solution as returned to the client (from an ORM row)."""


    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ordinal: int
    title: str
    intuition_md: str
    algorithm_md: str = ""
    code: str
    time_complexity: str
    space_complexity: str
    time_complexity_reason: str = ""
    space_complexity_reason: str = ""


class SolutionsResponse(BaseModel):
    items: list[SolutionOut]


class SolutionIn(BaseModel):
    ordinal: int
    title: str
    intuition_md: str
    algorithm_md: str = ""
    code: str
    time_complexity: str
    space_complexity: str
    time_complexity_reason: str = ""
    space_complexity_reason: str = ""
