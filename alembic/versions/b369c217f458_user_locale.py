"""Persist user interface locale; retain Russian for existing users.

Revision ID: b369c217f458
Revises: a258b106e347
"""

from alembic import op
import sqlalchemy as sa

revision = "b369c217f458"
down_revision = "a258b106e347"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("users", sa.Column("locale", sa.String(length=8), server_default="ru", nullable=False))
    op.create_check_constraint("ck_user_locale", "users", "locale IN ('ru','en','uk','zh','tr','hi','th','vi')")


def downgrade() -> None:
    op.drop_constraint("ck_user_locale", "users", type_="check")
    op.drop_column("users", "locale")
