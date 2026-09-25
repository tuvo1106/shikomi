"""Submission + judging schemas (the Run/Submit request and verdict shapes).

Note the code-size cap is enforced twice, on purpose: here at the schema boundary
(`max_length=_MAX_CODE`, so an oversized body is a clean 422) and again in the
service by byte length (a multibyte string can pass a char-count check yet exceed
the byte budget). Belt and suspenders around untrusted input.
"""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.config import get_settings

_MAX_CODE = get_settings().max_code_bytes


class SubmissionCreate(BaseModel):
    """The Run/Submit request body: which problem, and the code to judge."""

    problem_id: uuid.UUID
    code: str = Field(min_length=1, max_length=_MAX_CODE)


class SubmissionAccepted(BaseModel):
    """The immediate 202 reply — an id to poll and the initial (pending) status."""

    id: uuid.UUID
    status: str


class SubmissionOut(BaseModel):
    """A submission's full state (the poll response).

    `verdict_detail` is the judge's per-case breakdown (JSON, shape varies).
    `runtime_percentile` is not stored — the router computes and attaches it on an
    accepted verdict — hence the default of None for every other state.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    problem_id: uuid.UUID
    status: str
    code: str
    verdict_detail: Any | None = None
    runtime_ms: float | None = None
    is_run: bool
    created_at: datetime
    # % of accepted submissions this one is at least as fast as (accepted only).
    runtime_percentile: float | None = None


class SubmissionListItem(BaseModel):
    """A compact row for the Submissions history (no code/verdict payload)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    runtime_ms: float | None = None
    created_at: datetime


class SubmissionListResponse(BaseModel):
    """Wrapper around the history list (room to add paging later without a breaking
    change to the response shape)."""

    items: list[SubmissionListItem]


class RuntimeDistribution(BaseModel):
    """The runtime histogram for the success modal: `buckets` are counts per
    equal-width bin from `lo`..`hi`, `total` is the sample size."""

    buckets: list[int]  # count of accepted submissions per runtime bin, low → high
    lo: float
    hi: float
    total: int
