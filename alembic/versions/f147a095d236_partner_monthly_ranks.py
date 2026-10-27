"""Monthly partner activity, rank protection, and idempotent reviews.

Revision ID: f147a095d236
Revises: e0369f84c125
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "f147a095d236"
down_revision = "e0369f84c125"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_partner_program", "partner_program", type_="check")
    op.create_check_constraint("ck_partner_program", "partner_program", "id = 1 AND version IN (1, 2)")
    op.alter_column("partner_program", "version", server_default="2")
    op.add_column("partner_program", sa.Column("monthly_enabled_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.add_column("partner_accounts", sa.Column("current_rank", sa.SmallInteger(), nullable=False, server_default="0"))
    op.add_column("partner_accounts", sa.Column("promoted_at", sa.DateTime(timezone=True)))
    op.add_column("partner_accounts", sa.Column("protected_until", sa.DateTime(timezone=True)))
    op.add_column("partner_accounts", sa.Column("first_qualified_month", sa.Date()))
    op.add_column("partner_accounts", sa.Column("last_review_month", sa.Date()))
    op.create_check_constraint("ck_partner_current_rank", "partner_accounts", "current_rank BETWEEN 0 AND 7")
    op.create_table(
        "partner_rank_reviews",
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("month", sa.Date(), primary_key=True),
        sa.Column("previous_rank", sa.SmallInteger(), nullable=False),
        sa.Column("qualified_rank", sa.SmallInteger(), nullable=False),
        sa.Column("new_rank", sa.SmallInteger(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("active", sa.Integer(), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("previous_rank BETWEEN 0 AND 7 AND qualified_rank BETWEEN 0 AND 7 AND new_rank BETWEEN 0 AND 7 AND new_rank >= previous_rank - 1", name="ck_partner_review_rank"),
        sa.CheckConstraint("active BETWEEN 0 AND total", name="ck_partner_review_counts"),
        sa.CheckConstraint("(status = 'pending' AND applied_at IS NULL) OR (status = 'applied' AND applied_at IS NOT NULL)", name="ck_partner_review_status"),
    )
    op.drop_constraint("ck_partner_rank", "partner_earnings", type_="check")
    op.create_check_constraint("ck_partner_rank", "partner_earnings", "policy_version IN (1, 2) AND rank BETWEEN 0 AND 7")
    # Retain previously assigned earning ranks with a fresh, full protection term.
    # Do not edit the earning rows or recalculate their frozen monetary amounts.
    op.execute("""
        INSERT INTO partner_accounts (user_id, current_rank, promoted_at, protected_until, first_qualified_month)
        SELECT referrer_id, max(rank), CURRENT_TIMESTAMP,
            ((CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai') + interval '3 months') AT TIME ZONE 'Asia/Shanghai',
            date_trunc('month', CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date
        FROM partner_earnings WHERE rank > 0 GROUP BY referrer_id
        ON CONFLICT (user_id) DO UPDATE SET current_rank = EXCLUDED.current_rank,
            promoted_at = EXCLUDED.promoted_at, protected_until = EXCLUDED.protected_until,
            first_qualified_month = EXCLUDED.first_qualified_month
    """)
    op.execute("UPDATE partner_program SET version = 2, monthly_enabled_at = CURRENT_TIMESTAMP")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    used = op.get_bind().execute(sa.text("""
        SELECT EXISTS(SELECT 1 FROM partner_rank_reviews)
            OR EXISTS(SELECT 1 FROM partner_earnings WHERE policy_version = 2)
            OR EXISTS(SELECT 1 FROM partner_accounts WHERE current_rank > 0)
    """)).scalar_one()
    if used:
        raise RuntimeError("Assigned partner ranks and review history must be retained; downgrade refused.")
    op.execute("UPDATE partner_program SET version = 1")
    op.drop_table("partner_rank_reviews")
    op.drop_constraint("ck_partner_current_rank", "partner_accounts", type_="check")
    for column in ("last_review_month", "first_qualified_month", "protected_until", "promoted_at", "current_rank"):
        op.drop_column("partner_accounts", column)
    op.drop_column("partner_program", "monthly_enabled_at")
    op.alter_column("partner_program", "version", server_default="1")
    op.drop_constraint("ck_partner_program", "partner_program", type_="check")
    op.create_check_constraint("ck_partner_program", "partner_program", "id = 1 AND version = 1")
    op.drop_constraint("ck_partner_rank", "partner_earnings", type_="check")
    op.create_check_constraint("ck_partner_rank", "partner_earnings", "policy_version = 1 AND rank BETWEEN 0 AND 7")
