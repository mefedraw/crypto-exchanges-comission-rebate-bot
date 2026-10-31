"""Persist successful commission requests independently of ledger transactions."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c47ad328e569"
down_revision = "b369c217f458"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "commission_fetches",
        sa.Column(
            "account_id", sa.BigInteger(), sa.ForeignKey("exchange_accounts.id"), primary_key=True
        ),
        sa.Column("accrual_date", sa.Date(), primary_key=True),
        sa.Column("query_from", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("query_to", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("commission_fetches")
