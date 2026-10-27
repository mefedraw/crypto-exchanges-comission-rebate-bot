"""Payout records with address snapshot, nonce, idempotency. See DB_SCHEMA.md §3."""

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
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from bot.db.base import Base

payout_status = Enum(
    "reserved",
    "pending_admin",
    "submitting",
    "sent",
    "confirmed",
    "paid",
    "failed",
    "cancelled",
    "skipped_below_min",
    name="payout_status",
)


class Payout(Base):
    __tablename__ = "payouts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)

    wallet_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("wallets.id"), nullable=False)
    address_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    address_masked: Mapped[str] = mapped_column(String, nullable=False)

    covered_from: Mapped[date] = mapped_column(Date, nullable=False)
    covered_to: Mapped[date] = mapped_column(Date, nullable=False)
    gross_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    commission_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    commission_override_usdt: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    overridden_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    override_reason: Mapped[str | None] = mapped_column(Text)
    gas_bnb: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    gas_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, server_default="0")
    bnb_usdt_rate: Mapped[Decimal | None] = mapped_column(Numeric(38, 18))
    net_usdt: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False)
    asset: Mapped[str] = mapped_column(String, nullable=False, server_default="USDT")
    purpose: Mapped[str] = mapped_column(String, nullable=False, server_default="rebate")
    net_native: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, server_default="0")

    status: Mapped[str] = mapped_column(payout_status, nullable=False, server_default="reserved")
    nonce: Mapped[int | None] = mapped_column(BigInteger)
    txhash: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    action_token: Mapped[str | None] = mapped_column(UUID(as_uuid=True))

    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    approved_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("purpose IN ('rebate', 'partner')", name="ck_payout_purpose"),
        UniqueConstraint("idempotency_key", name="uq_payout_idem"),
        Index(
            "uq_payout_active_nonce",
            "nonce",
            unique=True,
            postgresql_where="status IN ('submitting','sent')",
        ),
        Index("ix_payout_user", "user_id", "requested_at"),
    )
