from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from bot.accrual import repository as accrual_repo
from bot.admin import repository as admin_repo
from bot.botui import repository as cabinet_repo
from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.invites import InviteAttribution
from bot.db.models.ledger import RebateAccrual
from bot.db.models.partners import PartnerEarning, PartnerProgram
from bot.invites.service import current_code
from bot.partners import service

pytestmark = pytest.mark.asyncio(loop_scope="session")
NOW = datetime(2026, 10, 29, 12, tzinfo=UTC)
START = date(2026, 10, 28)
PARENT = 84001


async def seed(session, grosses=("50", "50", "50"), *, parent=PARENT):
    session.add(PartnerProgram(id=1, starts_on=START, version=1))
    session.add(User(id=parent, role="user", status="active"))
    await session.flush()
    code = await current_code(session, parent)
    accounts = []
    for i, gross in enumerate(grosses, start=1):
        user_id = parent + i
        session.add(User(id=user_id, role="user", status="active"))
        await session.flush()
        session.add(
            InviteAttribution(
                invitee_id=user_id,
                referrer_id=parent,
                code_id=code.id,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        account = ExchangeAccount(
            user_id=user_id,
            exchange="gate",
            uid=str(user_id),
            status="active",
            accrual_since=date(2026, 1, 1),
        )
        session.add(account)
        await session.flush()
        row = historical(account, Decimal(gross))
        session.add(row)
        accounts.append(account)
    await session.flush()
    return accounts


def historical(account, gross, *, day=date(2026, 10, 1), asset="USDT", finalized=True):
    return RebateAccrual(
        user_id=account.user_id,
        account_id=account.id,
        accrual_date=day,
        asset=asset,
        amount_usdt=gross,
        amount_native=gross,
        net_usdt=gross * Decimal(".9"),
        net_native=gross * Decimal(".9"),
        commission_usdt=gross * Decimal(".1"),
        effective_rank=1,
        fee_pct=Decimal(".1"),
        state="available",
        available_at=NOW,
        expires_at=NOW + timedelta(days=30),
        finalized=finalized,
        fetched_at=datetime(2026, 10, 26, tzinfo=UTC),
    )


async def new_source(session, account, amount="100", *, day=START, asset="USDT", fx="1"):
    gross, rate = Decimal(amount), Decimal(fx)
    native = gross / rate
    await accrual_repo.upsert_accrual(
        session,
        user_id=account.user_id,
        account_id=account.id,
        accrual_date=day,
        asset=asset,
        amount_usdt=gross,
        amount_native=native,
        net_usdt=gross * Decimal(".9"),
        net_native=native * Decimal(".9"),
        fx_rate=rate,
        commission_usdt=gross * Decimal(".1"),
        effective_rank=1,
        fee_pct=Decimal(".1"),
        state="available",
        available_at=NOW,
        expires_at=NOW + timedelta(days=30),
        raw_records_count=1,
        adapter_notes=None,
    )
    return await session.scalar(
        select(RebateAccrual)
        .where(
            RebateAccrual.account_id == account.id,
            RebateAccrual.accrual_date == day,
            RebateAccrual.asset == asset,
        )
        .execution_options(populate_existing=True)
    )


async def earning_for(session, row):
    return await session.scalar(
        select(PartnerEarning).where(PartnerEarning.source_accrual_id == row.id)
    )


async def test_source_fee_not_invitee_net_funds_immediate_reward(db_session):
    accounts = await seed(db_session)
    row = await new_source(db_session, accounts[0])
    earning = await earning_for(db_session, row)
    assert earning.rank == 1 and earning.share == Decimal(".2")
    assert earning.owner_fee_usdt == 10 and earning.amount_usdt == 2
    assert row.amount_usdt == 100 and row.commission_usdt == 10 and row.net_usdt == 90
    summary = await service.summary(db_session, PARENT)
    assert summary.balances["USDT"].available == 2
    assert summary.counts == (3, 0, 0, 0)
    overview = await admin_repo.admin_overview(
        db_session, month_start=START, month_before=date(2026, 11, 1)
    )
    assert overview["owner_profit_total"] + overview["partner_total"] == overview["profit_total"]


async def test_rank_seven_requires_only_one_ten_thousand_invitee(db_session):
    accounts = await seed(db_session, ("10000",))
    row = await new_source(db_session, accounts[0])
    earning = await earning_for(db_session, row)
    assert earning.rank == 7 and earning.amount_usdt == 5


async def test_lifetime_gross_not_available_balance_or_uid_count(db_session):
    accounts = await seed(db_session, ("49.999999999999999999", "200", "1000"))
    old = await db_session.scalar(
        select(RebateAccrual).where(RebateAccrual.account_id == accounts[1].id)
    )
    old.state = "paid"
    accounts[2].status = "archived"
    second = ExchangeAccount(
        user_id=accounts[0].user_id,
        exchange="bitget",
        uid="other",
        status="active",
        accrual_since=START,
    )
    db_session.add(second)
    await db_session.flush()
    db_session.add(historical(second, Decimal(".000000000000000001")))
    db_session.add(historical(second, Decimal("10000"), day=date(2026, 10, 2), finalized=False))
    await db_session.flush()
    assert await service.qualification_counts(db_session, PARENT) == (3, 2, 1, 0)
    await cabinet_repo.archive_account(db_session, accounts[0].user_id, second.id)
    assert await service.qualification_counts(db_session, PARENT) == (3, 2, 1, 0)


async def test_rank_zero_marker_never_becomes_retroactive_reward(db_session):
    accounts = await seed(db_session, ("0", "0", "0"))
    old = await new_source(db_session, accounts[0], "1")
    marker = await earning_for(db_session, old)
    assert marker.rank == 0 and marker.amount_usdt == 0
    for account in accounts:
        await new_source(db_session, account, "100", day=date(2026, 10, 29))
    await service.capture_accrual(db_session, old)
    assert marker.amount_usdt == 0 and marker.rank == 0
    assert (await service.summary(db_session, PARENT)).rank.level == 1


async def test_prelaunch_and_corrections_never_create_backpay(db_session):
    accounts = await seed(db_session)
    old = await new_source(db_session, accounts[0], day=date(2026, 10, 27))
    assert await earning_for(db_session, old) is None
    old.accrual_date = START
    old.correction_key = "operator-correction-test"
    old.fee_pct = None
    await db_session.flush()
    assert await service.capture_accrual(db_session, old) is None


async def test_retries_and_fee_discounts_cannot_remove_reward_funding(db_session):
    accounts = await seed(db_session)
    row = await new_source(db_session, accounts[0])
    earning = await earning_for(db_session, row)
    await new_source(db_session, accounts[0], "200")
    assert row.amount_usdt == 100 and earning.amount_usdt == 2
    assert await db_session.scalar(select(func.count()).select_from(PartnerEarning)) == 1
    count = await admin_repo.recompute_current_month_unpaid(
        db_session, row.user_id, Decimal(0), START, date(2026, 11, 1)
    )
    assert count == 0
    await db_session.refresh(row)
    assert row.commission_usdt == 10 and row.net_usdt == 90


async def test_mnt_remains_native_and_never_enters_usdt_pool(db_session):
    accounts = await seed(db_session)
    row = await new_source(db_session, accounts[0], asset="MNT", fx="2")
    earning = await earning_for(db_session, row)
    assert earning.owner_fee_native == 5 and earning.amount_native == 1
    summary = await service.summary(db_session, PARENT)
    assert summary.balances["MNT"].available == 2
    assert summary.balances["MNT"].available_native == 1
    assert summary.balances["USDT"].available == 0


async def test_capture_rolls_back_with_source(db_session):
    accounts = await seed(db_session)
    with pytest.raises(RuntimeError, match="rollback"):
        async with db_session.begin_nested():
            await new_source(db_session, accounts[0])
            raise RuntimeError("rollback")
    assert await db_session.scalar(select(func.count()).select_from(PartnerEarning)) == 0
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(RebateAccrual)
            .where(RebateAccrual.accrual_date >= START)
        )
        == 0
    )
