"""baseline: extensions and run bookkeeping

Revision ID: 0001
Revises:
Create Date: 2026-11-24
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        """
        CREATE TABLE runs (
            id          bigserial PRIMARY KEY,
            stage       text NOT NULL,
            region      text NOT NULL,
            git_sha     text,
            params      jsonb NOT NULL DEFAULT '{}'::jsonb,
            stats       jsonb NOT NULL DEFAULT '{}'::jsonb,
            status      text NOT NULL DEFAULT 'running'
                        CHECK (status IN ('running', 'succeeded', 'failed')),
            started_at  timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz
        )
        """
    )
    op.execute("CREATE INDEX runs_stage_started_idx ON runs (stage, started_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE runs")
