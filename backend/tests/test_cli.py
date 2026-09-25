"""CLI tests: seed (validate-then-write), dev user, and account recovery commands (DESIGN.md §7.1, §9).

The CLI uses app.db.SessionLocal directly, so we point it at the test engine.
"""
import json

from sqlalchemy import select

from app import cli
from app import judge_budget as jb
from app.models import Problem, TestCase, User
from app.security import hash_password

SEED = {
    "slug": "demo",
    "title": "Demo",
    "difficulty": "easy",
    "statement_md": "x",
    "function_name": "f",
    "starter_code": "def f(x): ...",
    "params": [{"name": "x", "type": "int"}],
    "comparison": {"mode": "exact"},
    "is_published": True,
    "tags": ["demo"],
    "test_cases": [{"ordinal": 0, "input": [1], "expected": 1, "is_sample": True}],
    "solutions": [{"ordinal": 0, "title": "S", "intuition_md": "e", "code": "c",
                   "time_complexity": "O(1)", "space_complexity": "O(1)"}],
}


def _write(seed_dir, name, **overrides):
    seed_dir.mkdir(exist_ok=True)
    (seed_dir / f"{name}.json").write_text(json.dumps({**SEED, **overrides}))


async def _problems(session_factory):
    async with session_factory() as s:
        return (await s.execute(select(Problem).order_by(Problem.slug))).scalars().all()


async def _case_count(session_factory):
    async with session_factory() as s:
        return len((await s.execute(select(TestCase))).scalars().all())


async def test_seed_creates_then_upserts(session_factory, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "SessionLocal", session_factory)
    _write(tmp_path, "demo")

    assert await cli.seed(tmp_path) == 0
    assert [p.slug for p in await _problems(session_factory)] == ["demo"]

    # Re-seeding is idempotent: same problem, test cases replaced not duplicated.
    assert await cli.seed(tmp_path) == 0
    assert len(await _problems(session_factory)) == 1
    assert await _case_count(session_factory) == 1


async def test_seed_missing_or_empty_dir_is_an_error(session_factory, monkeypatch, tmp_path):
    """A mistyped --dir / PROBLEMS_DIR must fail loudly, not bring the site up empty."""
    monkeypatch.setattr(cli, "SessionLocal", session_factory)
    assert await cli.seed(tmp_path) == 1                    # exists, but no *.json files
    assert await cli.seed(tmp_path / "nope") == 1           # doesn't exist
    assert await _problems(session_factory) == []


async def test_one_bad_file_means_nothing_is_written(session_factory, monkeypatch, tmp_path, capsys):
    """Validation runs over the whole directory before any write, so a bad file can't
    leave a half-loaded catalog (or a published problem with no cases)."""
    monkeypatch.setattr(cli, "SessionLocal", session_factory)
    _write(tmp_path, "good")
    _write(tmp_path, "dupes", slug="dupes", test_cases=[
        {"ordinal": 0, "input": [1], "expected": 1}, {"ordinal": 0, "input": [2], "expected": 2}])

    assert await cli.seed(tmp_path) == 1
    assert await _problems(session_factory) == []
    assert "dupes.json" in capsys.readouterr().err


async def test_a_problem_needs_at_least_one_test_case(session_factory, monkeypatch, tmp_path):
    """Zero cases would judge every submission 0/0 = accepted."""
    monkeypatch.setattr(cli, "SessionLocal", session_factory)
    _write(tmp_path, "empty", test_cases=[])
    assert await cli.seed(tmp_path) == 1

    no_key = {k: v for k, v in SEED.items() if k != "test_cases"}
    (tmp_path / "empty.json").write_text(json.dumps({**no_key, "testcases": SEED["test_cases"]}))
    assert await cli.seed(tmp_path) == 1                    # a typo'd key is "no cases" too
    assert await _problems(session_factory) == []


async def test_duplicate_solution_ordinals_are_a_validation_error(session_factory, monkeypatch,
                                                                   tmp_path, capsys):
    """Caught per file before any write, not as an IntegrityError halfway through."""
    monkeypatch.setattr(cli, "SessionLocal", session_factory)
    _write(tmp_path, "demo", solutions=[SEED["solutions"][0], SEED["solutions"][0]])
    assert await cli.seed(tmp_path) == 1
    assert "solution ordinals must be unique" in capsys.readouterr().err
    assert await _problems(session_factory) == []


async def test_a_problem_needs_a_sample_case(session_factory, monkeypatch, tmp_path, capsys):
    """Run judges only samples: with none, any code would be `accepted` 0/0."""
    monkeypatch.setattr(cli, "SessionLocal", session_factory)
    _write(tmp_path, "demo", test_cases=[{"ordinal": 0, "input": [1], "expected": 1}])
    assert await cli.seed(tmp_path) == 1
    assert "is_sample" in capsys.readouterr().err


async def test_an_unreadable_file_is_reported_not_a_crash(session_factory, monkeypatch, tmp_path,
                                                         capsys):
    """Every bad file is listed, including one that isn't UTF-8 or can't be read."""
    monkeypatch.setattr(cli, "SessionLocal", session_factory)
    _write(tmp_path, "good")
    (tmp_path / "latin1.json").write_bytes('{"title": "Caf\xe9"}'.encode("latin-1"))
    (tmp_path / "dangling.json").symlink_to(tmp_path / "missing-target.json")
    assert await cli.seed(tmp_path) == 1
    err = capsys.readouterr().err
    assert "latin1.json" in err and "dangling.json" in err
    assert await _problems(session_factory) == []


async def test_seed_refuses_a_problem_over_the_judge_budget(session_factory, monkeypatch, tmp_path,
                                                          capsys):
    monkeypatch.setattr(cli, "SessionLocal", session_factory)
    most = jb.max_cases_within_job_timeout(2000)
    _write(tmp_path, "big", test_cases=[
        {"ordinal": i, "input": [i], "expected": i, "is_sample": i == 0} for i in range(most + 1)])
    assert await cli.seed(tmp_path) == 1
    assert f"at most {most} cases" in capsys.readouterr().err  # actionable


async def test_time_limit_and_case_count_change_together(session_factory, monkeypatch, tmp_path):
    """The budget is judged on the file's new numbers, not the stored case count: fewer,
    slower cases in one edit is valid even though old count x new limit wouldn't be."""
    monkeypatch.setattr(cli, "SessionLocal", session_factory)
    _write(tmp_path, "demo", time_limit_ms=2000, test_cases=[
        {"ordinal": i, "input": [i], "expected": i, "is_sample": i == 0} for i in range(100)])
    assert await cli.seed(tmp_path) == 0

    _write(tmp_path, "demo", time_limit_ms=10_000, test_cases=[
        {"ordinal": i, "input": [i], "expected": i, "is_sample": i == 0} for i in range(20)])
    assert not jb.fits_job_timeout(100, 10_000)               # the old check would refuse this
    assert await cli.seed(tmp_path) == 0
    assert (await _problems(session_factory))[0].time_limit_ms == 10_000
    assert await _case_count(session_factory) == 20


async def test_a_missing_slug_is_derived_from_the_title(session_factory, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "SessionLocal", session_factory)
    no_slug = {k: v for k, v in SEED.items() if k != "slug"}
    (tmp_path / "x.json").write_text(json.dumps({**no_slug, "title": "Pair Sum!"}))

    assert await cli.seed(tmp_path) == 0
    assert await cli.seed(tmp_path) == 0                    # same row both times
    assert [p.slug for p in await _problems(session_factory)] == ["pair-sum"]


async def test_two_files_with_the_same_slug_are_refused(session_factory, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "SessionLocal", session_factory)
    _write(tmp_path, "a")
    _write(tmp_path, "b")                                  # both slug "demo"
    assert await cli.seed(tmp_path) == 1
    assert await _problems(session_factory) == []


async def test_unknown_keys_warn_but_load(session_factory, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "SessionLocal", session_factory)
    _write(tmp_path, "demo", author="someone")
    assert await cli.seed(tmp_path) == 0
    assert "author" in capsys.readouterr().err


def test_validate_checks_every_rule_without_touching_the_db(tmp_path, capsys):
    """Same validation phase as seed, no database: 0 when valid, 1 listing each bad file."""
    _write(tmp_path, "demo")
    assert cli.main(["validate", "--dir", str(tmp_path)]) == 0
    assert "1 problem file(s) valid" in capsys.readouterr().out

    _write(tmp_path, "nosample", slug="nosample",
           test_cases=[{"ordinal": 0, "input": [1], "expected": 1}])
    assert cli.main(["validate", "--dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "nosample.json" in err and "1 problem file(s) failed" in err

    assert cli.main(["validate", "--dir", str(tmp_path / "missing")]) == 1


async def test_verify_email_marks_verified(session_factory, monkeypatch):
    monkeypatch.setattr(cli, "SessionLocal", session_factory)
    async with session_factory() as s:
        s.add(User(email="new@x.com", username="new",
                   password_hash=hash_password("password123"), email_verified=False))
        await s.commit()

    assert await cli.verify_email("new@x.com") == 0
    async with session_factory() as s:
        user = await s.scalar(select(User).where(User.email == "new@x.com"))
        assert user.email_verified is True

    assert await cli.verify_email("ghost@x.com") == 1   # missing user → exit 1


async def test_seed_dev_user_creates_verified_user_and_is_idempotent(session_factory, monkeypatch):
    monkeypatch.setattr(cli, "SessionLocal", session_factory)
    assert await cli.seed_dev_user() == 0
    async with session_factory() as s:
        user = await s.scalar(select(User).where(User.email == cli.DEV_USER["email"]))
        assert user.email_verified is True
    assert await cli.seed_dev_user() == 0  # second run is a no-op


def test_main_dispatches_subcommands(monkeypatch):
    calls = {}

    async def fake_seed(seed_dir):
        calls["seed"] = seed_dir
        return 0

    async def fake_verify(email):
        calls["verify"] = email
        return 0

    monkeypatch.setattr(cli, "seed", fake_seed)
    monkeypatch.setattr(cli, "verify_email", fake_verify)

    assert cli.main(["seed"]) == 0
    assert "seed" in calls
    assert cli.main(["verify-email", "a@b.com"]) == 0
    assert calls["verify"] == "a@b.com"
