"""Public problem endpoint tests: redaction, user_status, visibility (DESIGN.md §4.2, §10.2)."""


async def test_list_requires_auth(client):
    assert (await client.get("/api/v1/problems")).status_code == 401


async def test_list_returns_published_only(client, make_problem, user_headers):
    await make_problem(slug="pub", published=True)
    await make_problem(slug="draft", published=False)
    r = await client.get("/api/v1/problems", headers=user_headers)
    assert r.status_code == 200
    slugs = {i["slug"] for i in r.json()["items"]}
    assert slugs == {"pub"}
    assert r.json()["total"] == 1


async def test_detail_hides_unpublished(client, make_problem, user_headers):
    await make_problem(slug="draft", published=False)
    r = await client.get("/api/v1/problems/draft", headers=user_headers)
    assert r.status_code == 404


async def test_detail_exposes_only_sample_cases(client, make_problem, user_headers):
    await make_problem(slug="pair-sum")
    r = await client.get("/api/v1/problems/pair-sum", headers=user_headers)
    assert r.status_code == 200
    body = r.json()
    # One sample case exposed; the hidden case must not leak.
    assert len(body["sample_cases"]) == 1
    assert body["sample_cases"][0]["input"] == [[1, 6], 7]
    assert body["has_solutions"] is True
    # No hidden inputs/expected anywhere in the payload.
    assert "[3, 3]" not in str(body)


async def test_user_status_unsolved_by_default(client, make_problem, user_headers):
    await make_problem(slug="pair-sum")
    r = await client.get("/api/v1/problems/pair-sum", headers=user_headers)
    assert r.json()["user_status"] == "unsolved"


async def test_user_status_reflects_submissions(client, make_problem, make_user, make_submission):
    pid, _ = await make_problem(slug="pair-sum")
    user, headers = await make_user(email="s@example.com", username="solver")

    await make_submission(user["id"], pid, status="wrong_answer")
    r = await client.get("/api/v1/problems/pair-sum", headers=headers)
    assert r.json()["user_status"] == "attempted"

    await make_submission(user["id"], pid, status="accepted")
    r = await client.get("/api/v1/problems/pair-sum", headers=headers)
    assert r.json()["user_status"] == "solved"


async def test_run_submissions_excluded_from_status(client, make_problem, make_user, make_submission):
    pid, _ = await make_problem(slug="pair-sum")
    user, headers = await make_user(email="r@example.com", username="runner")
    await make_submission(user["id"], pid, status="accepted", is_run=True)
    r = await client.get("/api/v1/problems/pair-sum", headers=headers)
    assert r.json()["user_status"] == "unsolved"


async def test_status_filter(client, make_problem, make_user, make_submission):
    p1, _ = await make_problem(slug="solved-one")
    await make_problem(slug="untouched")
    user, headers = await make_user(email="f@example.com", username="filter")
    await make_submission(user["id"], p1, status="accepted")

    r = await client.get("/api/v1/problems", headers=headers, params={"status": "solved"})
    assert {i["slug"] for i in r.json()["items"]} == {"solved-one"}
    r = await client.get("/api/v1/problems", headers=headers, params={"status": "unsolved"})
    assert {i["slug"] for i in r.json()["items"]} == {"untouched"}


async def test_list_orders_alphabetically_by_title(client, make_problem, user_headers):
    # Inserted out of alphabetical order — the response must still come back
    # sorted by title, not by insertion/created_at order.
    await make_problem(slug="c", title="Charlie", with_solutions=False)
    await make_problem(slug="a", title="Alpha", with_solutions=False)
    await make_problem(slug="b", title="Bravo", with_solutions=False)
    r = await client.get("/api/v1/problems", headers=user_headers)
    assert [i["title"] for i in r.json()["items"]] == ["Alpha", "Bravo", "Charlie"]


async def test_pagination_paginates_in_sql(client, make_problem, user_headers):
    # Seed more than one page's worth; page_size=2 → 3 pages over 5 problems.
    for n in range(5):
        await make_problem(slug=f"p{n}", with_solutions=False)

    p1 = await client.get("/api/v1/problems", headers=user_headers,
                          params={"page": 1, "page_size": 2})
    p2 = await client.get("/api/v1/problems", headers=user_headers,
                          params={"page": 2, "page_size": 2})
    p3 = await client.get("/api/v1/problems", headers=user_headers,
                          params={"page": 3, "page_size": 2})

    # Every page reports the true total (not just what fit on the page)...
    assert p1.json()["total"] == p2.json()["total"] == 5
    for r in (p1, p2, p3):
        assert r.status_code == 200
    # ...pages are the expected sizes and fully disjoint (nothing lost or repeated).
    s1 = {i["slug"] for i in p1.json()["items"]}
    s2 = {i["slug"] for i in p2.json()["items"]}
    s3 = {i["slug"] for i in p3.json()["items"]}
    assert len(s1) == 2 and len(s2) == 2 and len(s3) == 1
    assert s1 | s2 | s3 == {f"p{n}" for n in range(5)}
    assert s1.isdisjoint(s2) and s1.isdisjoint(s3) and s2.isdisjoint(s3)


async def test_pagination_with_status_filter(client, make_problem, make_user, make_submission):
    # Status filter is derived from submissions but must still page + count in SQL.
    user, headers = await make_user(email="pg@example.com", username="pager")
    for n in range(3):
        pid, _ = await make_problem(slug=f"solved{n}", with_solutions=False)
        await make_submission(user["id"], pid, status="accepted")
    await make_problem(slug="never-touched", with_solutions=False)

    r1 = await client.get("/api/v1/problems", headers=headers,
                          params={"status": "solved", "page": 1, "page_size": 2})
    r2 = await client.get("/api/v1/problems", headers=headers,
                          params={"status": "solved", "page": 2, "page_size": 2})
    # The count reflects only matching (solved) rows, and paging is disjoint.
    assert r1.json()["total"] == 3
    solved = {i["slug"] for i in r1.json()["items"]} | {i["slug"] for i in r2.json()["items"]}
    assert solved == {f"solved{n}" for n in range(3)}
    assert all(i["user_status"] == "solved" for i in r1.json()["items"])


async def test_solutions_endpoint(client, make_problem, user_headers):
    await make_problem(slug="pair-sum", with_solutions=True)
    r = await client.get("/api/v1/problems/pair-sum/solutions", headers=user_headers)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["time_complexity"] == "O(n)"


async def test_difficulty_filter_validation(client, user_headers):
    r = await client.get("/api/v1/problems", headers=user_headers, params={"difficulty": "trivial"})
    assert r.status_code == 422


async def test_list_filters_match(client, make_problem, user_headers):
    await make_problem(slug="pair-sum")  # difficulty=easy, tags=[array], title "Pair Sum"
    for params in ({"difficulty": "easy"}, {"tag": "array"}, {"search": "Pair"}):
        r = await client.get("/api/v1/problems", headers=user_headers, params=params)
        assert {i["slug"] for i in r.json()["items"]} == {"pair-sum"}


async def test_list_filters_exclude(client, make_problem, user_headers):
    await make_problem(slug="pair-sum")
    r = await client.get("/api/v1/problems", headers=user_headers, params={"tag": "graph"})
    assert r.json()["items"] == []


async def test_list_filters_by_collection(client, make_problem, user_headers):
    await make_problem(slug="pair-sum", collections=["starter"])
    r = await client.get("/api/v1/problems", headers=user_headers,
                         params={"collection": "starter"})
    assert {i["slug"] for i in r.json()["items"]} == {"pair-sum"}
    r = await client.get("/api/v1/problems", headers=user_headers,
                         params={"collection": "gang-of-four"})
    assert r.json()["items"] == []


async def test_search_escapes_like_wildcards(client, make_problem, user_headers):
    """`%` and `_` are ILIKE wildcards (any run of characters / any single
    character) — a literal one in a search term must be escaped, or it
    matches far more (or less) than the literal text the user typed."""
    await make_problem(slug="discount", title="50% Off Deal", with_solutions=False)
    await make_problem(slug="unrelated", title="Binary Search Tree", with_solutions=False)
    await make_problem(slug="pair-sum", title="Pair Sum", with_solutions=False)

    # A bare "%" must match only the title that actually contains a literal
    # percent sign — an unescaped "%" is a wildcard matching *any* title
    # (including the ones with no "%" in them at all).
    r = await client.get("/api/v1/problems", headers=user_headers, params={"search": "%"})
    assert {i["slug"] for i in r.json()["items"]} == {"discount"}

    # A literal "_" must not act as a single-character wildcard and match
    # every title that merely has *some* character in that position.
    r = await client.get("/api/v1/problems", headers=user_headers, params={"search": "tw_"})
    assert r.json()["items"] == []


async def test_facets_requires_auth(client):
    assert (await client.get("/api/v1/problems/facets")).status_code == 401


async def test_facets_not_shadowed_by_the_slug_route(client, make_problem, user_headers):
    """`/problems/facets` must resolve as itself, not as `/problems/{slug}`.

    FastAPI matches routes in declaration order, so moving this handler below the
    path-param route would silently turn it into a lookup for a problem slugged
    "facets" — a 404 here, and a wrong 200 if such a problem ever existed.
    """
    await make_problem(slug="facets", title="Facets", tags=["array"])
    r = await client.get("/api/v1/problems/facets", headers=user_headers)
    assert r.status_code == 200
    assert set(r.json()) == {"tags", "collections"}


async def test_facets_span_the_whole_catalog_not_just_one_page(
    client, make_problem, user_headers
):
    """Why this endpoint exists: a tag used only beyond page 1.

    A dropdown built from the loaded page would leave `sql` (alphabetically
    last here) unselectable until you paged to it.
    """
    for i in range(3):
        await make_problem(slug=f"a-{i}", title=f"A{i}", tags=["array"])
    await make_problem(slug="z-last", title="Zebra", tags=["sql"])

    page1 = await client.get("/api/v1/problems?page_size=3", headers=user_headers)
    assert "sql" not in {t for i in page1.json()["items"] for t in i["tags"]}

    facets = await client.get("/api/v1/problems/facets", headers=user_headers)
    assert facets.json()["tags"] == ["array", "sql"]


async def test_facets_exclude_unpublished(client, make_problem, user_headers):
    """Drafts must not leak their vocabulary into the filter dropdowns."""
    await make_problem(slug="pub", tags=["array"], collections=["starter"])
    await make_problem(slug="draft", published=False, tags=["draft-only"],
                       collections=["secret-list"])
    await make_problem(slug="other", tags=["heap"], collections=["gang-of-four"])

    body = (await client.get("/api/v1/problems/facets", headers=user_headers)).json()
    assert body["tags"] == ["array", "heap"]
    assert body["collections"] == ["gang-of-four", "starter"]


async def test_facets_deduplicate_across_problems(client, make_problem, user_headers):
    await make_problem(slug="p1", tags=["array", "hash-table"])
    await make_problem(slug="p2", tags=["array"])
    body = (await client.get("/api/v1/problems/facets", headers=user_headers)).json()
    assert body["tags"] == ["array", "hash-table"]


# --- GET /problems/{slug}/next ------------------------------------------------------

NEXT = "/api/v1/problems/{}/next"


async def _three(make_problem):
    """a-first < b-second < c-third, by title (the list's order)."""
    for slug, title in (("a-first", "A First"), ("b-second", "B Second"), ("c-third", "C Third")):
        await make_problem(slug=slug, title=title)


async def test_next_follows_list_order_and_wraps_at_the_end(client, make_problem, user_headers):
    await _three(make_problem)

    assert (await client.get(NEXT.format("a-first"), headers=user_headers)).json()["slug"] == "b-second"
    assert (await client.get(NEXT.format("b-second"), headers=user_headers)).json()["slug"] == "c-third"
    # Off the end of the list: wrap to the start rather than dead-ending.
    assert (await client.get(NEXT.format("c-third"), headers=user_headers)).json()["slug"] == "a-first"


async def test_next_skips_solved_but_not_attempted(client, make_problem, make_user, make_submission):
    user, headers = await make_user(email="n@example.com", username="nexter")
    pids = {}
    for slug, title in (("a-first", "A First"), ("b-second", "B Second"), ("c-third", "C Third")):
        pids[slug], _ = await make_problem(slug=slug, title=title)
    await make_submission(user["id"], pids["b-second"], status="accepted")
    await make_submission(user["id"], pids["c-third"], status="wrong_answer")

    r = await client.get(NEXT.format("a-first"), headers=headers)

    assert r.json()["slug"] == "c-third"  # b is solved (skipped); c is only attempted
    assert r.json()["user_status"] == "attempted"


async def test_next_ignores_run_submissions_when_deciding_solved(
        client, make_problem, make_user, make_submission):
    user, headers = await make_user(email="r@example.com", username="runner")
    await make_problem(slug="a-first", title="A First")
    pid, _ = await make_problem(slug="b-second", title="B Second")
    await make_submission(user["id"], pid, status="accepted", is_run=True)  # a Run isn't a solve

    assert (await client.get(NEXT.format("a-first"), headers=headers)).json()["slug"] == "b-second"


async def test_next_is_null_when_nothing_else_qualifies(
        client, make_problem, make_user, make_submission):
    user, headers = await make_user(email="d@example.com", username="done")
    pid_a, _ = await make_problem(slug="a-first", title="A First")
    pid_b, _ = await make_problem(slug="b-second", title="B Second")
    await make_submission(user["id"], pid_b, status="accepted")  # only other problem: solved

    r = await client.get(NEXT.format("a-first"), headers=headers)

    assert r.status_code == 200 and r.json() is None


async def test_next_ignores_unpublished_and_404s_on_an_unknown_slug(client, make_problem, user_headers):
    await make_problem(slug="a-first", title="A First")
    await make_problem(slug="b-draft", title="B Draft", published=False)

    assert (await client.get(NEXT.format("a-first"), headers=user_headers)).json() is None
    assert (await client.get(NEXT.format("b-draft"), headers=user_headers)).status_code == 404
    assert (await client.get(NEXT.format("nope"), headers=user_headers)).status_code == 404


async def test_next_orders_by_title_then_id_like_the_list(client, make_problem, user_headers):
    """Ties on title are broken by id, exactly as the list orders them, so 'next'
    neither skips nor repeats a same-titled problem."""
    await make_problem(slug="dup-1", title="Same Title")
    await make_problem(slug="dup-2", title="Same Title")
    listed = [p["slug"] for p in (await client.get("/api/v1/problems", headers=user_headers)).json()["items"]]

    first, second = listed
    assert (await client.get(NEXT.format(first), headers=user_headers)).json()["slug"] == second
    assert (await client.get(NEXT.format(second), headers=user_headers)).json()["slug"] == first
