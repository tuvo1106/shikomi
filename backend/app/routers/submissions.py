"""Submit/Run endpoints, verdict polling, and the runtime-stats reads (§4.3).

Submit and Run both `202 Accepted` immediately with a pending id — judging is
async, so there's nothing to return yet — and the client polls
`GET /submissions/{id}` until the status is terminal. Both write endpoints depend
on `require_verified` (defense in depth: the UI also gates, but never trust the
client) so unverified accounts can't spend judge capacity. Reads use plain
`current_user`; ownership is enforced in the service.
"""
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.queue import Queue, get_queue
from app.schemas.submission import (
    RuntimeDistribution,
    SubmissionAccepted,
    SubmissionCreate,
    SubmissionListResponse,
    SubmissionOut,
)
from app.security import current_user, require_verified
from app.services import submission_service

router = APIRouter(tags=["submissions"])


@router.post("/submissions", status_code=202, response_model=SubmissionAccepted)
async def create_submission(body: SubmissionCreate, user=Depends(require_verified),
                            session: AsyncSession = Depends(get_session),
                            queue: Queue = Depends(get_queue)):
    """POST /submissions — judge against *all* cases; saved to history. 202 pending."""
    sub = await submission_service.create_submission(
        session, queue, user, body.problem_id, body.code, mode="submit")
    return SubmissionAccepted(id=sub.id, status=sub.status)


@router.post("/run", status_code=202, response_model=SubmissionAccepted)
async def run_submission(body: SubmissionCreate, user=Depends(require_verified),
                         session: AsyncSession = Depends(get_session),
                         queue: Queue = Depends(get_queue)):
    """POST /run — judge against *sample* cases only; unthrottled, not saved. 202."""
    sub = await submission_service.create_submission(
        session, queue, user, body.problem_id, body.code, mode="run")
    return SubmissionAccepted(id=sub.id, status=sub.status)


@router.get("/submissions/{submission_id}", response_model=SubmissionOut)
async def get_submission(submission_id: uuid.UUID, user=Depends(current_user),
                         session: AsyncSession = Depends(get_session),
                         queue: Queue = Depends(get_queue)):
    """GET /submissions/{id} — the submission (the poll target).

    On an accepted verdict, enriches the response with `runtime_percentile` so the
    client can show "beats X%" without a second request.
    """
    sub = await submission_service.get_submission(session, user, submission_id)
    out = SubmissionOut.model_validate(sub)
    if sub.status == "accepted" and sub.runtime_ms is not None:
        out.runtime_percentile = await submission_service.runtime_percentile(
            session, queue, sub.problem_id, sub.runtime_ms)
    return out


@router.delete("/submissions/{submission_id}", status_code=204)
async def delete_submission(submission_id: uuid.UUID, user=Depends(current_user),
                            session: AsyncSession = Depends(get_session)):
    """DELETE /submissions/{id} — remove one of your own submissions (204)."""
    await submission_service.delete_submission(session, user, submission_id)


@router.get("/problems/{slug}/submissions", response_model=SubmissionListResponse)
async def list_submissions(slug: str, user=Depends(current_user),
                           session: AsyncSession = Depends(get_session)):
    """GET /problems/{slug}/submissions — the caller's Submit history for a problem."""
    items = await submission_service.list_user_submissions(session, user, slug)
    return SubmissionListResponse(items=items)


@router.get("/submissions/{submission_id}/distribution", response_model=RuntimeDistribution)
async def submission_distribution(submission_id: uuid.UUID, user=Depends(current_user),
                                  session: AsyncSession = Depends(get_session)):
    """GET /submissions/{id}/distribution — runtime histogram for the success modal."""
    sub = await submission_service.get_submission(session, user, submission_id)
    return await submission_service.runtime_distribution(session, sub.problem_id)
