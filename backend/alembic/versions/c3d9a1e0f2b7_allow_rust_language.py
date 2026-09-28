"""allow language 'rust' on problems

Widens `ck_problems_language` to admit the Rust judge (DESIGN.md §13,
docs/adr/0004-rust-judge-compile-in-sandbox.md). Hand-written: autogenerate
doesn't diff CHECK constraint bodies, so it would emit nothing here.

Postgres can't alter a CHECK constraint in place, so it's dropped and
re-created. The downgrade fails while any Rust problem exists, because the
narrower constraint won't validate against those rows. That's deliberate:
deleting authored problems to make it pass isn't a migration's call to make.

Revision ID: c3d9a1e0f2b7
Revises: b172fe36a2e8
Create Date: 2026-09-27 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'c3d9a1e0f2b7'
down_revision: Union[str, None] = 'b172fe36a2e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('ck_problems_language', 'problems', type_='check')
    op.create_check_constraint(
        'ck_problems_language', 'problems', "language IN ('python', 'js', 'rust', 'mysql')")


def downgrade() -> None:
    op.drop_constraint('ck_problems_language', 'problems', type_='check')
    op.create_check_constraint(
        'ck_problems_language', 'problems', "language IN ('python', 'js', 'mysql')")
