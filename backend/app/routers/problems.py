"""Public problem endpoints: browse the catalog, read a problem, read solutions.

Routers are deliberately thin "controllers": parse/validate the request, depend
on `current_user` for auth, delegate to `problem_service`, shape the response.
The logic lives in the service so it's reusable and unit-testable without HTTP.
Query params use `Query(...)` with bounds (`ge`/`le`) so bad pagination is a
clean 422 before any handler code runs.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas.problem import (
    Difficulty,
    ProblemDetail,
    ProblemFacets,
    ProblemListItem,
    ProblemListResponse,
    UserStatus,
)
from app.schemas.solution import SolutionsResponse
from app.security import current_user
from app.services import problem_service

router = APIRouter(prefix="/problems", tags=["problems"])


@router.get("", response_model=ProblemListResponse)
async def list_problems(
    difficulty: Difficulty | None = None,
    tag: str | None = None,
    collection: str | None = None,
    search: str | None = None,
    status: UserStatus | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    user=Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """GET /problems — published problems with the caller's status, filtered/paged."""
    items, total = await problem_service.list_problems(
        session, user.id, difficulty=difficulty,
        tag=tag, collection=collection, search=search, status_filter=status,
        page=page, page_size=page_size)
    return ProblemListResponse(items=items, total=total)


@router.get("/facets", response_model=ProblemFacets)
async def problem_facets(
    _user=Depends(current_user),
    session: AsyncSession = Depends(get_session),
):
    """GET /problems/facets — the full tag/collection vocabulary for the filters.

    Declared *before* `/{slug}`: FastAPI matches routes in declaration order, so
    the path-param route would otherwise swallow "facets" and 404 as a missing
    problem.
    """
    return ProblemFacets(**await problem_service.list_filter_facets(session))


@router.get("/{slug}", response_model=ProblemDetail)
async def get_problem(slug: str, user=Depends(current_user),
                      session: AsyncSession = Depends(get_session)):
    """GET /problems/{slug} — the problem detail (sample cases only). 404 if draft."""
    return await problem_service.get_problem_detail(session, user.id, slug)


@router.get("/{slug}/next", response_model=ProblemListItem | None)
async def next_problem(slug: str, user=Depends(current_user),
                       session: AsyncSession = Depends(get_session)):
    """GET /problems/{slug}/next — the next unsolved problem after `slug`
    in list order (wrapping around), or `null` when there isn't one. Drives the
    accepted-submit modal's "Next problem" button."""
    return await problem_service.next_problem(session, user.id, slug)


@router.get("/{slug}/solutions", response_model=SolutionsResponse)
async def get_solutions(slug: str, _user=Depends(current_user),
                        session: AsyncSession = Depends(get_session)):
    """GET /problems/{slug}/solutions — editorial write-ups for a published problem."""
    return SolutionsResponse(items=await problem_service.get_solutions(session, slug))
