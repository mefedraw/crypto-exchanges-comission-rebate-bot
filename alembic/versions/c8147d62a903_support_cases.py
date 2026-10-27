"""Persistent support cases and conversation messages.

Revision ID: c8147d62a903
Revises: b702a3647f18
"""

import sqlalchemy as sa

from alembic import op

revision = "c8147d62a903"
down_revision = "b702a3647f18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "support_cases",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "account_id", sa.BigInteger(), sa.ForeignKey("exchange_accounts.id"), nullable=False
        ),
        sa.Column("exchange", sa.String(32), nullable=False),
        sa.Column("uid", sa.String(64), nullable=False),
        sa.Column("username", sa.String(64)),
        sa.Column("gross_at_open", sa.Numeric(38, 18), nullable=False),
        sa.Column("request_token", sa.String(48), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("closed_by", sa.BigInteger()),
        sa.CheckConstraint("gross_at_open >= 100", name="ck_support_case_eligible"),
    )
    op.create_index(
        "uq_support_open_account",
        "support_cases",
        ["user_id", "account_id"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.create_index("ix_support_user_created", "support_cases", ["user_id", "created_at"])
    op.create_index(
        "ix_support_open_updated",
        "support_cases",
        ["updated_at"],
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.create_table(
        "support_messages",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("case_id", sa.BigInteger(), sa.ForeignKey("support_cases.id"), nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("is_staff", sa.Boolean(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("request_token", sa.String(48), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "char_length(body) BETWEEN 1 AND 1200", name="ck_support_message_length"
        ),
    )
    op.create_index("ix_support_message_case", "support_messages", ["case_id", "id"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    count = op.get_bind().execute(sa.text("SELECT count(*) FROM support_cases")).scalar_one()
    if count:
        raise RuntimeError("Support history must be retained; downgrade refused.")
    op.drop_table("support_messages")
    op.drop_table("support_cases")
