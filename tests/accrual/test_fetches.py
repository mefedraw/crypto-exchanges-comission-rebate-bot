import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from bot.accrual import fetches
from bot.accrual.adapters import MappingAdapterProvider
from bot.accrual.worker import AccrualWorker
from bot.db.models.fetches import CommissionFetch
from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.ledger import RankHistory, RebateAccrual
from bot.exchanges.base import CommissionLine, CommissionResult, ExchangeAdapter, ExchangeApiError
from bot.payout.expiry import PAYOUT_TZ
from tests.conftest import make_settings

pytestmark = pytest.mark.asyncio(loop_scope="session")
DAY = date(2026, 7, 10)
LEFT = datetime(2026, 7, 10, tzinfo=PAYOUT_TZ)
RIGHT = LEFT + timedelta(days=1, microseconds=-1)


@pytest_asyncio.fixture(autouse=True)
async def clear_own_committed_rows(pg_engine):
    yield
    # Repository tests in this package intentionally inspect the whole ledger.
    ids = list(range(98001, 98011))
    async with pg_engine.begin() as conn:
        accounts = select(ExchangeAccount.id).where(ExchangeAccount.user_id.in_(ids))
        await conn.execute(delete(CommissionFetch).where(CommissionFetch.account_id.in_(accounts)))
        await conn.execute(delete(RebateAccrual).where(RebateAccrual.user_id.in_(ids)))
        await conn.execute(delete(RankHistory).where(RankHistory.user_id.in_(ids)))
        await conn.execute(delete(ExchangeAccount).where(ExchangeAccount.user_id.in_(ids)))
        await conn.execute(delete(User).where(User.id.in_(ids)))


class Adapter(ExchangeAdapter):
    name = "Test"
    code = "test"

    def __init__(self, *, fail=False, amount=Decimal("60"), lines=None):
        self.fail = fail
        self.amount = amount
        self.lines = lines
        self.calls = []

    async def get_commission(self, uid, date_from, date_to):
        self.calls.append((uid, date_from, date_to))
        if self.fail:
            raise ExchangeApiError("unavailable")
        return CommissionResult(
            exchange=self.name,
            uid=uid,
            date_from=date_from,
            date_to=date_to,
            total_usdt=self.amount if self.lines is None else None,
            lines=self.lines or [],
            raw_records_count=3,
            notes=["accrued"],
            settlement_note="not guaranteed",
        )


async def seed(sm, user_id, codes):
    async with sm() as s:
        s.add(User(id=user_id, role="user", status="active"))
        await s.flush()
        ids = []
        for code in codes:
            account = ExchangeAccount(
                user_id=user_id,
                exchange=code,
                uid=f"{user_id}-{code}",
                status="active",
                accrual_since=DAY,
            )
            s.add(account)
            await s.flush()
            ids.append(account.id)
        await s.commit()
    return ids


async def call(sm, adapter, account_id, uid, left=LEFT, right=RIGHT, now=None):
    return await fetches.get_or_fetch(
        sm,
        adapter,
        account_id=account_id,
        uid=uid,
        day=DAY,
        query_from=left,
        query_to=right,
        now=now,
    )


@pytest.mark.parametrize("failing_first", [False, True])
async def test_failure_keeps_other_successes_across_restart_and_atomic_ledger(
    pg_engine, failing_first
):
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    user_id = 98001 + int(failing_first)
    codes = ["bad", "good"] if failing_first else ["good", "bad"]
    ids = dict(zip(codes, await seed(sm, user_id, codes), strict=True))
    good, bad = Adapter(), Adapter(fail=True)
    provider = MappingAdapterProvider({"good": good, "bad": bad})

    def worker():
        return AccrualWorker(
            sm, provider, now=lambda: datetime(2026, 7, 12, 4, tzinfo=UTC), settings=make_settings()
        )

    await worker().run_cycle()
    async with sm() as s:
        assert (
            await s.scalar(
                select(func.count())
                .select_from(RebateAccrual)
                .where(RebateAccrual.user_id == user_id)
            )
            == 0
        )
        cached = list(
            (
                await s.scalars(
                    select(CommissionFetch).where(CommissionFetch.account_id.in_(ids.values()))
                )
            ).all()
        )
        assert len(cached) == 2  # both closed days of the good account
        assert all(row.account_id == ids["good"] for row in cached)
    await worker().run_cycle()  # a fresh worker reuses durable successes
    assert len(good.calls) == 2
    assert len(bad.calls) == 2  # one failed request/account/cycle, no cached zero
    bad.fail = False
    await worker().run_cycle()
    assert len(good.calls) == 2
    async with sm() as s:
        rows = list(
            (await s.scalars(select(RebateAccrual).where(RebateAccrual.user_id == user_id))).all()
        )
        assert len(rows) == 4
        assert all(row.effective_rank == (1 if row.accrual_date == DAY else 2) for row in rows)
        assert sum(row.net_usdt for row in rows) == Decimal("219.6")
    await worker().run_cycle()
    assert len(good.calls) == 2 and len(bad.calls) == 4


async def test_zero_is_a_success_not_a_missing_snapshot(pg_engine):
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    (account_id,) = await seed(sm, 98003, ["test"])
    adapter = Adapter(amount=Decimal(0))
    first = await call(sm, adapter, account_id, "98003-test")
    adapter.fail = True
    second = await call(sm, adapter, account_id, "98003-test")
    assert first.total_usdt == second.total_usdt == Decimal(0)
    assert len(adapter.calls) == 1


async def test_exact_assets_precision_and_window_are_preserved(pg_engine):
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    (account_id,) = await seed(sm, 98004, ["test"])
    lines = [
        CommissionLine("USDT", Decimal("1.000000000000000001"), "SPOT"),
        CommissionLine("USDT", Decimal("2.9"), "FUTURES"),
        CommissionLine("MNT", Decimal("0.000000000000000001")),
        CommissionLine("BTC", Decimal("0.00000001")),
        CommissionLine("ETH", Decimal("0.00000002")),
    ]
    adapter = Adapter(lines=lines)
    first = await call(sm, adapter, account_id, "98004-test")
    second = await call(sm, adapter, account_id, "98004-test")
    assert first == second
    assert second.lines == lines and second.total_usdt is None
    assert second.notes == ["accrued"] and second.raw_records_count == 3
    assert len(adapter.calls) == 1
    await call(sm, adapter, account_id, "98004-test", left=LEFT + timedelta(hours=8))
    assert len(adapter.calls) == 2  # legacy Gate cutover cannot reuse a full-day answer


async def test_cache_commit_survives_outer_rollback_and_rejects_corruption(pg_engine):
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    (account_id,) = await seed(sm, 98005, ["test"])
    adapter = Adapter()
    async with sm() as outer:
        await outer.execute(select(User).where(User.id == 98005))
        await call(sm, adapter, account_id, "98005-test")
        await outer.rollback()
    await call(sm, adapter, account_id, "98005-test")
    assert len(adapter.calls) == 1
    async with sm() as s:
        await s.execute(
            update(CommissionFetch)
            .where(CommissionFetch.account_id == account_id)
            .values(payload={})
        )
        await s.commit()
    with pytest.raises(ValueError):
        await call(sm, adapter, account_id, "98005-test")
    assert len(adapter.calls) == 1  # malformed data must not become a zero response


async def test_format_version_forces_refetch(pg_engine, monkeypatch):
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    (account_id,) = await seed(sm, 98006, ["test"])
    adapter = Adapter()
    await call(sm, adapter, account_id, "98006-test")
    monkeypatch.setattr(fetches, "FORMAT_VERSION", 2)
    await call(sm, adapter, account_id, "98006-test")
    assert len(adapter.calls) == 2


async def test_expired_snapshot_needs_successful_refresh_no_stale_fallback(pg_engine):
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    (account_id,) = await seed(sm, 98010, ["test"])
    adapter = Adapter()
    clock = datetime(2026, 10, 31, tzinfo=UTC)
    await call(sm, adapter, account_id, "98010-test", now=clock)
    adapter.fail = True
    fresh = await call(sm, adapter, account_id, "98010-test", now=clock + timedelta(hours=23))
    assert fresh.total_usdt == Decimal(60) and len(adapter.calls) == 1
    with pytest.raises(ExchangeApiError):
        await call(sm, adapter, account_id, "98010-test", now=clock + timedelta(hours=24))
    async with sm() as s:
        row = (
            await s.scalars(select(CommissionFetch).where(CommissionFetch.account_id == account_id))
        ).one()
        assert row.fetched_at == clock
    adapter.fail = False
    adapter.amount = Decimal(80)
    refreshed = await call(sm, adapter, account_id, "98010-test", now=clock + timedelta(hours=25))
    assert refreshed.total_usdt == Decimal(80) and len(adapter.calls) == 3


async def test_concurrent_fetches_return_the_same_persisted_winner(pg_engine):
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    (account_id,) = await seed(sm, 98007, ["test"])
    barrier = asyncio.Barrier(2)

    class ConcurrentAdapter(Adapter):
        async def get_commission(self, uid, date_from, date_to):
            result = await super().get_commission(uid, date_from, date_to)
            result.total_usdt = Decimal(len(self.calls))
            await barrier.wait()
            return result

    adapter = ConcurrentAdapter()
    first, second = await asyncio.gather(
        call(sm, adapter, account_id, "98007-test"),
        call(sm, adapter, account_id, "98007-test"),
    )
    assert first.total_usdt == second.total_usdt
    assert len(adapter.calls) == 2
    async with sm() as s:
        assert (
            await s.scalar(
                select(func.count())
                .select_from(CommissionFetch)
                .where(CommissionFetch.account_id == account_id)
            )
            == 1
        )


async def test_native_mnt_cache_does_not_freeze_fx_rate(pg_engine):
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    await seed(sm, 98008, ["bybit", "bad"])

    class MntAdapter(Adapter):
        code = "bybit"
        rate = Decimal(2)
        rate_calls = 0

        async def mnt_usdt_rate(self):
            self.rate_calls += 1
            return self.rate

    bybit = MntAdapter(lines=[CommissionLine("MNT", Decimal(1))])
    bad = Adapter(fail=True)
    provider = MappingAdapterProvider({"bybit": bybit, "bad": bad})
    settings = make_settings(MANTLE_RPC_URL="https://example.test")

    def worker():
        return AccrualWorker(
            sm, provider, now=lambda: datetime(2026, 7, 11, 4, tzinfo=UTC), settings=settings
        )

    await worker().run_cycle()
    bybit.rate = Decimal(3)
    bad.fail = False
    await worker().run_cycle()
    assert len(bybit.calls) == 1 and bybit.rate_calls == 2
    async with sm() as s:
        row = (
            await s.scalars(
                select(RebateAccrual).where(
                    RebateAccrual.user_id == 98008, RebateAccrual.asset == "MNT"
                )
            )
        ).one()
        assert row.amount_native == Decimal(1)
        assert row.fx_rate == Decimal(3) and row.amount_usdt == Decimal(3)


async def test_failure_after_an_earlier_success_retries_only_missing_day(pg_engine):
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    await seed(sm, 98009, ["test"])

    class LateFailure(Adapter):
        failing_day = DAY + timedelta(days=1)

        async def get_commission(self, uid, date_from, date_to):
            self.fail = date_from.date() == self.failing_day
            return await super().get_commission(uid, date_from, date_to)

    adapter = LateFailure()
    provider = MappingAdapterProvider({"test": adapter})

    def worker():
        return AccrualWorker(
            sm, provider, now=lambda: datetime(2026, 7, 12, 4, tzinfo=UTC), settings=make_settings()
        )

    await worker().run_cycle()
    assert len(adapter.calls) == 2
    adapter.failing_day = None
    await worker().run_cycle()
    assert len(adapter.calls) == 3
    async with sm() as s:
        assert (
            await s.scalar(
                select(func.count())
                .select_from(RebateAccrual)
                .where(RebateAccrual.user_id == 98009)
            )
            == 2
        )
