"""Persist exact Gate query coverage for timezone-safe accrual cutovers.

Revision ID: 93ecac0880f5
Revises: 536cf94b2929
"""

import sqlalchemy as sa

from alembic import op

revision = "93ecac0880f5"
down_revision = "536cf94b2929"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("rebate_accruals", sa.Column("query_from", sa.DateTime(timezone=True)))
    op.add_column("rebate_accruals", sa.Column("query_to", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("rebate_accruals", "query_to")
    op.drop_column("rebate_accruals", "query_from")
