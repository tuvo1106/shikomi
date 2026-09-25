"""Problem reads (list/detail), derived solve-status, and the seed-loading writes.

Two audiences share this module. API reads (`list_problems`,
`get_problem_detail`, `get_solutions`) only ever touch *published* problems and
carefully expose only sample test cases — hidden cases never leave the server.
The one write, `upsert_problem`, backs `app.cli seed`, which is how an operator
loads problems: there is no write API.
A user's "solved / attempted / unsolved" status isn't stored; it's *derived*
from their submissions on the fly (see `user_statuses`). (DESIGN.md §4.2, §7.1)
"""
import re

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.errors import APIError
from app.models import Problem, Solution, Submission, TestCase
from app.schemas.problem import (
    ParamSpec,
    ProblemDetail,
    ProblemFile,
    ProblemListItem,
    SampleCase,
)
from app.schemas.solution import SolutionOut


def slugify(title: str) -> str:
    """Turn a title into a URL-safe slug ("Vending Machine!" → "vending-machine").

    Lowercases, collapses any run of non-alphanumerics to a single hyphen, and
    trims stray hyphens. Falls back to "problem" if nothing survives (e.g. an
    all-symbol title), so a slug is never empty.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "problem"


def _escape_like(term: str) -> str:
    """Escape ILIKE wildcards in a user-supplied search term.

    `%` (any run of characters) and `_` (any single character) are SQL
    LIKE/ILIKE wildcards — without escaping, searching for a title containing
    a literal `%` or `_` (e.g. "50% Off" or "snake_case") would silently behave
    as a wildcard match instead of a literal one, over- or under-matching in
    ways that have nothing to do with what the user typed. The backslash
    itself is escaped first (Postgres's default LIKE/ILIKE escape character),
    or an already-escaped `\\%` in the input would be misread as a literal
    backslash followed by a wildcard.
    """
    return term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


async def user_statuses(session: AsyncSession, user_id, problem_ids) -> dict:
    """Derive each problem's solved/attempted/unsolved status for a user (§4.2).

    One grouped query does it: for the user's real submissions (`is_run` false)
    on these problems, `bool_or(status == 'accepted')` per problem tells us if any
    was accepted. So a problem with ≥1 accepted submission is *solved*, one with
    submissions but none accepted is *attempted*, and one with no submissions is
    *unsolved*. Computing this on read (vs a stored flag) means it can never drift
    out of sync with the submission history.
    """
    if not problem_ids:
        return {}
    rows = (await session.execute(
        select(Submission.problem_id,
               func.bool_or(Submission.status == "accepted").label("solved"))
        .where(Submission.user_id == user_id,
               Submission.is_run.is_(False),
               Submission.problem_id.in_(problem_ids))
        .group_by(Submission.problem_id))).all()
    seen = {pid: ("solved" if solved else "attempted") for pid, solved in rows}
    return {pid: seen.get(pid, "unsolved") for pid in problem_ids}


async def list_problems(session, user_id, *, difficulty=None, tag=None,
                        collection=None, search=None, status_filter=None, page=1, page_size=25):
    """List published problems with the caller's per-problem status, filtered.

    Pagination happens in **SQL** — a `COUNT` for the total plus `LIMIT/OFFSET`
    for the page — so the cost is O(page_size), not O(catalog): we never load the
    whole published set just to slice one page.

    `status_filter` (solved/attempted/unsolved) is *derived* from submissions, not
    a column, so when it's set we LEFT JOIN a per-problem "any accepted?" aggregate
    and filter on it in the same query — that keeps the `COUNT` and the page in
    agreement (filtering in Python after a SQL page would miscount and drop rows).
    Returns
    `(page_of_items, total_matched)`. Keyword-only args (after `*`) force call
    sites to name filters, which keeps them readable.
    """
    stmt = select(Problem).where(Problem.is_published.is_(True))
    if difficulty:
        stmt = stmt.where(Problem.difficulty == difficulty)
    if tag:
        stmt = stmt.where(Problem.tags.contains([tag]))
    if collection:
        stmt = stmt.where(Problem.collections.contains([collection]))
    if search:
        stmt = stmt.where(Problem.title.ilike(f"%{_escape_like(search)}%"))

    if status_filter:
        # Per-problem submission aggregate for this user (real submissions only):
        # a row's `solved` = any accepted. After the LEFT JOIN, solved True →
        # "solved", a present row with solved False → "attempted", and no row
        # (NULL) → "unsolved". Filtering here — rather than in Python after paging —
        # is what lets LIMIT/OFFSET and COUNT see the already-filtered set.
        agg = (select(Submission.problem_id.label("pid"),
                      func.bool_or(Submission.status == "accepted").label("solved"))
               .where(Submission.user_id == user_id, Submission.is_run.is_(False))
               .group_by(Submission.problem_id)).subquery()
        stmt = stmt.outerjoin(agg, Problem.id == agg.c.pid)
        if status_filter == "solved":
            stmt = stmt.where(agg.c.solved.is_(True))
        elif status_filter == "attempted":
            stmt = stmt.where(agg.c.solved.is_(False))
        else:  # unsolved
            stmt = stmt.where(agg.c.pid.is_(None))

    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    # Alphabetical by title, not insertion order — created_at reflects when a
    # problem happened to be seeded/authored, which is arbitrary from a reader's
    # perspective and reshuffles the list every time a new batch is loaded.
    # `title` isn't unique, so `id` breaks ties — together they're a total order,
    # so page boundaries stay stable even when two problems share a title.
    page_rows = (await session.execute(
        stmt.order_by(Problem.title, Problem.id)
        .limit(page_size).offset((page - 1) * page_size))).scalars().all()

    statuses = await user_statuses(session, user_id, [p.id for p in page_rows])
    items = [
        ProblemListItem(id=p.id, slug=p.slug, title=p.title, difficulty=p.difficulty,
                        tags=list(p.tags), user_status=statuses[p.id])
        for p in page_rows
    ]
    return items, total


async def next_problem(session: AsyncSession, user_id, slug: str):
    """The next problem worth opening after `slug`, or None if there isn't one.

    "Next" means the following problem in the list's own order (`title`, then `id`),
    skipping anything the viewer has already **solved** (an attempted-but-unsolved
    one is still fair game), and wrapping back to
    the start of the list once it runs off the end — so finishing the last problem
    still points somewhere useful. It returns None only when nothing else qualifies
    (everything else is solved).

    This lives on the server, not in the client, because "after" has to use the same
    ordering as the list's `ORDER BY`: Postgres collation and JavaScript string
    comparison disagree on case and punctuation, so a client-side "the one after
    this title" can skip or repeat problems. The row comparison
    `(title, id) > (current.title, current.id)` is evaluated by the database, with the
    same collation as the sort.

    Raises:
        APIError(404): `slug` isn't a published problem.
    """
    current = (await session.execute(
        select(Problem).where(Problem.slug == slug, Problem.is_published.is_(True))
    )).scalar_one_or_none()
    if current is None:
        raise APIError(404, "PROBLEM_NOT_FOUND", "Problem not found.")

    solved = (select(Submission.problem_id)
              .where(Submission.user_id == user_id, Submission.is_run.is_(False),
                     Submission.status == "accepted"))
    candidates = select(Problem).where(
        Problem.is_published.is_(True), Problem.id != current.id, Problem.id.not_in(solved))
    order = (Problem.title, Problem.id)

    after = candidates.where(
        tuple_(Problem.title, Problem.id) > tuple_(current.title, current.id))
    nxt = (await session.execute(after.order_by(*order).limit(1))).scalar_one_or_none()
    if nxt is None:  # ran off the end of the list: wrap to the first that qualifies
        nxt = (await session.execute(candidates.order_by(*order).limit(1))).scalar_one_or_none()
    if nxt is None:
        return None

    status = (await user_statuses(session, user_id, [nxt.id]))[nxt.id]
    return ProblemListItem(id=nxt.id, slug=nxt.slug, title=nxt.title, difficulty=nxt.difficulty,
                           tags=list(nxt.tags), user_status=status)


async def list_filter_facets(session: AsyncSession) -> dict[str, list[str]]:
    """Every distinct tag and collection across the published catalog, sorted.

    Exists because the filter dropdowns can't be built from the page the client
    happens to be looking at: `list_problems` returns one `page_size` slice, so a
    tag used only by problems on other pages would be unselectable (§4.2).

    `tags`/`collections` are Postgres `text[]`, so the distinct set comes from
    `unnest` in SQL rather than by loading every row and flattening in Python —
    the same "don't pull the catalog to answer a question about it" rule
    `list_problems` follows for paging.
    """
    async def distinct(column) -> list[str]:
        value = func.unnest(column).label("value")
        stmt = (select(value)
                .where(Problem.is_published.is_(True))
                .distinct()
                .order_by(value))
        return list((await session.execute(stmt)).scalars().all())

    return {"tags": await distinct(Problem.tags),
            "collections": await distinct(Problem.collections)}


async def _get_published(session: AsyncSession, slug: str) -> Problem:
    """Load a published problem by slug with its cases + solutions eager-loaded.

    `selectinload` fetches the children in one extra query up front, avoiding the
    N+1 lazy-loads that would otherwise fire later (and which don't even work off
    the event loop in async SQLAlchemy). An unpublished/missing slug is a 404 —
    drafts are invisible to the public API.
    """
    problem = (await session.execute(
        select(Problem)
        .where(Problem.slug == slug, Problem.is_published.is_(True))
        .options(selectinload(Problem.test_cases), selectinload(Problem.solutions))
    )).scalar_one_or_none()
    if problem is None:
        raise APIError(404, "NOT_FOUND", "Problem not found.")
    return problem


async def get_problem_detail(session: AsyncSession, user_id, slug: str) -> ProblemDetail:
    """Build the public problem view — statement, params, *sample* cases, status.

    Only `is_sample` cases are surfaced (hidden cases stay server-side), and
    `has_solutions` is a boolean so the UI can show/hide the Solutions tab without
    leaking the solutions themselves here.
    """
    problem = await _get_published(session, slug)
    samples = [SampleCase(ordinal=tc.ordinal, input=tc.input, expected=tc.expected)
               for tc in problem.test_cases if tc.is_sample]
    status = (await user_statuses(session, user_id, [problem.id]))[problem.id]
    return ProblemDetail(
        id=problem.id, slug=problem.slug, title=problem.title, difficulty=problem.difficulty,
        statement_md=problem.statement_md, starter_code=problem.starter_code,
        kind=problem.kind, language=problem.language,
        function_name=problem.function_name, class_name=problem.class_name,
        params=[ParamSpec(**p) for p in problem.params], return_type=problem.return_type,
        tags=list(problem.tags), constraints=list(problem.constraints), sample_cases=samples,
        has_solutions=len(problem.solutions) > 0, user_status=status,
    )


async def get_solutions(session: AsyncSession, slug: str) -> list[SolutionOut]:
    """Editorial solutions for a published problem (ordinal order).

    Open to every signed-in user; the client treats them as spoilers (DESIGN.md
    §6.3), but the server doesn't gate them.
    """
    problem = await _get_published(session, slug)
    return [SolutionOut.model_validate(s) for s in problem.solutions]


# --- seed loading -----------------------------------------------------------
# Called only by `app.cli seed` (operator-run, never over HTTP). They bypass the
# publish gate and write hidden cases.

async def upsert_problem(session: AsyncSession, data: ProblemFile) -> tuple[Problem, str]:
    """Create or overwrite one problem, with its test cases and solutions, from a file.

    Keyed on `slug`, derived from the title when the file omits it (so a slug-less
    file still maps to the same row on every re-seed). An update is a full replace:
    every metadata field is overwritten and the cases and solutions are swapped
    wholesale (delete-then-insert), because a problem file is always the complete
    desired state, never a patch.

    **Does not commit.** The caller owns the transaction, so `app.cli seed` can load
    a whole directory atomically: either every file lands or none does. All
    validation (schema, unique ordinals, at least one case, the judge budget) has
    already happened in `ProblemFile`, before anything here writes.

    Returns:
        The problem row and `"created"` or `"updated"`.
    """
    slug = data.slug or slugify(data.title)
    fields = data.model_dump(exclude={"slug", "test_cases", "solutions"})
    problem = await session.scalar(select(Problem).where(Problem.slug == slug))
    if problem is None:
        problem = Problem(slug=slug, **fields)
        session.add(problem)
        await session.flush()  # assign problem.id for the child rows below
        action = "created"
    else:
        for field, value in fields.items():
            setattr(problem, field, value)
        await session.execute(delete(TestCase).where(TestCase.problem_id == problem.id))
        await session.execute(delete(Solution).where(Solution.problem_id == problem.id))
        action = "updated"
    session.add_all(
        TestCase(problem_id=problem.id, **tc.model_dump()) for tc in data.test_cases)
    session.add_all(
        Solution(problem_id=problem.id, **sol.model_dump()) for sol in data.solutions)
    await session.flush()
    return problem, action
