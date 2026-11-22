from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from bot.accrual.binance_recovery import Day, Target, reconcile, source_days
from bot.db.engine import make_sessionmaker
from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.ledger import RebateAccrual
from bot.db.models.ops import AuditEvent
from bot.exchanges.base import CommissionResult
from bot.exchanges.binance import POLICY

pytestmark = pytest.mark.asyncio(loop_scope="session")


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


async def test_preview_apply_and_repeat_preserve_paid_rows_and_claim_window(pg_engine):
    sessionmaker = make_sessionmaker(pg_engine)
    owner = 989817
    day = date(2026, 8, 30)
    end = date(2026, 9, 1)
    now = datetime(2026, 11, 5, tzinfo=UTC)
    async with sessionmaker.begin() as s:
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
    async with sessionmaker.begin() as s:
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
    async with sessionmaker.begin() as s:
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
