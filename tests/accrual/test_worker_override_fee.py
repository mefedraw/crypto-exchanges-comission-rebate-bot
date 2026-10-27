# tests/accrual/test_worker_override_fee.py
"""Per-user commission override integration into the accrual worker (Phase 6,
Task 2). Mirrors tests/accrual/test_worker.py's FakeAdapter/seed helpers.

Money rules under test (see bot/payout/commission.py::effective_fee):
  - since-gated: override applies to a day only if `d >= commission_override_since`.
  - only-down: the override can never raise the fee above the rank's base fee.
  - freeze: the resulting fee is baked into the frozen row's fee_pct/commission_usdt.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.accrual.adapters import MappingAdapterProvider
from bot.accrual.worker import AccrualWorker
from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.ledger import RebateAccrual
from bot.exchanges.base import CommissionResult, ExchangeAdapter

pytestmark = pytest.mark.asyncio(loop_scope="session")


class FakeAdapter(ExchangeAdapter):
    """Returns scripted per-day USDT amounts keyed by date."""

    name = "Bybit"
    code = "bybit"

    def __init__(self, by_day: dict[date, Decimal]) -> None:
        self._by_day = by_day

    async def get_commission(self, uid, date_from, date_to):  # type: ignore[override]
        amount = self._by_day.get(date_from.date(), Decimal(0))
        return CommissionResult(
            exchange="Bybit",
            uid=uid,
            date_from=date_from,
            date_to=date_to,
            total_usdt=amount,
        )


@pytest.fixture
def sessionmaker(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)


async def _seed_user_account(
    sm,
    user_id: int,
    since: date,
    *,
    exchange: str = "bybit",
    override_pct: Decimal | None = None,
    override_since: date | None = None,
) -> int:
    async with sm() as s:
        s.add(
            User(
                id=user_id,
                role="user",
                status="active",
                commission_override_pct=override_pct,
                commission_override_since=override_since,
            )
        )
        await s.flush()
        acc = ExchangeAccount(
            user_id=user_id,
            exchange=exchange,
            uid=f"U{user_id}",
            status="active",
            accrual_since=since,
        )
        s.add(acc)
        await s.flush()
        acc_id = acc.id
        await s.commit()
    return acc_id


def _now_at(d: date) -> datetime:
    # A UTC instant that is 'd' 12:00 in UTC+8 -> today in PAYOUT_TZ is d.
    return datetime(d.year, d.month, d.day, 4, 0, tzinfo=UTC)


# $20/day keeps cum_before <= 100 through July 15 -> rank stays 1 every day
# (see bot/payout/rank_timing.py::day_effective_rank).
_DAILY = {date(2026, 7, d): Decimal("20") for d in range(10, 16)}


async def test_override_lowers_fee_from_since_date(sessionmaker):
    acc_id = await _seed_user_account(
        sessionmaker,
        610,
        date(2026, 7, 10),
        override_pct=Decimal("0.02"),
        override_since=date(2026, 7, 12),
    )
    worker = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider({"bybit": FakeAdapter(_DAILY)}),
        now=lambda: _now_at(date(2026, 7, 16)),  # last_closed = July 15
    )
    await worker.run_cycle()

    async with sessionmaker() as s:
        rows = {
            r.accrual_date: r
            for r in (
                await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == acc_id))
            ).all()
        }
    assert len(rows) == 6  # July 10..15 inclusive

    for d in (date(2026, 7, 10), date(2026, 7, 11)):
        assert rows[d].effective_rank == 1
        assert rows[d].fee_pct == Decimal("0.10")  # pre-`since` day: plain rank fee
        assert rows[d].commission_usdt == Decimal("2.00")  # 20 * 0.10

    for d in (date(2026, 7, 12), date(2026, 7, 13), date(2026, 7, 14), date(2026, 7, 15)):
        assert rows[d].effective_rank == 1
        assert rows[d].fee_pct == Decimal("0.02")  # on/after `since`: override wins (only-down)
        assert rows[d].commission_usdt == Decimal("0.40")  # 20 * 0.02
        assert rows[d].net_usdt == Decimal("19.60")


async def test_override_above_rank_fee_never_raises_it(sessionmaker):
    acc_id = await _seed_user_account(
        sessionmaker,
        611,
        date(2026, 7, 10),
        override_pct=Decimal("0.20"),  # above rank-1's 0.10
        override_since=date(2026, 7, 1),  # in effect for the whole range
    )
    worker = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider({"bybit": FakeAdapter(_DAILY)}),
        now=lambda: _now_at(date(2026, 7, 16)),
    )
    await worker.run_cycle()

    async with sessionmaker() as s:
        rows = (
            await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == acc_id))
        ).all()
    assert len(rows) == 6
    for r in rows:
        assert r.fee_pct == Decimal("0.10")  # only-down: override above base is ignored
        assert r.commission_usdt == Decimal("2.00")


async def test_no_override_keeps_plain_rank_fee(sessionmaker):
    acc_id = await _seed_user_account(sessionmaker, 612, date(2026, 7, 10))
    worker = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider({"bybit": FakeAdapter(_DAILY)}),
        now=lambda: _now_at(date(2026, 7, 16)),
    )
    await worker.run_cycle()

    async with sessionmaker() as s:
        rows = (
            await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == acc_id))
        ).all()
    assert len(rows) == 6
    for r in rows:
        assert r.fee_pct == Decimal("0.10")
        assert r.commission_usdt == Decimal("2.00")
        assert r.net_usdt == Decimal("18.00")
