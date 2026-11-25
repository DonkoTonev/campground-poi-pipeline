"""LLM ledger: keep the provider's error message

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-25
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A failed call must say why (the first live run lost "compiled grammar is too large").
    op.execute("ALTER TABLE llm_calls ADD COLUMN error text")


def downgrade() -> None:
    op.execute("ALTER TABLE llm_calls DROP COLUMN error")
