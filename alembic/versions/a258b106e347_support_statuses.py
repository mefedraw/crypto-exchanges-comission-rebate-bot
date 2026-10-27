"""Four support statuses with a safe backfill of existing conversations.

Revision ID: a258b106e347
Revises: f147a095d236
"""

import sqlalchemy as sa

from alembic import op

revision = "a258b106e347"
down_revision = "f147a095d236"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("support_cases", sa.Column("status", sa.String(24), nullable=False, server_default="sent"))
    op.execute("""
        UPDATE support_cases c SET status = CASE
            WHEN c.closed_at IS NOT NULL THEN 'closed'
            WHEN (SELECT m.is_staff FROM support_messages m
                  WHERE m.case_id = c.id ORDER BY m.id DESC LIMIT 1) THEN 'requires_reply'
            WHEN EXISTS(SELECT 1 FROM support_messages m WHERE m.case_id = c.id AND m.is_staff)
                THEN 'open'
            ELSE 'sent' END
    """)
    op.create_check_constraint(
        "ck_support_case_status", "support_cases",
        "status IN ('sent', 'open', 'requires_reply', 'closed') "
        "AND ((status = 'closed') = (closed_at IS NOT NULL))",
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    if op.get_bind().execute(sa.text("SELECT EXISTS(SELECT 1 FROM support_cases)")).scalar_one():
        raise RuntimeError("Support status history must be retained; downgrade refused.")
    op.drop_constraint("ck_support_case_status", "support_cases", type_="check")
    op.drop_column("support_cases", "status")
