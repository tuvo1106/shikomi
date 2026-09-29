"""store every custom validator as a per-language map

`comparison.validator_code` becomes a map of language to validator source
(docs/adr/0007-custom-validators-in-every-language.md). `ProblemIn` already stores
new seeds that way; this converts rows seeded earlier, where it was one Python
string, so the stored form is uniform: `{"python": "<source>"}`.

The downgrade converts back to the string, taking the Python entry, because the
previous release hands `validator_code` straight to judge/harness.py, which can only
compile a string: a map left in place would make every submission to the problem a
judge_error after a rollback. It refuses to run while any validator problem has no
Python validator, since there's no string the old release could use for it, and
deleting or rewriting authored problems isn't a migration's call to make (the same
stance as c3d9a1e0f2b7's downgrade).

Revision ID: e5a8c2f41d93
Revises: d41f7a2b9c06
Create Date: 2026-09-28 12:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e5a8c2f41d93'
down_revision: Union[str, None] = 'd41f7a2b9c06'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_VALIDATOR = "comparison->>'mode' = 'custom_validator'"


def upgrade() -> None:
    op.execute(f"""
        UPDATE problems
        SET comparison = jsonb_set(comparison, '{{validator_code}}',
                                   jsonb_build_object('python', comparison->'validator_code'))
        WHERE {_VALIDATOR} AND jsonb_typeof(comparison->'validator_code') = 'string'
    """)


def downgrade() -> None:
    stranded = op.get_bind().execute(sa.text(f"""
        SELECT slug FROM problems
        WHERE {_VALIDATOR} AND jsonb_typeof(comparison->'validator_code') = 'object'
          AND jsonb_typeof(comparison->'validator_code'->'python') IS DISTINCT FROM 'string'
        ORDER BY slug
    """)).scalars().all()
    if stranded:
        raise RuntimeError(
            "can't downgrade: these custom_validator problems have no Python validator, "
            f"which the previous release needs: {', '.join(stranded)}")
    op.execute(f"""
        UPDATE problems
        SET comparison = jsonb_set(comparison, '{{validator_code}}',
                                   comparison->'validator_code'->'python')
        WHERE {_VALIDATOR} AND jsonb_typeof(comparison->'validator_code') = 'object'
    """)
