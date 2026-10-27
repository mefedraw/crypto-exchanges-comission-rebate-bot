"""Opaque invitation codes and immutable first-registration attribution."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from bot.db.base import Base


class InviteCode(Base):
    __tablename__ = "invite_codes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("code ~ '^[a-z0-9_-]{5,32}$'", name="ck_invite_code_format"),
        Index("uq_invite_current_user", "user_id", unique=True, postgresql_where="is_current"),
        Index("ix_invite_codes_user", "user_id"),
    )


class InviteAttribution(Base):
    __tablename__ = "invite_attributions"

    invitee_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), primary_key=True)
    referrer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    code_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("invite_codes.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("invitee_id <> referrer_id", name="ck_invite_not_self"),
        Index("ix_invite_referrer_created", "referrer_id", "created_at"),
    )
