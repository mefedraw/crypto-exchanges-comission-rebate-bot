from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.ledger import RebateAccrual
from bot.db.models.ops import AuditEvent, Notification
from bot.db.models.support import SupportCase, SupportMessage
from bot.support import service, statuses
from tests.support.test_service import ADMINS, NOW, opened, seed

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.mark.parametrize("action", ["take", "close"])
async def test_concurrent_admin_actions_refresh_cached_case_and_apply_once(pg_engine, action):
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    user_id = 81288
    async with sm() as session, session.begin():
        account = await seed(session, user_id=user_id)
        case_id = (await opened(session, account)).case.id
    ready = asyncio.Event()
    waiting = 0
    target = statuses.OPEN if action == "take" else statuses.CLOSED

    async def change(actor):
        nonlocal waiting
        async with sm() as session:
            cached = await session.get(SupportCase, case_id)
            assert cached.status == statuses.SENT
            waiting += 1
            if waiting == 2:
                ready.set()
            await ready.wait()
            method = service.take_case if action == "take" else service.close_case
            receipt = await method(
                session, case_id=case_id, actor_id=actor, admin_ids=ADMINS, now=NOW
            )
            await session.commit()
            assert cached.status == target
            return receipt.created

    try:
        assert sorted(await asyncio.gather(change(9001), change(9002))) == [False, True]
        async with sm() as session:
            assert (await session.get(SupportCase, case_id)).status == target
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(
                        AuditEvent.action == "support_status_changed",
                        AuditEvent.entity_id == str(case_id),
                    )
                )
                == 1
            )
            assert await session.scalar(
                select(func.count())
                .select_from(Notification)
                .where(Notification.recipient_chat_id == user_id)
            ) == (1 if action == "close" else 0)
    finally:
        async with sm() as session, session.begin():
            await session.execute(
                delete(Notification).where(Notification.payload["case_id"].astext == str(case_id))
            )
            await session.execute(
                delete(AuditEvent).where(
                    AuditEvent.entity_type == "support_case", AuditEvent.entity_id == str(case_id)
                )
            )
            await session.execute(delete(SupportMessage).where(SupportMessage.case_id == case_id))
            await session.execute(delete(SupportCase).where(SupportCase.id == case_id))
            await session.execute(delete(RebateAccrual).where(RebateAccrual.user_id == user_id))
            await session.execute(delete(ExchangeAccount).where(ExchangeAccount.user_id == user_id))
            await session.execute(delete(User).where(User.id == user_id))
