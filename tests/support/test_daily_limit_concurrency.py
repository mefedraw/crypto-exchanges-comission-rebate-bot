from __future__ import annotations

import asyncio
from datetime import date

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.ledger import RebateAccrual
from bot.db.models.ops import AuditEvent, Notification
from bot.db.models.support import SupportCase, SupportMessage
from bot.support import service
from tests.support.test_service import ADMINS, NOW, opened, seed

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_two_concurrent_different_uid_submissions_cannot_create_a_third_case(pg_engine):
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    user_id = 81199
    async with sm() as session, session.begin():
        first = await seed(session, user_id=user_id)
        others = [
            ExchangeAccount(
                user_id=user_id,
                exchange="gate",
                uid=f"daily-limit-{i}",
                status="active",
                accrual_since=date(2026, 7, 10),
            )
            for i in range(2)
        ]
        session.add_all(others)
        await session.flush()
        account_ids = [a.id for a in others]
        await opened(session, first)

    ready = asyncio.Event()
    waiting = 0

    async def submit(account_id, token):
        nonlocal waiting
        async with sm() as session:
            waiting += 1
            if waiting == 2:
                ready.set()
            await ready.wait()
            try:
                receipt = await service.open_case(
                    session,
                    user_id=user_id,
                    account_id=account_id,
                    body="Concurrent case",
                    token=token,
                    admin_ids=ADMINS,
                    now=NOW,
                )
                await session.commit()
                return receipt.created
            except service.CaseError as exc:
                await session.rollback()
                assert "24 часа" in str(exc) and "2 обращения" in str(exc)
                return False

    try:
        assert sorted(
            await asyncio.gather(submit(account_ids[0], "b" * 24), submit(account_ids[1], "c" * 24))
        ) == [False, True]
        async with sm() as session:
            ids = select(SupportCase.id).where(SupportCase.user_id == user_id)
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(SupportCase)
                    .where(SupportCase.user_id == user_id)
                )
                == 2
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(SupportMessage)
                    .where(SupportMessage.case_id.in_(ids))
                )
                == 2
            )
    finally:
        async with sm() as session, session.begin():
            ids = list(
                (
                    await session.scalars(
                        select(SupportCase.id).where(SupportCase.user_id == user_id)
                    )
                ).all()
            )
            for case_id in ids:
                await session.execute(
                    delete(Notification).where(
                        Notification.payload["case_id"].astext == str(case_id)
                    )
                )
                await session.execute(
                    delete(AuditEvent).where(
                        AuditEvent.entity_type == "support_case",
                        AuditEvent.entity_id == str(case_id),
                    )
                )
            await session.execute(delete(SupportMessage).where(SupportMessage.case_id.in_(ids)))
            await session.execute(delete(SupportCase).where(SupportCase.user_id == user_id))
            await session.execute(delete(RebateAccrual).where(RebateAccrual.user_id == user_id))
            await session.execute(delete(ExchangeAccount).where(ExchangeAccount.user_id == user_id))
            await session.execute(delete(User).where(User.id == user_id))
