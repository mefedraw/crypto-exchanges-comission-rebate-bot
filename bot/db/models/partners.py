"""Partner obligations are funded by immutable source fees, not users' rebates."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bot.db.base import Base


class PartnerProgram(Base):
    __tablename__ = "partner_program"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="2")
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    monthly_enabled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (CheckConstraint("id = 1 AND version IN (1, 2)", name="ck_partner_program"),)


class PartnerAccount(Base):
    __tablename__ = "partner_accounts"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), primary_key=True)
    current_rank: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="0")
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    protected_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_qualified_month: Mapped[date | None] = mapped_column(Date)
    last_review_month: Mapped[date | None] = mapped_column(Date)

    __table_args__ = (
        CheckConstraint("current_rank BETWEEN 0 AND 7", name="ck_partner_current_rank"),
    )


class PartnerRankReview(Base):
    __tablename__ = "partner_rank_reviews"

    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), primary_key=True)
    month: Mapped[date] = mapped_column(Date, primary_key=True)
    previous_rank: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    qualified_rank: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    new_rank: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    total: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[int] = mapped_column(Integer, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(String, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "previous_rank BETWEEN 0 AND 7 AND qualified_rank BETWEEN 0 AND 7 "
            "AND new_rank BETWEEN 0 AND 7 AND new_rank >= previous_rank - 1",
            name="ck_partner_review_rank",
        ),
        CheckConstraint("active BETWEEN 0 AND total", name="ck_partner_review_counts"),
        CheckConstraint(
            "(status = 'pending' AND applied_at IS NULL) OR "
            "(status = 'applied' AND applied_at IS NOT NULL)",
            name="ck_partner_review_status",
        ),
    )


class PartnerEarning(Base):
    __tablename__ = "partner_earnings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_accrual_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("rebate_accruals.id"), nullable=False, unique=True
    )
    referrer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    invitee_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    rank: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    share: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    asset: Mapped[str] = mapped_column(String, nullable=False)
    owner_fee_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    owner_fee_native: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    amount_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    amount_native: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, server_default="available")
    payout_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("payouts.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("referrer_id <> invitee_id", name="ck_partner_not_self"),
        CheckConstraint(
            "policy_version IN (1, 2) AND rank BETWEEN 0 AND 7", name="ck_partner_rank"
        ),
        CheckConstraint(
            "share = CASE rank WHEN 0 THEN 0 WHEN 1 THEN 0.20 WHEN 2 THEN 0.25 "
            "WHEN 3 THEN 0.30 WHEN 4 THEN 0.35 WHEN 5 THEN 0.40 "
            "WHEN 6 THEN 0.45 WHEN 7 THEN 0.50 END",
            name="ck_partner_share",
        ),
        CheckConstraint("asset IN ('USDT', 'MNT')", name="ck_partner_asset"),
        CheckConstraint(
            "owner_fee_usdt >= 0 AND owner_fee_native >= 0 AND amount_usdt >= 0 "
            "AND amount_native >= 0 AND amount_usdt <= owner_fee_usdt * share "
            "AND amount_native <= owner_fee_native * share",
            name="ck_partner_funded",
        ),
        CheckConstraint(
            "(state = 'available' AND payout_id IS NULL) OR "
            "(state IN ('reserved', 'paid') AND payout_id IS NOT NULL)",
            name="ck_partner_state",
        ),
        Index("ix_partner_balance", "referrer_id", "asset", "state"),
    )
