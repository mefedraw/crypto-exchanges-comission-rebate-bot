"""mnt multiasset and username

Revision ID: 536cf94b2929
Revises: 81ab1864bc0b
Create Date: 2026-07-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '536cf94b2929'
down_revision: Union[str, Sequence[str], None] = '81ab1864bc0b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("rebate_accruals", sa.Column("amount_native", sa.Numeric(38, 18), nullable=False, server_default="0"))
    op.add_column("rebate_accruals", sa.Column("net_native", sa.Numeric(38, 18), nullable=False, server_default="0"))
    op.add_column("rebate_accruals", sa.Column("fx_rate", sa.Numeric(38, 18), nullable=False, server_default="1"))
    op.execute("UPDATE rebate_accruals SET amount_native = amount_usdt, net_native = net_usdt WHERE amount_native = 0")
    op.drop_constraint("uq_accrual_day", "rebate_accruals", type_="unique")
    op.create_unique_constraint("uq_accrual_day", "rebate_accruals", ["account_id", "accrual_date", "asset"])
    op.add_column("payouts", sa.Column("asset", sa.String(), nullable=False, server_default="USDT"))
    op.add_column("payouts", sa.Column("net_native", sa.Numeric(38, 18), nullable=False, server_default="0"))
    op.execute("UPDATE payouts SET net_native = net_usdt WHERE net_native = 0")
    op.add_column("users", sa.Column("username", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "username")
    op.drop_column("payouts", "net_native")
    op.drop_column("payouts", "asset")
    op.drop_constraint("uq_accrual_day", "rebate_accruals", type_="unique")
    op.create_unique_constraint("uq_accrual_day", "rebate_accruals", ["account_id", "accrual_date"])
    op.drop_column("rebate_accruals", "fx_rate")
    op.drop_column("rebate_accruals", "net_native")
    op.drop_column("rebate_accruals", "amount_native")
