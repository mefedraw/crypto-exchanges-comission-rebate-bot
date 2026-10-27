"""Daily rebate accruals and monthly rank history. See DB_SCHEMA.md §2."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from bot.db.base import Base

accrual_state = Enum("accruing", "available", "reserved", "paid", "expired", name="accrual_state")


class RebateAccrual(Base):
    __tablename__ = "rebate_accruals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("exchange_accounts.id"), nullable=False
    )
    accrual_date: Mapped[date] = mapped_column(Date, nullable=False)
    asset: Mapped[str] = mapped_column(String, nullable=False, server_default="USDT")

    amount_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    effective_rank: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    fee_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    commission_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    net_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    amount_native: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, server_default="0"
    )
    net_native: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, server_default="0")
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, server_default="1")

    state: Mapped[str] = mapped_column(accrual_state, nullable=False, server_default="accruing")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payout_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("payouts.id"))

    raw_records_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    adapter_notes: Mapped[str | None] = mapped_column(Text)
    # Exact Gate API coverage. NULL denotes a legacy UTC-calendar query.
    query_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    query_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    correction_key: Mapped[str | None] = mapped_column(String(160))
    finalized: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "accrual_date",
            "asset",
            "correction_key",
            name="uq_accrual_day",
            postgresql_nulls_not_distinct=True,
        ),
        UniqueConstraint("correction_key", name="uq_accrual_correction_key"),
        CheckConstraint(
            "(correction_key IS NULL AND fee_pct IS NOT NULL) OR "
            "(correction_key IS NOT NULL AND fee_pct IS NULL)",
            name="ck_accrual_correction_fee",
        ),
        Index("uq_accrual_payout", "id", unique=True, postgresql_where="payout_id IS NOT NULL"),
        Index(
            "ix_accrual_available",
            "user_id",
            "accrual_date",
            postgresql_where="state = 'available'",
        ),
        Index("ix_accrual_expiry", "expires_at", postgresql_where="state = 'available'"),
        Index(
            "ix_accrual_finalized_owner", "user_id", "accrual_date", postgresql_where="finalized"
        ),
    )


class RankHistory(Base):
    __tablename__ = "rank_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    period_month: Mapped[date] = mapped_column(Date, nullable=False)
    achieved_rank: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    gross_month_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("user_id", "period_month", name="uq_rank_month"),)
