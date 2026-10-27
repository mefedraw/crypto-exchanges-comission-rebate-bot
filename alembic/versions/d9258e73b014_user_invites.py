"""Opaque user invitation codes and first-registration attribution.

Revision ID: d9258e73b014
Revises: c8147d62a903
"""

import sqlalchemy as sa
from alembic import op

revision = "d9258e73b014"
down_revision = "c8147d62a903"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "invite_codes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("code", sa.String(32), nullable=False, unique=True),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("code ~ '^[a-z0-9_-]{5,32}$'", name="ck_invite_code_format"),
    )
    op.create_index(
        "uq_invite_current_user",
        "invite_codes",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_current"),
    )
    op.create_index("ix_invite_codes_user", "invite_codes", ["user_id"])
    op.create_table(
        "invite_attributions",
        sa.Column("invitee_id", sa.BigInteger(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("referrer_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("code_id", sa.BigInteger(), sa.ForeignKey("invite_codes.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("invitee_id <> referrer_id", name="ck_invite_not_self"),
    )
    op.create_index(
        "ix_invite_referrer_created", "invite_attributions", ["referrer_id", "created_at"]
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    count = op.get_bind().execute(sa.text("SELECT count(*) FROM invite_codes")).scalar_one()
    if count:
        raise RuntimeError(
            "Issued invite links and referral history must be retained; downgrade refused."
        )
    op.drop_table("invite_attributions")
    op.drop_table("invite_codes")
