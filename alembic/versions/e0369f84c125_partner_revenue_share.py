"""Funded direct-invitation earnings and payout provenance.

Revision ID: e0369f84c125
Revises: d9258e73b014
"""

import sqlalchemy as sa
from alembic import op

revision = "e0369f84c125"
down_revision = "d9258e73b014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "payouts", sa.Column("purpose", sa.String(), nullable=False, server_default="rebate")
    )
    op.create_check_constraint("ck_payout_purpose", "payouts", "purpose IN ('rebate', 'partner')")
    op.create_table(
        "partner_program",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column(
            "activated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("id = 1 AND version = 1", name="ck_partner_program"),
    )
    # A partial pre-launch affiliate day cannot be reconstructed safely.
    op.execute(
        "INSERT INTO partner_program (id, starts_on) VALUES "
        "(1, (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Shanghai')::date + 1)"
    )
    op.create_table(
        "partner_accounts",
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), primary_key=True),
    )
    op.create_table(
        "partner_earnings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "source_accrual_id", sa.BigInteger(), sa.ForeignKey("rebate_accruals.id"),
            nullable=False, unique=True,
        ),
        sa.Column("referrer_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("invitee_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("rank", sa.SmallInteger(), nullable=False),
        sa.Column("share", sa.Numeric(6, 4), nullable=False),
        sa.Column("asset", sa.String(), nullable=False),
        sa.Column("owner_fee_usdt", sa.Numeric(38, 18), nullable=False),
        sa.Column("owner_fee_native", sa.Numeric(38, 18), nullable=False),
        sa.Column("amount_usdt", sa.Numeric(38, 18), nullable=False),
        sa.Column("amount_native", sa.Numeric(38, 18), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="available"),
        sa.Column("payout_id", sa.BigInteger(), sa.ForeignKey("payouts.id")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("referrer_id <> invitee_id", name="ck_partner_not_self"),
        sa.CheckConstraint("policy_version = 1 AND rank BETWEEN 0 AND 7", name="ck_partner_rank"),
        sa.CheckConstraint(
            "share = CASE rank WHEN 0 THEN 0 WHEN 1 THEN 0.20 WHEN 2 THEN 0.25 "
            "WHEN 3 THEN 0.30 WHEN 4 THEN 0.35 WHEN 5 THEN 0.40 "
            "WHEN 6 THEN 0.45 WHEN 7 THEN 0.50 END", name="ck_partner_share",
        ),
        sa.CheckConstraint("asset IN ('USDT', 'MNT')", name="ck_partner_asset"),
        sa.CheckConstraint(
            "owner_fee_usdt >= 0 AND owner_fee_native >= 0 AND amount_usdt >= 0 "
            "AND amount_native >= 0 AND amount_usdt <= owner_fee_usdt * share "
            "AND amount_native <= owner_fee_native * share", name="ck_partner_funded",
        ),
        sa.CheckConstraint(
            "(state = 'available' AND payout_id IS NULL) OR "
            "(state IN ('reserved', 'paid') AND payout_id IS NOT NULL)", name="ck_partner_state",
        ),
    )
    op.create_index("ix_partner_balance", "partner_earnings", ["referrer_id", "asset", "state"])
    op.create_index(
        "ix_accrual_finalized_owner", "rebate_accruals", ["user_id", "accrual_date"],
        postgresql_where=sa.text("finalized"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    count = op.get_bind().execute(sa.text("SELECT count(*) FROM partner_earnings")).scalar_one()
    if count:
        raise RuntimeError("Partner earning and payout history must be retained; downgrade refused.")
    op.drop_index("ix_accrual_finalized_owner", "rebate_accruals")
    op.drop_table("partner_earnings")
    op.drop_table("partner_accounts")
    op.drop_table("partner_program")
    op.drop_constraint("ck_payout_purpose", "payouts", type_="check")
    op.drop_column("payouts", "purpose")
