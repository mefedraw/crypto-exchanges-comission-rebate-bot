from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.db.models.identity import Wallet
from bot.db.models.ops import Notification
from bot.db.models.partners import PartnerAccount, PartnerEarning, PartnerRankReview
from bot.db.models.payouts import Payout
from bot.executor import repository as executor_repo
from bot.partners import rank_control, service, withdrawal
from tests.partners.test_monthly_ranks import JAN, PARENT, active_month, at, seed_monthly
from tests.partners.test_service import new_source

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_parallel_monthly_review_applies_one_downgrade_with_cached_orm(committed_engine):
    sm = async_sessionmaker(committed_engine, expire_on_commit=False)
    async with sm() as session:
        accounts = await seed_monthly(session)
        await active_month(session, accounts[1:6], 4)
        session.add(
            PartnerAccount(
                user_id=PARENT,
                current_rank=7,
                promoted_at=at(1),
                protected_until=at(4),
                first_qualified_month=date(2026, 1, 1),
                last_review_month=date(2026, 3, 1),
            )
        )
        await session.commit()
    arrived = asyncio.Event()
    count = 0

    async def review():
        nonlocal count
        async with sm() as session:
            cached = await session.get(PartnerAccount, PARENT)
            assert cached.current_rank == 7
            count += 1
            if count == 2:
                arrived.set()
            await arrived.wait()
            await rank_control.refresh_rank(session, PARENT, at(5))
            await session.commit()

    await asyncio.wait_for(asyncio.gather(review(), review()), timeout=20)
    async with sm() as session:
        account = await session.get(PartnerAccount, PARENT)
        assert account.current_rank == 6
        assert await session.scalar(select(func.count()).select_from(PartnerRankReview)) == 1
        assert await session.scalar(select(func.count()).select_from(Notification)) == 1


async def test_monthly_changes_never_reprice_paid_earnings_and_apply_only_to_new_fees(
    db_session, monkeypatch
):
    db_session.sync_session.expire_on_commit = False
    accounts = await seed_monthly(db_session)
    monkeypatch.setattr(rank_control, "utc_now", lambda: JAN)
    # The first referee's lifetime check gives rank 7 once January activity is proven.
    old = await new_source(db_session, accounts[0], "1000", day=JAN.date())
    earned = await db_session.scalar(
        select(PartnerEarning).where(PartnerEarning.source_accrual_id == old.id)
    )
    assert earned.rank == 7 and earned.amount_usdt == 50 and earned.policy_version == 2
    db_session.add(
        Wallet(
            user_id=PARENT,
            address_encrypted=b"encrypted",
            address_hash=b"parent",
            address_masked="0x1234...abcd",
        )
    )
    await db_session.flush()
    preview = await withdrawal.prepare(db_session, PARENT, "USDT")
    token = await withdrawal.issue_token(db_session, PARENT, preview, JAN)
    payout = await withdrawal.confirm(db_session, PARENT, token, JAN)
    assert isinstance(payout, Payout)
    await executor_repo.mark_submitting(db_session, payout.id, 0)
    await executor_repo.mark_sent(db_session, payout.id, "0xtest", JAN)
    await executor_repo.finalize_paid(db_session, payout.id, JAN)
    frozen = (
        old.amount_usdt,
        old.commission_usdt,
        old.net_usdt,
        earned.amount_usdt,
        payout.net_usdt,
    )
    await active_month(db_session, accounts[1:6], 4)
    account = await rank_control.refresh_rank(db_session, PARENT, at(5))
    assert account.current_rank == 6 and earned.state == "paid"
    assert (
        old.amount_usdt,
        old.commission_usdt,
        old.net_usdt,
        earned.amount_usdt,
        payout.net_usdt,
    ) == frozen
    # Eight active 1k referees close rank 6 in May without reactivating the 10k referee.
    await active_month(db_session, accounts[1:9], 5)
    monkeypatch.setattr(rank_control, "utc_now", lambda: at(5, 2))
    new = await new_source(db_session, accounts[1], "100", day=at(5, 2).date())
    new_earning = await db_session.scalar(
        select(PartnerEarning).where(PartnerEarning.source_accrual_id == new.id)
    )
    assert new_earning.rank == 6 and new_earning.amount_usdt == Decimal("4.5")
    assert earned.amount_usdt == 50 and earned.state == "paid"
    summary = await service.summary(db_session, PARENT, at(5, 2))
    assert summary.rank.level == 6 and summary.balances["USDT"].paid == 50
    assert summary.balances["USDT"].available == Decimal("4.5")


async def test_rank_change_notifications_are_atomic_with_rollback(db_session):
    accounts = await seed_monthly(db_session)
    await active_month(db_session, accounts[:1], 1)
    with pytest.raises(RuntimeError, match="rollback"):
        async with db_session.begin_nested():
            await rank_control.refresh_rank(db_session, PARENT, JAN)
            raise RuntimeError("rollback")
    assert await db_session.scalar(select(func.count()).select_from(Notification)) == 0
    assert await db_session.get(PartnerAccount, PARENT) is None
