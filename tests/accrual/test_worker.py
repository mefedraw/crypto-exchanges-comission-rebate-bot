# tests/accrual/test_worker.py
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.accrual import repository as acc_repo
from bot.accrual.adapters import MappingAdapterProvider
from bot.accrual.worker import AccrualWorker
from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.ledger import RankHistory, RebateAccrual
from bot.db.models.ops import Notification
from bot.exchanges.base import CommissionResult, ExchangeAdapter
from bot.notifications import repository as notif_repo

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


class RaisingAdapter(ExchangeAdapter):
    """Always raises — proves a failing user doesn't block other users."""

    name = "Raising"
    code = "raising"

    async def get_commission(self, uid, date_from, date_to):  # type: ignore[override]
        raise RuntimeError("adapter boom")


@pytest.fixture
def sessionmaker(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)


async def _seed_user_account(sm, user_id: int, since: date, exchange: str = "bybit") -> int:
    async with sm() as s:
        s.add(User(id=user_id, role="user", status="active"))
        await s.flush()
        acc_id = await _add_account(s, user_id, exchange, since)
        await s.commit()
    return acc_id


async def _seed_account(sm, user_id: int, exchange: str, since: date) -> int:
    """Add another account to an ALREADY-existing user."""
    async with sm() as s:
        acc_id = await _add_account(s, user_id, exchange, since)
        await s.commit()
    return acc_id


async def _add_account(s, user_id: int, exchange: str, since: date) -> int:
    acc = ExchangeAccount(
        user_id=user_id,
        exchange=exchange,
        uid=f"U{user_id}",
        status="active",
        accrual_since=since,
    )
    s.add(acc)
    await s.flush()
    return acc.id


def _now_at(d: date) -> datetime:
    # A UTC instant that is 'd' 12:00 in UTC+8 -> today in PAYOUT_TZ is d.
    return datetime(d.year, d.month, d.day, 4, 0, tzinfo=UTC)


async def test_sub_10_month_is_not_burned_but_rank0(sessionmaker):
    acc_id = await _seed_user_account(sessionmaker, 400, date(2026, 7, 10))
    # July total = 2+3 = $5 (< $10). The month is achieved-rank 0 (not withdrawable
    # via the rank gate), but its rows are NOT destroyed — they stay `available` and
    # expire on the normal 30-day schedule.
    adapter = FakeAdapter({date(2026, 7, 10): Decimal("2"), date(2026, 7, 11): Decimal("3")})
    worker = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider({"bybit": adapter}),
        now=lambda: _now_at(date(2026, 8, 5)),  # August in progress -> July fully closed
    )
    await worker.run_cycle()
    async with sessionmaker() as s:
        rows = (
            await s.scalars(
                select(RebateAccrual)
                .where(RebateAccrual.account_id == acc_id)
                .order_by(RebateAccrual.accrual_date)
            )
        ).all()
        assert [r.amount_usdt for r in rows[:2]] == [Decimal("2"), Decimal("3")]
        assert all(r.effective_rank == 1 for r in rows[:2])  # base floor rank 1 (for fee)
        assert all(r.state == "available" for r in rows[:2])  # NOT burned
        rh = (await s.scalars(select(RankHistory).where(RankHistory.user_id == 400))).one()
        assert rh.achieved_rank == 0 and rh.gross_month_usdt == Decimal("5")  # month rank 0


async def test_rank_upgrade_next_day_and_frozen_fee(sessionmaker):
    acc_id = await _seed_user_account(sessionmaker, 401, date(2026, 7, 10))
    # Day10 $95 (cum 95, R1), Day11 $50 (cum_before 95 -> still R1), Day12 (cum_before 145 -> R2).
    adapter = FakeAdapter(
        {
            date(2026, 7, 10): Decimal("95"),
            date(2026, 7, 11): Decimal("50"),
            date(2026, 7, 12): Decimal("20"),
        }
    )
    worker = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider({"bybit": adapter}),
        now=lambda: _now_at(date(2026, 8, 1)),
    )
    await worker.run_cycle()
    async with sessionmaker() as s:
        rows = {
            r.accrual_date: r
            for r in (
                await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == acc_id))
            ).all()
        }
        assert rows[date(2026, 7, 10)].effective_rank == 1
        assert rows[date(2026, 7, 11)].effective_rank == 1  # upgrade not yet
        assert rows[date(2026, 7, 12)].effective_rank == 2  # next day after crossing 100
        # frozen fee: day 11 at 10%, day 12 at 7%
        assert rows[date(2026, 7, 11)].commission_usdt == Decimal("5.0")
        assert rows[date(2026, 7, 12)].commission_usdt == Decimal("1.40")
        # month >= $10 -> NOT burned
        assert rows[date(2026, 7, 12)].state == "available"


async def test_run_cycle_is_idempotent(sessionmaker):
    acc_id = await _seed_user_account(sessionmaker, 402, date(2026, 7, 10))
    adapter = FakeAdapter({date(2026, 7, 10): Decimal("40")})
    worker = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider({"bybit": adapter}),
        now=lambda: _now_at(date(2026, 7, 15)),
    )
    await worker.run_cycle()
    async with sessionmaker() as s:
        rows_first = (
            await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == acc_id))
        ).all()
        count_first = len(rows_first)
    await worker.run_cycle()  # second run must not duplicate or change
    async with sessionmaker() as s:
        rows = (
            await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == acc_id))
        ).all()
        assert len(rows) == 5  # July 10..14 inclusive = 5 days
        assert len(rows) == count_first  # second run is a no-op on row count


async def test_does_not_process_today(sessionmaker):
    acc_id = await _seed_user_account(sessionmaker, 403, date(2026, 7, 10))
    adapter = FakeAdapter({date(2026, 7, 10): Decimal("40"), date(2026, 7, 11): Decimal("40")})
    worker = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider({"bybit": adapter}),
        now=lambda: _now_at(date(2026, 7, 11)),  # today=Jul11 -> only Jul10 closed
    )
    await worker.run_cycle()
    async with sessionmaker() as s:
        dates = sorted(
            r.accrual_date
            for r in (
                await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == acc_id))
            ).all()
        )
        assert dates == [date(2026, 7, 10)]  # Jul 11 (today) not processed


async def test_one_user_failure_does_not_block_others(sessionmaker):
    acc_a = await _seed_user_account(sessionmaker, 500, date(2026, 7, 10), exchange="bybit")
    acc_b = await _seed_user_account(sessionmaker, 501, date(2026, 7, 10), exchange="okx")
    good_adapter = FakeAdapter({date(2026, 7, 10): Decimal("40")})
    worker = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider({"bybit": RaisingAdapter(), "okx": good_adapter}),
        now=lambda: _now_at(date(2026, 7, 15)),
    )
    await worker.run_cycle()  # user 500's adapter blows up here; see assertions below

    async with sessionmaker() as s:
        rows_a = (
            await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == acc_a))
        ).all()
        rows_b = (
            await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == acc_b))
        ).all()
    # this is the actual proof run_cycle didn't propagate the failure: user A's
    # failed transaction rolled back entirely (no partial rows), while user B's
    # rows were still committed.
    assert rows_a == []
    assert len(rows_b) == 5  # July 10..14 inclusive — user B unaffected
    by_day = {r.accrual_date: r.amount_usdt for r in rows_b}
    assert by_day[date(2026, 7, 10)] == Decimal("40")


async def test_user_failure_enqueues_dev_alert(sessionmaker):
    """A per-user accrual failure must reach the developer via the outbox (#1)."""
    await _seed_user_account(sessionmaker, 510, date(2026, 7, 10), exchange="bybit")
    worker = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider({"bybit": RaisingAdapter()}),
        now=lambda: _now_at(date(2026, 7, 15)),
        developer_id=999999,
    )
    await worker.run_cycle()

    async with sessionmaker() as s:
        alerts = (
            await s.scalars(select(Notification).where(Notification.kind == notif_repo.DEV_ALERT))
        ).all()
    # run_cycle processes every committed user in the shared DB, so isolate ours by
    # the failing user id in the detail (this test owns user 510).
    mine = [a for a in alerts if "510" in (a.payload or {}).get("detail", "")]
    assert len(mine) == 1
    assert mine[0].recipient_chat_id == 999999


async def test_missed_cycle_gap_records_downtime(sessionmaker):
    """A heartbeat gap beyond the threshold is recorded as a downtime window; a
    small gap is not."""
    t0 = datetime(2026, 7, 10, 4, tzinfo=UTC)
    async with sessionmaker() as s:
        await acc_repo.set_heartbeat(s, t0)
        await s.commit()

    # 4-day gap (> 2-day threshold) -> downtime recorded
    worker = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider({}),
        now=lambda: datetime(2026, 7, 14, 4, tzinfo=UTC),
    )
    await worker.run_cycle()
    async with sessionmaker() as s:
        windows = await acc_repo.closed_downtime_windows(s)
        assert windows == [(t0, datetime(2026, 7, 14, 4, tzinfo=UTC))]
        assert await acc_repo.get_heartbeat(s) == datetime(2026, 7, 14, 4, tzinfo=UTC)

    # next cycle only 1 day later (< threshold) -> no new window
    worker2 = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider({}),
        now=lambda: datetime(2026, 7, 15, 4, tzinfo=UTC),
    )
    await worker2.run_cycle()
    async with sessionmaker() as s:
        assert len(await acc_repo.closed_downtime_windows(s)) == 1  # unchanged


async def test_late_second_uid_into_sub10_month_not_destroyed(sessionmaker):
    """UID_A alone makes July sub-$10; a later UID_B backfills the same month.
    Nothing is burned — all rows stay 'available' (the rank gate, not destruction,
    keeps dust non-withdrawable). Also proves finalized UID_A days aren't re-polled."""
    acc_a = await _seed_user_account(sessionmaker, 520, date(2026, 7, 10), exchange="bybit")
    worker1 = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider(
            {
                "bybit": FakeAdapter(
                    {date(2026, 7, 10): Decimal("2"), date(2026, 7, 11): Decimal("3")}
                )
            }
        ),
        now=lambda: _now_at(date(2026, 8, 1)),  # July fully closed; rows expire Aug 9/10 (young)
    )
    await worker1.run_cycle()
    async with sessionmaker() as s:
        states = [
            r.state
            for r in (
                await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == acc_a))
            ).all()
        ]
        assert states and all(st == "available" for st in states)  # NOT burned

    # Add UID_B backfilling the same month with $8 -> July gross 5 + 8 = $13 (>= $10).
    acc_b = await _seed_account(sessionmaker, 520, "mexc", date(2026, 7, 10))
    worker2 = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider(
            {
                "bybit": FakeAdapter({}),  # UID_A days already finalized -> never re-polled
                "mexc": FakeAdapter({date(2026, 7, 10): Decimal("8")}),
            }
        ),
        now=lambda: _now_at(date(2026, 8, 1)),
    )
    await worker2.run_cycle()
    async with sessionmaker() as s:
        rows_a = {
            r.accrual_date: r.state
            for r in (
                await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == acc_a))
            ).all()
        }
        rows_b = (
            await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == acc_b))
        ).all()
        rh = (await s.scalars(select(RankHistory).where(RankHistory.user_id == 520))).one()
    assert rows_a[date(2026, 7, 10)] == "available"  # stayed available throughout
    assert rows_a[date(2026, 7, 11)] == "available"
    assert all(r.state == "available" for r in rows_b)  # UID_B born alive
    assert rh.gross_month_usdt == Decimal("13")  # month now >= $10


async def test_finalized_rows_not_repolled_or_overwritten(sessionmaker):
    user_id = 502
    # Ahead account: accrual_since == epoch, gets fully finalized through last_closed
    # in cycle 1.
    ahead_acc = await _seed_user_account(sessionmaker, user_id, date(2026, 7, 10), exchange="gate")
    ahead_amounts = {date(2026, 7, d): Decimal("5") for d in range(10, 32)}
    worker1 = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider({"gate": FakeAdapter(ahead_amounts)}),
        now=lambda: _now_at(date(2026, 8, 1)),  # last_closed = July 31
    )
    await worker1.run_cycle()

    async with sessionmaker() as s:
        frozen_before = {
            r.accrual_date: (r.amount_usdt, r.effective_rank)
            for r in (
                await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == ahead_acc))
            ).all()
        }
    assert len(frozen_before) == 22  # July 10..31 inclusive

    # Now add a LAGGING sibling account whose accrual_since falls inside the range
    # the ahead account already froze, so cycle 2's `start` (min across accounts)
    # lands on days the ahead account already finalized.
    lagging_acc = await _seed_account(sessionmaker, user_id, "mexc", date(2026, 7, 20))
    lagging_amounts = {date(2026, 7, d): Decimal("3") for d in range(20, 32)}

    class RaisingIfPolled(ExchangeAdapter):
        """Would prove a re-poll happened — must never be called."""

        name = "Gate"
        code = "gate"

        async def get_commission(self, uid, date_from, date_to):  # type: ignore[override]
            raise AssertionError(
                f"re-polled already-finalized day {date_from.date()} for account {uid}"
            )

    worker2 = AccrualWorker(
        sessionmaker,
        MappingAdapterProvider({"gate": RaisingIfPolled(), "mexc": FakeAdapter(lagging_amounts)}),
        now=lambda: _now_at(date(2026, 8, 1)),
    )
    # The real proof is below (frozen_after == frozen_before): if the ahead
    # account's already-finalized days had been re-polled instead of reused,
    # RaisingIfPolled would have raised and this call would have propagated it.
    await worker2.run_cycle()

    async with sessionmaker() as s:
        frozen_after = {
            r.accrual_date: (r.amount_usdt, r.effective_rank)
            for r in (
                await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == ahead_acc))
            ).all()
        }
        lagging_rows = (
            await s.scalars(select(RebateAccrual).where(RebateAccrual.account_id == lagging_acc))
        ).all()

    assert frozen_after == frozen_before  # ahead account's frozen rows untouched
    assert len(lagging_rows) == 12  # July 20..31 inclusive, newly polled
