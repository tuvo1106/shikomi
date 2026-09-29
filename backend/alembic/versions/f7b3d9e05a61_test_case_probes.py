"""add test_cases.probes

Extra calls the harness makes on an operations case's instance after the replay, for
a custom validator to check (docs/adr/0007-custom-validators-in-every-language.md).
Nullable, and NULL for every existing case, so the upgrade touches no data.

The downgrade drops the column, and with it any probes seeded since. It can't make a
problem that uses them work on the previous release, whose harness doesn't know the
probe form of a validator (`validate(actual, expected, args, probe_results)`): every
submission to such a problem is a judge_error there until it's re-seeded from problem
files that use the older `instance` form.

Revision ID: f7b3d9e05a61
Revises: e5a8c2f41d93
Create Date: 2026-09-28 18:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'f7b3d9e05a61'
down_revision: Union[str, None] = 'e5a8c2f41d93'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('test_cases', sa.Column('probes', postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column('test_cases', 'probes')
