from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.ledger import RebateAccrual

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_two_assets_same_account_day_coexist(db_session: AsyncSession):
    session = db_session
    session.add(User(id=800, role="user", status="active"))
    await session.flush()
    acc = ExchangeAccount(
        user_id=800, exchange="bybit", uid="U800", status="active", accrual_since=date(2026, 7, 1)
    )
    session.add(acc)
    await session.flush()

    def _row(asset: str, native: Decimal, rate: Decimal) -> RebateAccrual:
        usdt = native * rate
        return RebateAccrual(
            user_id=800,
            account_id=acc.id,
            accrual_date=date(2026, 7, 5),
            asset=asset,
            amount_usdt=usdt,
            amount_native=native,
            net_native=native,
            fx_rate=rate,
            effective_rank=1,
            fee_pct=Decimal("0"),
            commission_usdt=Decimal("0"),
            net_usdt=usdt,
            state="available",
            available_at=datetime(2026, 7, 6, tzinfo=UTC),
            expires_at=datetime(2026, 8, 4, tzinfo=UTC),
            raw_records_count=1,
        )

    session.add(_row("USDT", Decimal("50"), Decimal("1")))
    session.add(_row("MNT", Decimal("100"), Decimal("0.6")))
    await session.flush()
    rows = (
        await session.scalars(select(RebateAccrual).where(RebateAccrual.account_id == acc.id))
    ).all()
    assert {r.asset for r in rows} == {"USDT", "MNT"}
    mnt = next(r for r in rows if r.asset == "MNT")
    assert mnt.amount_usdt == Decimal("60") and mnt.fx_rate == Decimal("0.6")
