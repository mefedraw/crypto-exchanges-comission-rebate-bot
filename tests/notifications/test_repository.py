# tests/notifications/test_repository.py
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models.ops import Notification
from bot.notifications import repository as repo

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


async def test_enqueue_then_claim_then_mark_delivered(db_session: AsyncSession):
    await repo.enqueue(
        db_session, recipient_chat_id=555, kind=repo.PAYOUT_PAID, payload={"net_usdt": "14.64"}
    )
    await db_session.flush()

    claimed = await repo.claim_undelivered(db_session)
    assert len(claimed) == 1
    assert claimed[0].recipient_chat_id == 555
    assert claimed[0].kind == repo.PAYOUT_PAID

    await repo.mark_delivered(db_session, claimed[0].id, NOW)
    await db_session.flush()

    # delivered rows are no longer claimable
    assert await repo.claim_undelivered(db_session) == []


async def test_claim_orders_oldest_first(db_session: AsyncSession):
    await repo.enqueue(db_session, recipient_chat_id=1, kind=repo.DEV_ALERT, payload={"title": "a"})
    await db_session.flush()
    await repo.enqueue(db_session, recipient_chat_id=2, kind=repo.DEV_ALERT, payload={"title": "b"})
    await db_session.flush()

    claimed = await repo.claim_undelivered(db_session)
    assert [n.recipient_chat_id for n in claimed] == [1, 2]


async def test_attempts_cap_stops_claiming(db_session: AsyncSession):
    db_session.add(
        Notification(recipient_chat_id=9, kind=repo.PAYOUT_FAILED, payload={}, attempts=10)
    )
    await db_session.flush()
    # attempts >= max_attempts (10) -> not claimed
    assert await repo.claim_undelivered(db_session, max_attempts=10) == []


async def test_bump_attempt_increments(db_session: AsyncSession):
    await repo.enqueue(db_session, recipient_chat_id=7, kind=repo.PAYOUT_PAID, payload={})
    await db_session.flush()
    nid = (await db_session.scalars(select(Notification.id))).one()

    await repo.bump_attempt(db_session, nid)
    await db_session.flush()
    row = (await db_session.scalars(select(Notification).where(Notification.id == nid))).one()
    assert row.attempts == 1
