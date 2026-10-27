# tests/accrual/test_worker_edges.py
"""Edge-case coverage for AccrualWorker beyond Task 5's happy-path tests.

See .superpowers/sdd/task-6-brief.md.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from bot.accrual import repository as repo
from bot.accrual.adapters import MappingAdapterProvider
from bot.accrual.worker import AccrualWorker
from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.ledger import RebateAccrual
from bot.exchanges.base import CommissionResult, ExchangeAdapter
from bot.exchanges.gate import GateAdapter

pytestmark = pytest.mark.asyncio(loop_scope="session")


class FakeAdapter(ExchangeAdapter):
    """Returns scripted per-day USDT amounts keyed by date."""

    name = "Bybit"
    code = "bybit"

    def __init__(self, by_day: dict[date, Decimal]) -> None:
        self._by_day = by_day

    async def get_commission(self, uid, date_from, date_to):  # type: ignore[override]
        return CommissionResult(
            exchange="Bybit",
            uid=uid,
            date_from=date_from,
            date_to=date_to,
            total_usdt=self._by_day.get(date_from.date(), Decimal(0)),
        )


class FakeKucoin(FakeAdapter):
    """A KuCoin-like adapter with a settlement lag (defers freezing recent days)."""

    name = "KuCoin"
    code = "kucoin"
    settlement_lag_days = 2


class FakeGate(FakeAdapter):
    name = "Gate"
    code = "gate"
    settlement_lag_days = GateAdapter.settlement_lag_days


@pytest.fixture
def sm(pg_engine: AsyncEngine):
    return async_sessionmaker(pg_engine, expire_on_commit=False)


def _now_at(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 4, 0, tzinfo=UTC)


async def _seed(sm, user_id, since, exchange="bybit"):
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
        aid = acc.id
        await s.commit()
    return aid


async def test_kucoin_settlement_lag_defers_recent_days(sm):
    # now = Aug 20 -> last_closed = Aug 19. KuCoin lag 2 -> effective last_closed = Aug 17.
    # Days 15-17 freeze; days 18-19 are deferred (their commission may not have settled),
    # so no rows exist for them yet — they'll freeze on a later cycle once they age out.
    aid = await _seed(sm, 630, date(2026, 8, 15), exchange="kucoin")
    adapter = FakeKucoin(
        {
            d: Decimal("40")
            for d in (
                date(2026, 8, 15),
                date(2026, 8, 16),
                date(2026, 8, 17),
                date(2026, 8, 18),
                date(2026, 8, 19),
            )
        }
    )
    worker = AccrualWorker(
        sm,
        MappingAdapterProvider({"kucoin": adapter}),
        now=lambda: _now_at(date(2026, 8, 20)),
    )
    await worker.run_cycle()
    async with sm() as s:
        days = sorted(
            r.accrual_date
            for r in (
                await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == aid))
            ).all()
        )
    assert days == [date(2026, 8, 15), date(2026, 8, 16), date(2026, 8, 17)]


async def test_gate_waits_for_settlement_then_accrues_the_deferred_day(sm):
    aid = await _seed(sm, 981001, date(2026, 8, 15), exchange="gate")
    adapter = FakeGate({date(2026, 8, day): Decimal("40") for day in range(15, 20)})
    clock = [_now_at(date(2026, 8, 20))]
    worker = AccrualWorker(sm, MappingAdapterProvider({"gate": adapter}), now=lambda: clock[0])

    await worker.run_cycle()
    async with sm() as s:
        rows = (await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == aid))).all()
        frozen = {row.accrual_date: (row.id, row.fetched_at, row.net_usdt) for row in rows}
    assert sorted(frozen) == [date(2026, 8, day) for day in range(15, 19)]

    clock[0] = _now_at(date(2026, 8, 21))
    await worker.run_cycle()
    async with sm() as s:
        rows = (await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == aid))).all()
    assert sorted(row.accrual_date for row in rows) == [date(2026, 8, day) for day in range(15, 20)]
    assert all(
        (row.id, row.fetched_at, row.net_usdt) == frozen[row.accrual_date]
        for row in rows
        if row.accrual_date in frozen
    )


async def test_gate_cutover_resumes_after_legacy_utc_window_without_overlap(sm):
    aid = await _seed(sm, 981002, date(2026, 8, 17), exchange="gate")
    async with sm() as s:
        await repo.upsert_accrual(
            s,
            user_id=981002,
            account_id=aid,
            accrual_date=date(2026, 8, 17),
            amount_usdt=Decimal("40"),
            effective_rank=1,
            fee_pct=Decimal("0.10"),
            commission_usdt=Decimal("4"),
            net_usdt=Decimal("36"),
            state="paid",
            available_at=datetime(2026, 8, 18, tzinfo=UTC),
            expires_at=datetime(2026, 9, 17, tzinfo=UTC),
            raw_records_count=1,
            adapter_notes="legacy UTC query",
        )
        await s.commit()

    class RecordingGate(FakeGate):
        def __init__(self):
            super().__init__({date(2026, 8, day): Decimal("40") for day in (18, 19)})
            self.windows = []

        async def get_commission(self, uid, date_from, date_to):
            self.windows.append((date_from, date_to))
            return await super().get_commission(uid, date_from, date_to)

    adapter = RecordingGate()
    clock = [_now_at(date(2026, 8, 20))]
    worker = AccrualWorker(sm, MappingAdapterProvider({"gate": adapter}), now=lambda: clock[0])
    await worker.run_cycle()
    clock[0] = _now_at(date(2026, 8, 21))
    await worker.run_cycle()

    assert len(adapter.windows) == 2
    assert adapter.windows[0][0] == datetime(2026, 8, 18, tzinfo=UTC)
    assert adapter.windows[0][1].astimezone(UTC).replace(microsecond=0) == datetime(
        2026, 8, 18, 15, 59, 59, tzinfo=UTC
    )
    assert adapter.windows[1][0] == datetime(2026, 8, 18, 16, tzinfo=UTC)
    async with sm() as s:
        rows = (
            await s.scalars(
                select(RebateAccrual)
                .where(RebateAccrual.account_id == aid)
                .order_by(RebateAccrual.accrual_date)
            )
        ).all()
        latest_end = await repo.last_gate_query_end(s, aid)
    assert len(rows) == 3
    assert rows[0].state == "paid"
    assert rows[0].net_usdt == Decimal("36")
    assert rows[0].query_from is None and rows[0].query_to is None
    assert rows[1].query_from == datetime(2026, 8, 18, tzinfo=UTC)
    assert rows[2].query_from == rows[1].query_to + timedelta(seconds=1)
    assert latest_end == rows[2].query_to


async def test_no_lag_exchange_freezes_through_last_closed(sm):
    # Control: a lag-0 exchange (bybit) freezes every closed day, through Aug 19.
    aid = await _seed(sm, 631, date(2026, 8, 15), exchange="bybit")
    adapter = FakeAdapter(
        {
            d: Decimal("40")
            for d in (
                date(2026, 8, 15),
                date(2026, 8, 16),
                date(2026, 8, 17),
                date(2026, 8, 18),
                date(2026, 8, 19),
            )
        }
    )
    worker = AccrualWorker(
        sm,
        MappingAdapterProvider({"bybit": adapter}),
        now=lambda: _now_at(date(2026, 8, 20)),
    )
    await worker.run_cycle()
    async with sm() as s:
        days = sorted(
            r.accrual_date
            for r in (
                await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == aid))
            ).all()
        )
    assert days == [
        date(2026, 8, 15),
        date(2026, 8, 16),
        date(2026, 8, 17),
        date(2026, 8, 18),
        date(2026, 8, 19),
    ]


async def test_kucoin_lag_defers_whole_user_including_other_exchanges(sm):
    # A user with BOTH kucoin (lag 2) and bybit (lag 0): the lag applies to the WHOLE
    # user, so even the bybit account's recent days are deferred — never freeze a day
    # only partially (that would compute its rank without the still-pending kucoin data).
    async with sm() as s:
        s.add(User(id=632, role="user", status="active"))
        await s.flush()
        kc = ExchangeAccount(
            user_id=632,
            exchange="kucoin",
            uid="Ukc",
            status="active",
            accrual_since=date(2026, 8, 15),
        )
        by = ExchangeAccount(
            user_id=632,
            exchange="bybit",
            uid="Uby",
            status="active",
            accrual_since=date(2026, 8, 15),
        )
        s.add_all([kc, by])
        await s.flush()
        by_id = by.id
        await s.commit()
    amounts = {
        d: Decimal("40")
        for d in (
            date(2026, 8, 17),
            date(2026, 8, 18),
            date(2026, 8, 19),
        )
    }
    worker = AccrualWorker(
        sm,
        MappingAdapterProvider({"kucoin": FakeKucoin(amounts), "bybit": FakeAdapter(amounts)}),
        now=lambda: _now_at(date(2026, 8, 20)),
    )
    await worker.run_cycle()
    async with sm() as s:
        bybit_days = sorted(
            r.accrual_date
            for r in (
                await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == by_id))
            ).all()
        )
    # bybit would otherwise reach Aug 19, but is capped at Aug 17 by the user's kucoin lag.
    assert max(bybit_days) == date(2026, 8, 17)


async def test_backfill_born_expired_for_old_days(sm):
    # Account added today with epoch far in the past -> days > 30d old are born expired.
    aid = await _seed(sm, 600, date(2026, 7, 10))
    adapter = FakeAdapter({date(2026, 7, 10): Decimal("40"), date(2026, 7, 11): Decimal("40")})
    worker = AccrualWorker(
        sm,
        MappingAdapterProvider({"bybit": adapter}),
        now=lambda: _now_at(date(2026, 9, 1)),  # ~50 days later
    )
    await worker.run_cycle()
    async with sm() as s:
        rows = (await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == aid))).all()
        # Days whose 30-day life ended before `now` are born expired by AGE (the funded
        # July days included). Recent days (within 30 days) are born `available` — they
        # are not burned (there is no sub-$10 month burn anymore), just $0 rebate.
        old = [r for r in rows if r.accrual_date <= date(2026, 8, 2)]
        assert old and all(r.state == "expired" for r in old)


async def test_carry_over_lowers_fee_next_month(sm):
    # Month 1 hits rank 3 ($1500). Month 2 first day should already be rank 3 (carry floor).
    aid = await _seed(sm, 601, date(2026, 7, 10))
    adapter = FakeAdapter({date(2026, 7, 10): Decimal("1500"), date(2026, 8, 1): Decimal("20")})
    worker = AccrualWorker(
        sm,
        MappingAdapterProvider({"bybit": adapter}),
        now=lambda: _now_at(date(2026, 9, 1)),
    )
    await worker.run_cycle()
    async with sm() as s:
        aug1 = (
            await s.scalars(
                select(RebateAccrual).where(
                    RebateAccrual.account_id == aid, RebateAccrual.accrual_date == date(2026, 8, 1)
                )
            )
        ).one()
        assert aug1.effective_rank == 3  # carried from July
        assert aug1.fee_pct == Decimal("0.03")


async def test_two_uids_rank_by_combined_gross(sm):
    # Two accounts, same user, each $60/day one day -> combined $120 -> rank 2 next day.
    async with sm() as s:
        s.add(User(id=602, role="user", status="active"))
        await s.flush()
        a1 = ExchangeAccount(
            user_id=602,
            exchange="bybit",
            uid="X1",
            status="active",
            accrual_since=date(2026, 7, 10),
        )
        a2 = ExchangeAccount(
            user_id=602,
            exchange="okx",
            uid="X2",
            status="active",
            accrual_since=date(2026, 7, 10),
        )
        s.add_all([a1, a2])
        await s.flush()
        await s.commit()
    prov = MappingAdapterProvider(
        {
            "bybit": FakeAdapter(
                {date(2026, 7, 10): Decimal("60"), date(2026, 7, 11): Decimal("1")}
            ),
            "okx": FakeAdapter({date(2026, 7, 10): Decimal("60"), date(2026, 7, 11): Decimal("1")}),
        }
    )
    worker = AccrualWorker(sm, prov, now=lambda: _now_at(date(2026, 7, 20)))
    await worker.run_cycle()
    async with sm() as s:
        d11 = (
            await s.scalars(
                select(RebateAccrual).where(
                    RebateAccrual.user_id == 602, RebateAccrual.accrual_date == date(2026, 7, 11)
                )
            )
        ).all()
        # cum_before day11 = 120 (60+60) -> rank 2 for BOTH accounts' day-11 rows
        assert len(d11) == 2 and all(r.effective_rank == 2 for r in d11)


async def test_sub10_month_rows_stay_available_not_burned(sm):
    """A user's July closes at $5 (< $10), and a second account backfills $3 (combined
    $8, still < $10). Nothing is destroyed: all rows stay `available`. Dust is kept
    non-withdrawable by the withdrawal rank gate (rank 0), not by burning it here —
    and it expires on the normal 30-day schedule (PAYOUT_BUSINESS_LOGIC.md §4.4)."""
    user_id = 700

    def now() -> datetime:
        return _now_at(date(2026, 8, 5))  # August in progress -> July fully closed

    aid_a = await _seed(sm, user_id, date(2026, 7, 10), exchange="bybit")
    adapter_a = FakeAdapter({date(2026, 7, 10): Decimal("5")})
    worker1 = AccrualWorker(sm, MappingAdapterProvider({"bybit": adapter_a}), now=now)
    await worker1.run_cycle()

    async with sm() as s:
        july_rows_a = (
            await s.scalars(
                select(RebateAccrual).where(
                    RebateAccrual.account_id == aid_a,
                    RebateAccrual.accrual_date < date(2026, 8, 1),
                )
            )
        ).all()
        assert july_rows_a and all(r.state == "available" for r in july_rows_a)

    # A second exchange account is added for the SAME user with a July backfill.
    async with sm() as s:
        acc_b = ExchangeAccount(
            user_id=user_id,
            exchange="okx",
            uid=f"U{user_id}b",
            status="active",
            accrual_since=date(2026, 7, 12),
        )
        s.add(acc_b)
        await s.flush()
        aid_b = acc_b.id
        await s.commit()

    adapter_b = FakeAdapter({date(2026, 7, 12): Decimal("3")})
    worker2 = AccrualWorker(
        sm, MappingAdapterProvider({"bybit": adapter_a, "okx": adapter_b}), now=now
    )
    await worker2.run_cycle()

    async with sm() as s:
        july_rows_b = (
            await s.scalars(
                select(RebateAccrual).where(
                    RebateAccrual.account_id == aid_b,
                    RebateAccrual.accrual_date < date(2026, 8, 1),
                )
            )
        ).all()
        # Combined July gross = $5 + $3 = $8 (< $10): rows are NOT destroyed, they stay
        # available; the rank-0 withdrawal gate is what keeps this dust unpayable.
        assert july_rows_b
        assert all(r.state == "available" for r in july_rows_b)
