"""Successful normalized API snapshots, never a spendable financial ledger."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bot.db.base import Base


class CommissionFetch(Base):
    __tablename__ = "commission_fetches"

    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("exchange_accounts.id"), primary_key=True
    )
    accrual_date: Mapped[date] = mapped_column(Date, primary_key=True)
    query_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    query_to: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
