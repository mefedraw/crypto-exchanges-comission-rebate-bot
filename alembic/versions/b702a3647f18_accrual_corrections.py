"""Allow separately audited, idempotent monetary corrections.

Revision ID: b702a3647f18
Revises: 93ecac0880f5
"""

import sqlalchemy as sa

from alembic import op

revision = "b702a3647f18"
down_revision = "93ecac0880f5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("rebate_accruals", sa.Column("correction_key", sa.String(160)))
    op.alter_column("rebate_accruals", "fee_pct", existing_type=sa.Numeric(6, 4), nullable=True)
    op.drop_constraint("uq_accrual_day", "rebate_accruals", type_="unique")
    op.create_unique_constraint(
        "uq_accrual_day",
        "rebate_accruals",
        ["account_id", "accrual_date", "asset", "correction_key"],
        postgresql_nulls_not_distinct=True,
    )
    op.create_unique_constraint("uq_accrual_correction_key", "rebate_accruals", ["correction_key"])
    op.create_check_constraint(
        "ck_accrual_correction_fee",
        "rebate_accruals",
        "(correction_key IS NULL AND fee_pct IS NOT NULL) OR "
        "(correction_key IS NOT NULL AND fee_pct IS NULL)",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    count = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM rebate_accruals WHERE correction_key IS NOT NULL"))
        .scalar_one()
    )
    if count:
        raise RuntimeError("Posted monetary corrections must be retained; downgrade refused.")
    op.drop_constraint("ck_accrual_correction_fee", "rebate_accruals", type_="check")
    op.drop_constraint("uq_accrual_correction_key", "rebate_accruals", type_="unique")
    op.drop_constraint("uq_accrual_day", "rebate_accruals", type_="unique")
    op.create_unique_constraint(
        "uq_accrual_day", "rebate_accruals", ["account_id", "accrual_date", "asset"]
    )
    op.alter_column("rebate_accruals", "fee_pct", existing_type=sa.Numeric(6, 4), nullable=False)
    op.drop_column("rebate_accruals", "correction_key")
