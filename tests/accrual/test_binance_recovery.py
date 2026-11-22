from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from bot.accrual.adapters import MappingAdapterProvider
from bot.accrual.binance_recovery import Day, Target, reconcile, source_days
from bot.accrual.worker import AccrualWorker
from bot.admin.repository import recompute_current_month_unpaid
from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.ledger import RebateAccrual
from bot.db.models.ops import AuditEvent
from bot.exchanges.base import CommissionResult, ExchangeAdapter
from bot.exchanges.binance import POLICY
from tests.conftest import make_settings

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_surcharge_worker_recovery_and_personal_rate_use_same_earning_day(db_session):
    s = db_session
    prior, first = date(2026, 11, 22), date(2026, 11, 23)
    now = datetime(2026, 11, 27, tzinfo=UTC)
    owner = 989818

    class Days(ExchangeAdapter):
        def __init__(self, code):
            self.code = self.name = code

        async def get_commission(self, uid, date_from, date_to):
            return CommissionResult(
                self.code,
                uid,
                date_from,
                date_to,
                total_usdt=Decimal(5 if date_from.date() == prior else 100),
                settlement_note=POLICY if self.code == "binance" else None,
                raw_records_count=1,
            )

    s.add(User(id=owner, role="user", status="active"))
    await s.flush()
    accounts = [
        ExchangeAccount(
            user_id=owner, exchange=ex, uid=str(i + 1), status="active", accrual_since=prior
        )
        for i, ex in enumerate(("binance", "blofin"))
    ]
    s.add_all(accounts)
    await s.flush()
    worker = AccrualWorker(
        None,
        MappingAdapterProvider({a.exchange: Days(a.exchange) for a in accounts}),
        now=lambda: now,
        settings=make_settings(),
    )
    for _ in range(2):
        await worker._process_user(s, owner, accounts, first, first.replace(day=1), now)
    rows = (await s.scalars(select(RebateAccrual).where(RebateAccrual.user_id == owner))).all()
    assert len(rows) == 4
    amounts = {
        (r.account_id, r.accrual_date): (r.amount_usdt, r.commission_usdt, r.net_usdt) for r in rows
    }
    assert (
        amounts[accounts[0].id, prior]
        == amounts[accounts[1].id, prior]
        == (5, Decimal(".5"), Decimal("4.5"))
    )
    assert amounts[accounts[0].id, first] == (100, 11, 89)
    assert amounts[accounts[1].id, first] == (100, 10, 90)
    target = Target(owner, accounts[0].id, accounts[0].uid, prior, 2)
    checked = await reconcile(
        s, {target: {prior: Day(Decimal(5), 1), first: Day(Decimal(100), 1)}}, first, now
    )
    assert checked["accounts"][0]["new_days"] == 0
    await recompute_current_month_unpaid(
        s, owner, Decimal(0), first.replace(day=1), date(2026, 12, 1)
    )
    assert {(r.account_id, r.accrual_date): r.fee_pct for r in rows} == {
        (accounts[0].id, prior): 0,
        (accounts[1].id, prior): 0,
        (accounts[0].id, first): Decimal(".01"),
        (accounts[1].id, first): 0,
    }


async def test_period_control_must_match_daily_reports():
    day = date(2026, 8, 1)

    class Source:
        max_window_days = 30
        accrual_start = AsyncMock(return_value=day)

        async def get_commission(self, uid, start, end):
            r = CommissionResult("Binance", uid, start, end, settlement_note=POLICY)
            r.add_amount("USDT", Decimal("11") if start.date() != end.date() else Decimal("5"))
            return r.finalize()

    with pytest.raises(ValueError, match="daily sum"):
        await source_days(Source(), Target(1, 1, "123", day, 2), day + timedelta(days=1))


async def test_preview_apply_and_repeat_preserve_paid_rows_and_claim_window(db_session):
    s = db_session
    owner = 989817
    day = date(2026, 8, 30)
    end = date(2026, 9, 1)
    now = datetime(2026, 11, 5, tzinfo=UTC)
    async with s.begin():
        s.add(User(id=owner, role="user", status="active"))
        await s.flush()
        acc = ExchangeAccount(
            user_id=owner, uid="123456", exchange="binance", status="active", accrual_since=day
        )
        s.add(acc)
        await s.flush()
        target = Target(owner, acc.id, acc.uid, day, 2)
    sources = {
        target: {
            day: Day(Decimal("125"), 1),
            day + timedelta(days=1): Day(Decimal("10"), 1),
            end: Day(Decimal("20"), 1),
        }
    }
    async with s.begin():
        preview = await reconcile(s, sources, end, now)
        assert Decimal(preview["accounts"][0]["new_net"]) == Decimal("140.40")
        assert (
            len(
                (await s.scalars(select(RebateAccrual).where(RebateAccrual.user_id == owner))).all()
            )
            == 0
        )
        with pytest.raises(ValueError, match="plan changed"):
            await reconcile(s, sources, end, now, "wrong")
        await reconcile(s, sources, end, now, preview["digest"])
    async with s.begin():
        rows = (
            await s.scalars(
                select(RebateAccrual)
                .where(RebateAccrual.user_id == owner)
                .order_by(RebateAccrual.accrual_date)
            )
        ).all()
        assert [r.fee_pct for r in rows] == [Decimal(".10"), Decimal(".07"), Decimal(".07")]
        assert all(r.expires_at == now + timedelta(days=30) for r in rows)
        rows[0].state = "paid"
        await s.flush()
        repeat = await reconcile(s, sources, end, now + timedelta(days=4))
        assert repeat["accounts"][0]["new_days"] == 0
        await reconcile(s, sources, end, now + timedelta(days=4), repeat["digest"])
        assert rows[0].state == "paid" and rows[0].expires_at == now + timedelta(days=30)
        assert (
            len(
                (
                    await s.scalars(
                        select(AuditEvent).where(
                            AuditEvent.entity_id == str(target.account),
                            AuditEvent.actor == "binance-recovery",
                        )
                    )
                ).all()
            )
            == 1
        )
