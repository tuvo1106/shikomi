"""initial schema

The whole schema in one migration (shikomi starts with a fresh history). Two
things here autogenerate would not emit on its own, so keep them if this is
ever regenerated: the `citext` extension (users.email/username depend on it)
and the `created_at DESC` column of `ix_submissions_user_problem_created`,
which autogenerate renders as a `literal_column` (AGENTS.md gotchas).

Revision ID: b172fe36a2e8
Revises: 
Create Date: 2026-09-23 18:40:41.873538
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'b172fe36a2e8'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.create_table('problems',
    sa.Column('slug', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('difficulty', sa.Text(), nullable=False),
    sa.Column('statement_md', sa.Text(), nullable=False),
    sa.Column('language', sa.Text(), server_default='python', nullable=False),
    sa.Column('kind', sa.Text(), server_default='function', nullable=False),
    sa.Column('function_name', sa.Text(), nullable=True),
    sa.Column('class_name', sa.Text(), nullable=True),
    sa.Column('starter_code', sa.Text(), nullable=False),
    sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('return_type', sa.Text(), server_default='', nullable=False),
    sa.Column('comparison', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('time_limit_ms', sa.Integer(), server_default='2000', nullable=False),
    sa.Column('memory_limit_mb', sa.Integer(), server_default='256', nullable=False),
    sa.Column('is_published', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('tags', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('constraints', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('collections', postgresql.ARRAY(sa.Text()), server_default='{}', nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(comparison->>'mode' IS DISTINCT FROM 'custom_validator') OR (language = 'python' AND length(coalesce(comparison->>'validator_code', '')) > 0)", name='ck_problems_custom_validator_requires_python'),
    sa.CheckConstraint("(kind = 'function' AND function_name IS NOT NULL AND class_name IS NULL) OR (kind = 'operations' AND class_name IS NOT NULL AND function_name IS NULL) OR (kind = 'sql' AND function_name IS NULL AND class_name IS NULL)", name='ck_problems_kind_name_consistency'),
    sa.CheckConstraint("(kind = 'sql') = (language = 'mysql')", name='ck_problems_sql_kind_mysql_language'),
    sa.CheckConstraint("language IN ('python', 'js', 'mysql')", name='ck_problems_language'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug')
    )
    op.create_table('users',
    sa.Column('email', postgresql.CITEXT(), nullable=False),
    sa.Column('username', postgresql.CITEXT(), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=False),
    sa.Column('email_verified', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('totp_secret_enc', sa.Text(), nullable=True),
    sa.Column('totp_enabled', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('totp_last_step', sa.BigInteger(), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email'),
    sa.UniqueConstraint('username')
    )
    op.create_table('email_tokens',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('token_hash', sa.Text(), nullable=False),
    sa.Column('purpose', sa.Text(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_table('recovery_codes',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('code_hash', sa.Text(), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code_hash')
    )
    op.create_index('ix_recovery_codes_user_id', 'recovery_codes', ['user_id'], unique=False)
    op.create_table('refresh_tokens',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('token_hash', sa.Text(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('rotated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('persistent', sa.Boolean(), server_default='true', nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index('ix_refresh_tokens_user_id_active', 'refresh_tokens', ['user_id'], unique=False, postgresql_where=sa.text('revoked_at IS NULL'))
    op.create_table('solutions',
    sa.Column('problem_id', sa.UUID(), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('intuition_md', sa.Text(), nullable=False),
    sa.Column('algorithm_md', sa.Text(), server_default='', nullable=False),
    sa.Column('code', sa.Text(), nullable=False),
    sa.Column('time_complexity', sa.Text(), nullable=False),
    sa.Column('space_complexity', sa.Text(), nullable=False),
    sa.Column('time_complexity_reason', sa.Text(), server_default='', nullable=False),
    sa.Column('space_complexity_reason', sa.Text(), server_default='', nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['problem_id'], ['problems.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('problem_id', 'ordinal', name='uq_solution_ordinal')
    )
    op.create_table('submissions',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('problem_id', sa.UUID(), nullable=False),
    sa.Column('code', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), server_default='pending', nullable=False),
    sa.Column('verdict_detail', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('runtime_ms', sa.Float(), nullable=True),
    sa.Column('is_run', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['problem_id'], ['problems.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_submissions_problem_is_run_status', 'submissions', ['problem_id', 'is_run', 'status'], unique=False)
    op.create_index('ix_submissions_user_problem_created', 'submissions', ['user_id', 'problem_id', sa.literal_column('created_at DESC')], unique=False)
    op.create_index('ix_submissions_unfinished_updated', 'submissions', ['updated_at'], unique=False, postgresql_where=sa.text("status IN ('pending', 'running')"))
    op.create_table('test_cases',
    sa.Column('problem_id', sa.UUID(), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('input', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('expected', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('is_sample', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['problem_id'], ['problems.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('problem_id', 'ordinal', name='uq_test_case_ordinal')
    )


def downgrade() -> None:
    op.drop_table('test_cases')
    op.drop_index('ix_submissions_unfinished_updated', table_name='submissions', postgresql_where=sa.text("status IN ('pending', 'running')"))
    op.drop_index('ix_submissions_user_problem_created', table_name='submissions')
    op.drop_index('ix_submissions_problem_is_run_status', table_name='submissions')
    op.drop_table('submissions')
    op.drop_table('solutions')
    op.drop_index('ix_refresh_tokens_user_id_active', table_name='refresh_tokens', postgresql_where=sa.text('revoked_at IS NULL'))
    op.drop_table('refresh_tokens')
    op.drop_index('ix_recovery_codes_user_id', table_name='recovery_codes')
    op.drop_table('recovery_codes')
    op.drop_table('email_tokens')
    op.drop_table('users')
    op.drop_table('problems')
