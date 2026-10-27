"""Users, wallets, exchange-account (UID) models. See DB_SCHEMA.md §1."""

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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from bot.db.base import Base

user_role = Enum("user", "dev", "admin", name="user_role")
user_status = Enum("active", "blocked", "manual_review", name="user_status")
wallet_status = Enum("active", "replaced", "disabled", "admin_hold", name="wallet_status")
account_status = Enum("active", "blocked", "archived", name="account_status")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # telegram_user_id
    role: Mapped[str] = mapped_column(user_role, nullable=False, server_default="user")
    status: Mapped[str] = mapped_column(user_status, nullable=False, server_default="active")
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    locale: Mapped[str] = mapped_column(String(8), nullable=False, server_default="ru")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # per-user fee override (only-down); see bot.payout.commission.effective_fee.
    commission_override_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    commission_override_since: Mapped[date | None] = mapped_column(Date)

    __table_args__ = (
        CheckConstraint(
            "locale IN ('ru','en','uk','zh','tr','hi','th','vi')", name="ck_user_locale"
        ),
    )


class Wallet(Base):
    __tablename__ = "wallets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    chain: Mapped[str] = mapped_column(String, nullable=False, server_default="BEP20")
    address_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    address_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    address_masked: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(wallet_status, nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    replaced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "uq_wallet_active",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_wallet_hash", "address_hash"),
        CheckConstraint("chain = 'BEP20'", name="ck_wallet_chain_bep20"),
    )


class ExchangeAccount(Base):
    __tablename__ = "exchange_accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    exchange: Mapped[str] = mapped_column(String, nullable=False)
    uid: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(account_status, nullable=False, server_default="active")
    # = REBATE_EPOCH or later; set by the accrual-worker layer (business 3.2).
    accrual_since: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("exchange", "uid", name="uq_exchange_uid"),
        Index("ix_exch_acc_user", "user_id"),
    )
