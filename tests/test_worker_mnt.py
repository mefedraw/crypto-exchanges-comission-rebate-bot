# tests/test_worker_mnt.py
"""Task 5: the accrual worker freezes BOTH a USDT and an MNT row per Bybit
account/day, valuing MNT in USDT-equivalent so it counts toward rank.

Deliberately a FLAT file (not tests/accrual/test_worker_mnt.py) — a new
tests/<pkg>/ directory gets its own session-scoped Postgres-container fixture
(see tests/accrual/conftest.py et al.), and one more of those trips a
testcontainers/pytest-asyncio session-loop interaction that breaks ~37
unrelated tests.

The engine fixture below is deliberately FUNCTION-scoped (not "session", unlike
the per-package conftests) for the same reason: this file sorts alphabetically
after every package directory but among the OTHER flat tests/test_*.py files
(most of which run with the default per-test event loop, no loop_scope
override). A session-scoped async engine created under pytest-asyncio's
"session" loop, then reused after intervening default-scoped tests have each
spun up and torn down their own loop, ends up bound to a stale loop — asyncpg
then raises "got Future ... attached to a different loop". A fresh
function-scoped engine per test (cheap: `create_all` is a no-op past the first
call, and the container itself is still just started once, module-scoped)
sidesteps the whole cross-test loop-lifecycle question.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.postgres import PostgresContainer

from bot.accrual.adapters import MappingAdapterProvider
from bot.accrual.worker import AccrualWorker
from bot.db.base import Base
from bot.db.engine import make_engine
from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.ledger import RebateAccrual
from bot.exchanges.base import CommissionLine, CommissionResult, ExchangeAdapter
from tests.conftest import make_settings

pytestmark = pytest.mark.asyncio(loop_scope="function")


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest_asyncio.fixture(loop_scope="function")
async def pg_engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(pg_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True, loop_scope="function")
async def _clear_downtime_state(pg_engine: AsyncEngine) -> AsyncIterator[None]:
    async with pg_engine.begin() as conn:
        await conn.execute(text("DELETE FROM service_availability"))
        await conn.execute(text("DELETE FROM system_flags WHERE key = 'accrual_heartbeat'"))
    yield


class BybitStub(ExchangeAdapter):
    """Scripted per-day lines (USDT/MNT), plus a stubbed mnt_usdt_rate()."""

    name = "Bybit"
    code = "bybit"

    def __init__(self, lines_by_day: dict[date, list[CommissionLine]], rate: Decimal) -> None:
        self._lines_by_day = lines_by_day
        self._rate = rate
        self.rate_calls = 0

    async def get_commission(self, uid, date_from, date_to):  # type: ignore[override]
        lines = list(self._lines_by_day.get(date_from.date(), []))
        return CommissionResult(
            exchange="Bybit", uid=uid, date_from=date_from, date_to=date_to, lines=lines
        )

    async def mnt_usdt_rate(self) -> Decimal:
        self.rate_calls += 1
        return self._rate


class RaisingMntAdapter(ExchangeAdapter):
    """Same USDT/MNT lines as BybitStub, but mnt_usdt_rate() always raises —
    proves a failed rate fetch skips MNT for the cycle without blocking USDT."""

    name = "Bybit"
    code = "bybit"

    def __init__(self, lines_by_day: dict[date, list[CommissionLine]]) -> None:
        self._lines_by_day = lines_by_day

    async def get_commission(self, uid, date_from, date_to):  # type: ignore[override]
        lines = list(self._lines_by_day.get(date_from.date(), []))
        return CommissionResult(
            exchange="Bybit", uid=uid, date_from=date_from, date_to=date_to, lines=lines
        )

    async def mnt_usdt_rate(self) -> Decimal:
        raise RuntimeError("rate feed down")


class UsdtOnlyAdapter(ExchangeAdapter):
    """A plain (non-Bybit-shaped) USDT-only adapter, e.g. Gate/MEXC — no MNT at
    all, not even a mnt_usdt_rate method."""

    name = "Gate"
    code = "gate"

    def __init__(self, by_day: dict[date, Decimal]) -> None:
        self._by_day = by_day

    async def get_commission(self, uid, date_from, date_to):  # type: ignore[override]
        amount = self._by_day.get(date_from.date(), Decimal(0))
        return CommissionResult(
            exchange="Gate", uid=uid, date_from=date_from, date_to=date_to, total_usdt=amount
        )


@pytest.fixture
def sessionmaker(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)


async def _seed_user_account(sm, user_id: int, since: date, exchange: str) -> int:
    async with sm() as s:
        s.add(User(id=user_id, role="user", status="active"))
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


async def test_bybit_account_freezes_usdt_and_mnt_rows_at_same_rank(sessionmaker):
    acc_id = await _seed_user_account(sessionmaker, 700, date(2026, 7, 10), exchange="bybit")
    adapter = BybitStub(
        {
            date(2026, 7, 10): [
                CommissionLine(asset="USDT", amount=Decimal("50")),
                CommissionLine(asset="MNT", amount=Decimal("100")),
            ]
        },
        rate=Decimal("0.6"),
    )
    worker = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider({"bybit": adapter}),
        now=lambda: _now_at(date(2026, 7, 11)),  # today=Jul11 -> only Jul10 closed
        settings=make_settings(mantle_rpc_url="https://rpc.mantle.xyz"),
    )
    await worker.run_cycle()

    async with sessionmaker() as s:
        rows = {
            r.asset: r
            for r in (
                await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == acc_id))
            ).all()
        }

    assert set(rows) == {"USDT", "MNT"}
    usdt_row, mnt_row = rows["USDT"], rows["MNT"]

    # USDT row: native == usdt-equiv, fx == 1.
    assert usdt_row.amount_usdt == Decimal("50")
    assert usdt_row.amount_native == Decimal("50")
    assert usdt_row.fx_rate == Decimal("1")

    # MNT row: valued at the stubbed rate (100 * 0.6 = 60), native kept as-is.
    assert mnt_row.amount_usdt == Decimal("60")
    assert mnt_row.amount_native == Decimal("100")
    assert mnt_row.fx_rate == Decimal("0.6")

    # Rank is driven by the day's combined USDT-equiv gross (50 + 60 = 110), and
    # applies identically to both assets (no MNT surcharge).
    assert usdt_row.effective_rank == mnt_row.effective_rank
    assert usdt_row.fee_pct == mnt_row.fee_pct
    fee = usdt_row.fee_pct
    assert mnt_row.net_native == Decimal("100") * (Decimal(1) - fee)
    assert mnt_row.net_usdt == Decimal("60") - Decimal("60") * fee

    # The rate is fetched at most once per cycle, not once per account/day.
    assert adapter.rate_calls == 1


async def test_usdt_only_account_still_produces_exactly_one_row(sessionmaker):
    """Regression: a non-Bybit (or Bybit-without-MNT) account must not gain a
    spurious second row now that two-asset freezing exists."""
    acc_id = await _seed_user_account(sessionmaker, 701, date(2026, 7, 10), exchange="gate")
    adapter = UsdtOnlyAdapter({date(2026, 7, 10): Decimal("40")})
    worker = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider({"gate": adapter}),
        now=lambda: _now_at(date(2026, 7, 11)),
        settings=make_settings(mantle_rpc_url="https://rpc.mantle.xyz"),  # MNT enabled globally
    )
    await worker.run_cycle()

    async with sessionmaker() as s:
        rows = (
            await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == acc_id))
        ).all()

    assert len(rows) == 1
    assert rows[0].asset == "USDT"
    assert rows[0].amount_usdt == Decimal("40")


async def test_mnt_disabled_skips_mnt_row_but_freezes_usdt(sessionmaker):
    acc_id = await _seed_user_account(sessionmaker, 702, date(2026, 7, 10), exchange="bybit")
    adapter = BybitStub(
        {
            date(2026, 7, 10): [
                CommissionLine(asset="USDT", amount=Decimal("50")),
                CommissionLine(asset="MNT", amount=Decimal("100")),
            ]
        },
        rate=Decimal("0.6"),
    )
    worker = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider({"bybit": adapter}),
        now=lambda: _now_at(date(2026, 7, 11)),
        settings=make_settings(),  # mnt_enabled defaults to False (no MANTLE_RPC_URL)
    )
    await worker.run_cycle()

    async with sessionmaker() as s:
        rows = (
            await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == acc_id))
        ).all()

    assert len(rows) == 1
    assert rows[0].asset == "USDT"
    assert rows[0].amount_usdt == Decimal("50")  # rank driven by USDT alone, MNT ignored
    assert adapter.rate_calls == 0  # rate never fetched when MNT is disabled


async def test_mnt_rate_fetch_failure_does_not_partially_freeze_and_recovers(sessionmaker):
    """Regression for the critical money-loss bug: the OLD (buggy) behavior froze
    the USDT row while the rate fetch failed, which finalized the day and
    permanently advanced ``last_finalized_day`` past it — so even after the rate
    recovered, that day's MNT could never be frozen (and the day's rank, computed
    from a partial gross, was corrupted). The fix must NOT freeze ANY row (not
    even USDT) for a Bybit-bearing user's not-yet-finalized days while the MNT
    rate is unavailable, so the day stays open and both rows freeze together —
    at the correct shared rank — once the rate recovers."""
    acc_id = await _seed_user_account(sessionmaker, 703, date(2026, 7, 10), exchange="bybit")
    lines_by_day = {
        date(2026, 7, 10): [
            CommissionLine(asset="USDT", amount=Decimal("50")),
            CommissionLine(asset="MNT", amount=Decimal("100")),
        ]
    }

    # Cycle 1: rate fetch fails outright.
    failing_adapter = RaisingMntAdapter(lines_by_day)
    worker = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider({"bybit": failing_adapter}),
        now=lambda: _now_at(date(2026, 7, 11)),
        settings=make_settings(mantle_rpc_url="https://rpc.mantle.xyz"),
    )
    await worker.run_cycle()  # must not raise — the rate-fetch failure is swallowed

    async with sessionmaker() as s:
        rows = (
            await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == acc_id))
        ).all()
    assert rows == []  # NOTHING frozen — not even USDT — day left open for retry

    # Cycle 2: rate recovers. The same day must now be retried (last_finalized_day
    # never advanced past it) and freeze BOTH assets together at one shared rank.
    recovered_adapter = BybitStub(lines_by_day, rate=Decimal("0.6"))
    worker2 = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider({"bybit": recovered_adapter}),
        now=lambda: _now_at(date(2026, 7, 11)),
        settings=make_settings(mantle_rpc_url="https://rpc.mantle.xyz"),
    )
    await worker2.run_cycle()

    async with sessionmaker() as s:
        rows2 = {
            r.asset: r
            for r in (
                await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == acc_id))
            ).all()
        }

    assert set(rows2) == {"USDT", "MNT"}
    usdt_row, mnt_row = rows2["USDT"], rows2["MNT"]
    assert usdt_row.amount_usdt == Decimal("50")
    assert mnt_row.amount_usdt == Decimal("60")
    # Rank driven by the FULL day gross (50 + 60 = 110), shared by both rows —
    # not corrupted by a partial (USDT-only) gross from a prematurely finalized day.
    assert usdt_row.effective_rank == mnt_row.effective_rank


async def test_mnt_rate_outage_does_not_affect_non_bybit_user(sessionmaker):
    """Same cycle, same rate outage: a Gate (USDT-only, no MNT exposure) user must
    freeze normally — the skip-unfinalized-days protection only applies to users
    who actually have a Bybit (MNT-bearing) account."""
    bybit_acc_id = await _seed_user_account(sessionmaker, 704, date(2026, 7, 10), exchange="bybit")
    gate_acc_id = await _seed_user_account(sessionmaker, 705, date(2026, 7, 10), exchange="gate")
    bybit_adapter = RaisingMntAdapter(
        {
            date(2026, 7, 10): [
                CommissionLine(asset="USDT", amount=Decimal("50")),
                CommissionLine(asset="MNT", amount=Decimal("100")),
            ]
        }
    )
    gate_adapter = UsdtOnlyAdapter({date(2026, 7, 10): Decimal("40")})
    worker = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider({"bybit": bybit_adapter, "gate": gate_adapter}),
        now=lambda: _now_at(date(2026, 7, 11)),
        settings=make_settings(mantle_rpc_url="https://rpc.mantle.xyz"),
    )
    await worker.run_cycle()

    async with sessionmaker() as s:
        bybit_rows = (
            await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == bybit_acc_id))
        ).all()
        gate_rows = (
            await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == gate_acc_id))
        ).all()

    assert bybit_rows == []  # affected user: nothing frozen this cycle
    assert len(gate_rows) == 1  # unaffected user: freezes normally
    assert gate_rows[0].asset == "USDT"
    assert gate_rows[0].amount_usdt == Decimal("40")
