"""Persistent support cases, account snapshots, and their messages."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from bot.db.base import Base


class SupportCase(Base):
    __tablename__ = "support_cases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("exchange_accounts.id"), nullable=False
    )
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    uid: Mapped[str] = mapped_column(String(64), nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    gross_at_open: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    request_token: Mapped[str] = mapped_column(String(48), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="sent")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (
        CheckConstraint("gross_at_open >= 100", name="ck_support_case_eligible"),
        CheckConstraint(
            "status IN ('sent', 'open', 'requires_reply', 'closed') "
            "AND ((status = 'closed') = (closed_at IS NOT NULL))",
            name="ck_support_case_status",
        ),
        Index(
            "uq_support_open_account",
            "user_id",
            "account_id",
            unique=True,
            postgresql_where="closed_at IS NULL",
        ),
        Index("ix_support_user_created", "user_id", "created_at"),
        Index("ix_support_open_updated", "updated_at", postgresql_where="closed_at IS NULL"),
    )


class SupportMessage(Base):
    __tablename__ = "support_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("support_cases.id"), nullable=False)
    author_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_staff: Mapped[bool] = mapped_column(Boolean, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    request_token: Mapped[str] = mapped_column(String(48), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("char_length(body) BETWEEN 1 AND 1200", name="ck_support_message_length"),
        Index("ix_support_message_case", "case_id", "id"),
    )
