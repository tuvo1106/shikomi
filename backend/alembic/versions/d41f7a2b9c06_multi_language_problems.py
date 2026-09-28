"""multi-language problems

Moves everything language-specific off `problems` into `problem_languages` (one
row per language a problem is offered in), moves solution code into
`solution_codes` (one row per solution per language), and records the language
on every submission (docs/adr/0005-multi-language-problems.md).

Hand-written, because the interesting part is the backfill, which autogenerate
can't produce: every existing problem becomes a one-language problem whose
single variant (ordinal 0) carries its old columns, every solution's code
becomes that language's `solution_codes` row, and every submission gets its
problem's language. Nothing is lost on the way up.

The downgrade restores the old columns from each problem's ordinal-0 variant.
It is lossy only for what the old schema can't hold: extra languages, the
submissions made in them, and solutions with no code in the default language.
Those are deleted, since the old schema would judge and show them as the
default language.

Revision ID: d41f7a2b9c06
Revises: c3d9a1e0f2b7
Create Date: 2026-09-27 18:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'd41f7a2b9c06'
down_revision: Union[str, None] = 'c3d9a1e0f2b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UUID_PK = sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False)


def _timestamps():
    return [
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        'problem_languages',
        _UUID_PK.copy(),
        sa.Column('problem_id', sa.UUID(), nullable=False),
        sa.Column('ordinal', sa.Integer(), nullable=False),
        sa.Column('language', sa.Text(), nullable=False),
        sa.Column('starter_code', sa.Text(), nullable=False),
        sa.Column('function_name', sa.Text(), nullable=True),
        sa.Column('class_name', sa.Text(), nullable=True),
        sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('return_type', sa.Text(), server_default='', nullable=False),
        sa.Column('note_md', sa.Text(), server_default='', nullable=False),
        *_timestamps(),
        sa.CheckConstraint("language IN ('python', 'js', 'rust', 'mysql')",
                           name='ck_problem_languages_language'),
        sa.ForeignKeyConstraint(['problem_id'], ['problems.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('problem_id', 'language', name='uq_problem_language'),
        sa.UniqueConstraint('problem_id', 'ordinal', name='uq_problem_language_ordinal'),
    )
    op.execute("""
        INSERT INTO problem_languages
            (problem_id, ordinal, language, starter_code, function_name, class_name,
             params, return_type)
        SELECT id, 0, language, starter_code, function_name, class_name, params, return_type
        FROM problems
    """)

    op.create_table(
        'solution_codes',
        _UUID_PK.copy(),
        sa.Column('solution_id', sa.UUID(), nullable=False),
        sa.Column('language', sa.Text(), nullable=False),
        sa.Column('code', sa.Text(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(['solution_id'], ['solutions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('solution_id', 'language', name='uq_solution_code_language'),
    )
    op.execute("""
        INSERT INTO solution_codes (solution_id, language, code)
        SELECT s.id, p.language, s.code FROM solutions s JOIN problems p ON p.id = s.problem_id
    """)
    op.drop_column('solutions', 'code')

    # Nullable first so existing rows can be backfilled, then tightened.
    op.add_column('submissions', sa.Column('language', sa.Text(), nullable=True))
    op.execute("""
        UPDATE submissions s SET language = p.language FROM problems p WHERE p.id = s.problem_id
    """)
    op.alter_column('submissions', 'language', nullable=False)
    op.drop_index('ix_submissions_problem_is_run_status', table_name='submissions')
    op.create_index('ix_submissions_problem_language_is_run_status', 'submissions',
                    ['problem_id', 'language', 'is_run', 'status'])

    for name in ('ck_problems_kind_name_consistency', 'ck_problems_language',
                 'ck_problems_sql_kind_mysql_language',
                 'ck_problems_custom_validator_requires_python'):
        op.drop_constraint(name, 'problems', type_='check')
    for column in ('language', 'starter_code', 'function_name', 'class_name', 'params',
                   'return_type'):
        op.drop_column('problems', column)
    op.create_check_constraint(
        'ck_problems_kind', 'problems', "kind IN ('function', 'operations', 'sql')")


def downgrade() -> None:
    op.drop_constraint('ck_problems_kind', 'problems', type_='check')
    op.add_column('problems', sa.Column('language', sa.Text(), server_default='python',
                                        nullable=False))
    op.add_column('problems', sa.Column('starter_code', sa.Text(), server_default='',
                                        nullable=False))
    op.add_column('problems', sa.Column('function_name', sa.Text(), nullable=True))
    op.add_column('problems', sa.Column('class_name', sa.Text(), nullable=True))
    op.add_column('problems', sa.Column('params', postgresql.JSONB(astext_type=sa.Text()),
                                        server_default='[]', nullable=False))
    op.add_column('problems', sa.Column('return_type', sa.Text(), server_default='',
                                        nullable=False))
    op.execute("""
        UPDATE problems p SET language = v.language, starter_code = v.starter_code,
            function_name = v.function_name, class_name = v.class_name,
            params = v.params, return_type = v.return_type
        FROM problem_languages v WHERE v.problem_id = p.id AND v.ordinal = 0
    """)
    op.alter_column('problems', 'starter_code', server_default=None)
    op.alter_column('problems', 'params', server_default=None)
    op.create_check_constraint(
        'ck_problems_kind_name_consistency', 'problems',
        "(kind = 'function' AND function_name IS NOT NULL AND class_name IS NULL) OR "
        "(kind = 'operations' AND class_name IS NOT NULL AND function_name IS NULL) OR "
        "(kind = 'sql' AND function_name IS NULL AND class_name IS NULL)")
    op.create_check_constraint(
        'ck_problems_language', 'problems', "language IN ('python', 'js', 'rust', 'mysql')")
    op.create_check_constraint(
        'ck_problems_sql_kind_mysql_language', 'problems', "(kind = 'sql') = (language = 'mysql')")
    op.create_check_constraint(
        'ck_problems_custom_validator_requires_python', 'problems',
        "(comparison->>'mode' IS DISTINCT FROM 'custom_validator') OR "
        "(language = 'python' AND length(coalesce(comparison->>'validator_code', '')) > 0)")

    # The old schema can only judge a submission as its problem's one language.
    op.execute("""
        DELETE FROM submissions s USING problems p
        WHERE p.id = s.problem_id AND s.language <> p.language
    """)
    op.drop_index('ix_submissions_problem_language_is_run_status', table_name='submissions')
    op.create_index('ix_submissions_problem_is_run_status', 'submissions',
                    ['problem_id', 'is_run', 'status'])
    op.drop_column('submissions', 'language')

    op.add_column('solutions', sa.Column('code', sa.Text(), server_default='', nullable=False))
    op.execute("""
        UPDATE solutions s SET code = c.code
        FROM solution_codes c, problems p
        WHERE c.solution_id = s.id AND p.id = s.problem_id AND c.language = p.language
    """)
    # A solution with no version in the default language ("Rust only") has nothing
    # the old schema could show.
    op.execute("DELETE FROM solutions WHERE code = ''")
    op.alter_column('solutions', 'code', server_default=None)
    op.drop_table('solution_codes')
    op.drop_table('problem_languages')
