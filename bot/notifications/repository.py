# bot/notifications/repository.py
"""Outbox persistence for cross-process Telegram notifications.

Producers (executor/accrual) call :func:`enqueue`; the bot's poller claims
undelivered rows with FOR UPDATE SKIP LOCKED, sends them, and marks them
delivered. Kinds: ``payout_paid`` / ``payout_failed`` (to the user) and
``dev_alert`` (to the developer).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models.ops import Notification

PAYOUT_PAID = "payout_paid"
PAYOUT_FAILED = "payout_failed"
DEV_ALERT = "dev_alert"


async def enqueue(
    session: AsyncSession,
    *,
    recipient_chat_id: int,
    kind: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Add a notification to the outbox (caller commits with its own transaction)."""
    session.add(Notification(recipient_chat_id=recipient_chat_id, kind=kind, payload=payload))


async def claim_undelivered(
    session: AsyncSession, limit: int = 20, max_attempts: int = 10
) -> list[Notification]:
    """Lock and return the oldest undelivered notifications (skip already-locked rows).

    Rows that have failed ``max_attempts`` times are left alone (still undelivered,
    visible via ``attempts``) so a blocked recipient can't spin the poller forever.
    """
    stmt = (
        select(Notification)
        .where(Notification.delivered_at.is_(None), Notification.attempts < max_attempts)
        .order_by(Notification.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list((await session.scalars(stmt)).all())


async def mark_delivered(session: AsyncSession, notification_id: int, now: datetime) -> None:
    await session.execute(
        update(Notification).where(Notification.id == notification_id).values(delivered_at=now)
    )


async def bump_attempt(session: AsyncSession, notification_id: int) -> None:
    """Record a failed send attempt so a stuck row is visible (and still retried)."""
    await session.execute(
        update(Notification)
        .where(Notification.id == notification_id)
        .values(attempts=Notification.attempts + 1)
    )
