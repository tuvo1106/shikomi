"""Operator CLI (DESIGN.md §7.1 seeding, §9 operations).

The CLI is the only way problems get into the database: shikomi has no
authoring API or UI, so an operator writes JSON files and loads them here.

    python -m app.cli seed [--dir PATH]
    python -m app.cli seed-dev-user
    python -m app.cli verify-email <email>
    python -m app.cli disable-2fa <email>
    python -m app.cli schema-docs
"""
import argparse
import asyncio
import json
import pathlib
import sys

from pydantic import ValidationError
from sqlalchemy import select

from app.audit import audit
from app.config import get_settings
from app.db import SessionLocal
from app.models import User
from app.schemas.problem import ProblemFile
from app.security import hash_password
from app.services import problem_service

DEV_USER = {"email": "dev@example.com", "username": "dev", "password": "devpassword"}

SEED_DIR = pathlib.Path(__file__).resolve().parents[2] / "seed" / "problems"


def _load_problem_files(seed_dir: pathlib.Path) -> tuple[list[ProblemFile], list[str]]:
    """Parse and validate every `*.json` in `seed_dir`, writing nothing.

    Returns the valid problems and one error line per bad file (all of them, not
    just the first, so an operator fixes a directory in one pass). A missing or
    empty directory is an error too: under Compose a mistyped `PROBLEMS_DIR`
    bind-mounts an empty directory, and "seeded nothing, exit 0" would bring the
    site up with no problems and no failing signal.
    """
    if not seed_dir.is_dir():
        return [], [f"{seed_dir}: not a directory"]
    paths = sorted(seed_dir.glob("*.json"))
    if not paths:
        return [], [f"{seed_dir}: no *.json problem files"]
    problems, errors, slugs = [], [], {}
    for path in paths:
        try:
            raw = json.loads(path.read_text())
            problem = ProblemFile.model_validate(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        unknown = sorted(set(raw) - set(ProblemFile.model_fields))
        if unknown:
            print(f"  warning: {path.name}: ignoring unknown keys {unknown}", file=sys.stderr)
        slug = problem.slug or problem_service.slugify(problem.title)
        if slug in slugs:  # two files would silently overwrite each other
            errors.append(f"{path.name}: slug {slug!r} already used by {slugs[slug]}")
            continue
        slugs[slug] = path.name
        problems.append(problem)
    return problems, errors


async def seed(seed_dir: pathlib.Path) -> int:
    """Load every `*.json` problem in `seed_dir` into the DB (idempotent upsert).

    `--dir` is what makes shikomi bring-your-own-problem: the bundled
    `seed/problems/` holds only the starter examples, and an operator points
    this at their own directory (a private repo, a mounted volume) instead.

    Two phases, so a bad file can't leave the database half-loaded: every file is
    validated first (`ProblemFile`: schema, at least one test case, unique ordinals,
    the judge budget), and only if all pass are they written, in one transaction.
    Upsert means create-or-overwrite by slug; a problem missing from the directory
    is left alone, never deleted.

    Returns:
        0 on success, 1 if any file failed validation (nothing is written).
    """
    problems, errors = _load_problem_files(seed_dir)
    if errors:
        for line in errors:
            print(f"error: {line}", file=sys.stderr)
        print(f"Seed aborted: {len(errors)} problem file(s) failed; nothing was written.",
              file=sys.stderr)
        return 1
    async with SessionLocal() as session:
        done = [await problem_service.upsert_problem(session, p) for p in problems]
        await session.commit()
    # Reported only after the commit, so a failed write never reads as a successful load.
    for row, action in done:
        print(f"  {action}: {row.slug}")
    print(f"Seeded {len(problems)} problem(s).")
    return 0


SCHEMA_DOC = pathlib.Path(__file__).resolve().parents[2] / "docs" / "schema.md"
_TYPE_NAMES = {
    "uuid": "uuid", "citext": "citext", "text": "text", "integer": "int",
    "boolean": "bool", "datetime": "timestamptz", "jsonb": "jsonb", "array": "text_array",
}


def _col_type(col) -> str:
    name = col.type.__class__.__name__.lower()
    return _TYPE_NAMES.get(name, name)


def _col_key(col) -> str:
    if col.primary_key:
        return "PK"
    if col.foreign_keys:
        return "FK"
    if col.unique:
        return "UK"
    return ""


def write_schema_docs() -> int:
    """Regenerate docs/schema.md as a Mermaid ER diagram from the models."""
    from app.models import Base

    lines = ["erDiagram"]
    rels: set[str] = set()
    for table in Base.metadata.sorted_tables:
        lines.append(f"    {table.name} {{")
        for col in table.columns:
            key = _col_key(col)
            lines.append(f"        {_col_type(col)} {col.name}{(' ' + key) if key else ''}")
        lines.append("    }")
        for fk in table.foreign_keys:
            rels.add(f'    {fk.column.table.name} ||--o{{ {table.name} : ""')
    lines.extend(sorted(rels))

    content = (
        "# Database schema\n\n"
        "> Auto-generated from the SQLAlchemy models: `python -m app.cli schema-docs`.\n"
        "> Do not edit by hand — re-run after changing models or migrations.\n\n"
        "```mermaid\n" + "\n".join(lines) + "\n```\n"
    )
    SCHEMA_DOC.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_DOC.write_text(content)
    print(f"wrote {SCHEMA_DOC}")
    return 0


async def seed_dev_user() -> int:
    """Create a known, pre-verified login for local dev (idempotent, refuses in prod)."""
    if get_settings().env == "prod":
        print("refusing to seed a dev user when ENV=prod", file=sys.stderr)
        return 1
    async with SessionLocal() as session:
        existing = await session.scalar(select(User).where(User.email == DEV_USER["email"]))
        if existing is not None:
            print(f"dev user already exists: {DEV_USER['email']}")
            return 0
        session.add(User(email=DEV_USER["email"], username=DEV_USER["username"],
                         password_hash=hash_password(DEV_USER["password"]),
                         email_verified=True))
        await session.commit()
    print(f"created dev user: {DEV_USER['email']} / {DEV_USER['password']}")
    return 0


async def verify_email(email: str) -> int:
    """Mark a user's email verified out-of-band (ops / test bootstrap).

    Verification normally happens by clicking the emailed link, but there are cases
    where an operator needs to confirm an account directly — and the HTTP-only
    pipeline smoke test (`scripts/e2e_submit.py`) uses this so its throwaway user
    can pass the `require_verified` gate without an inbox. Returns an exit code.
    """
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            print(f"No user with email {email!r}", file=sys.stderr)
            return 1
        user.email_verified = True
        await session.commit()
        print(f"{email} is now verified.")
    return 0


async def disable_2fa(email: str) -> int:
    """Turn off a user's two-factor auth out-of-band (ops recovery for a lost phone).

    The account owner disables it themselves in Settings; this exists for the case where
    they've lost both the authenticator *and* the recovery codes. It's operator-only (needs
    shell access to the DB), so it's the trust anchor for that recovery — verify the
    person's identity out of band before running it. Returns an exit code.
    """
    from app.services import totp_service
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.email == email))
        if user is None:
            print(f"No user with email {email!r}", file=sys.stderr)
            return 1
        await totp_service.clear(session, user.id)
        # The highest-trust bypass, so leave the same trail the in-app disable does.
        audit("auth.2fa.disabled", user_id=user.id, email=email, via="cli")
        print(f"Two-factor authentication is now off for {email}.")
    return 0


def main(argv=None) -> int:
    """Parse argv, dispatch to the chosen subcommand, and return its exit code."""
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    seed_p = sub.add_parser("seed", help="upsert problems from seed JSON files")
    seed_p.add_argument("--dir", type=pathlib.Path, default=SEED_DIR)

    sub.add_parser("seed-dev-user", help="create a known, verified login for local dev")

    sub.add_parser("schema-docs", help="regenerate docs/schema.md from the models")

    verify_p = sub.add_parser("verify-email", help="mark a user's email verified")
    verify_p.add_argument("email")

    disable2fa_p = sub.add_parser("disable-2fa", help="turn off a user's two-factor auth")
    disable2fa_p.add_argument("email")

    args = parser.parse_args(argv)
    if args.command == "seed":
        return asyncio.run(seed(args.dir))
    if args.command == "seed-dev-user":
        return asyncio.run(seed_dev_user())
    if args.command == "schema-docs":
        return write_schema_docs()
    if args.command == "verify-email":
        return asyncio.run(verify_email(args.email))
    if args.command == "disable-2fa":
        return asyncio.run(disable_2fa(args.email))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
