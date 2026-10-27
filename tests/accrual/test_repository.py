# tests/accrual/test_repository.py
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.accrual import repository as repo
from bot.db.models.identity import ExchangeAccount, User, Wallet
from bot.db.models.ledger import RebateAccrual
from bot.db.models.payouts import Payout

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _user_with_account(session: AsyncSession, uid_num: int) -> ExchangeAccount:
    session.add(User(id=uid_num, role="user", status="active"))
    await session.flush()
    acc = ExchangeAccount(
        user_id=uid_num,
        exchange="bybit",
        uid=f"U{uid_num}",
        status="active",
        accrual_since=date(2026, 7, 10),
    )
    session.add(acc)
    await session.flush()
    return acc


def _row_kwargs(user_id, account_id, d, amount, rank):
    fee = Decimal("0.10") if rank == 1 else Decimal("0.07")
    return {
        "user_id": user_id,
        "account_id": account_id,
        "accrual_date": d,
        "amount_usdt": amount,
        "effective_rank": rank,
        "fee_pct": fee,
        "commission_usdt": amount * fee,
        "net_usdt": amount - amount * fee,
        "state": "available",
        "available_at": datetime(2026, 7, 12, tzinfo=UTC),
        "expires_at": datetime(2026, 8, 10, tzinfo=UTC),
        "raw_records_count": 1,
        "adapter_notes": None,
    }


async def _settled_accrual(
    session: AsyncSession,
    user_id: int,
    account_id: int,
    accrual_date: date,
    amount: Decimal,
    state: str = "paid",
) -> RebateAccrual:
    """Build a Wallet + Payout + a RebateAccrual tied to that payout (settled row)."""
    wallet = Wallet(
        user_id=user_id,
        address_encrypted=b"x",
        address_hash=b"h",
        address_masked="0x..abcd",
    )
    session.add(wallet)
    await session.flush()
    payout = Payout(
        user_id=user_id,
        wallet_id=wallet.id,
        address_encrypted=b"x",
        address_masked="0x..abcd",
        covered_from=date(2026, 7, 1),
        covered_to=date(2026, 7, 31),
        gross_usdt=Decimal("10"),
        commission_usdt=Decimal("1"),
        net_usdt=Decimal("9"),
        idempotency_key=f"idem-{user_id}-{accrual_date.isoformat()}",
    )
    session.add(payout)
    await session.flush()
    kwargs = _row_kwargs(user_id, account_id, accrual_date, amount, 1)
    kwargs["state"] = state
    kwargs["payout_id"] = payout.id
    row = RebateAccrual(**kwargs)
    session.add(row)
    await session.flush()
    return row


async def test_upsert_is_idempotent_on_account_day(db_session):
    acc = await _user_with_account(db_session, 300)
    await repo.upsert_accrual(
        db_session, **_row_kwargs(300, acc.id, date(2026, 7, 11), Decimal("5"), 1)
    )
    await repo.upsert_accrual(
        db_session, **_row_kwargs(300, acc.id, date(2026, 7, 11), Decimal("8"), 2)
    )
    await db_session.flush()
    rows = (
        await db_session.scalars(select(RebateAccrual).where(RebateAccrual.account_id == acc.id))
    ).all()
    assert len(rows) == 1
    assert rows[0].amount_usdt == Decimal("8")  # second upsert overwrote
    assert rows[0].effective_rank == 2


async def test_user_month_gross_sums_regardless_of_state(db_session):
    acc = await _user_with_account(db_session, 301)
    await repo.upsert_accrual(
        db_session, **_row_kwargs(301, acc.id, date(2026, 7, 11), Decimal("3"), 1)
    )
    await repo.upsert_accrual(
        db_session, **_row_kwargs(301, acc.id, date(2026, 7, 12), Decimal("4"), 1)
    )
    # a row in the NEXT month must be excluded
    await repo.upsert_accrual(
        db_session, **_row_kwargs(301, acc.id, date(2026, 8, 1), Decimal("99"), 1)
    )
    await db_session.flush()
    g = await repo.user_month_gross(db_session, 301, date(2026, 7, 1), date(2026, 8, 1))
    assert g == Decimal("7")


async def test_write_or_bump_rank_history(db_session):
    db_session.add(User(id=303, role="user", status="active"))
    await db_session.flush()
    first = await repo.write_or_bump_rank_history(
        db_session,
        user_id=303,
        period_month=date(2026, 7, 1),
        achieved_rank=1,
        gross_month_usdt=Decimal("50"),
    )
    assert first is True
    again = await repo.write_or_bump_rank_history(
        db_session,
        user_id=303,
        period_month=date(2026, 7, 1),
        achieved_rank=3,
        gross_month_usdt=Decimal("1500"),
    )
    assert again is False
    assert await repo.get_achieved_rank(db_session, 303, date(2026, 7, 1)) == 3  # bumped up


async def test_expire_aged(db_session):
    acc = await _user_with_account(db_session, 304)
    kw = _row_kwargs(304, acc.id, date(2026, 7, 11), Decimal("3"), 1)
    kw["expires_at"] = datetime(2026, 8, 10, tzinfo=UTC)
    await repo.upsert_accrual(db_session, **kw)
    await db_session.flush()
    n = await repo.expire_aged(db_session, datetime(2026, 8, 11, tzinfo=UTC))
    await db_session.flush()
    assert n == 1


async def test_upsert_does_not_overwrite_row_tied_to_payout(db_session):
    acc = await _user_with_account(db_session, 310)
    d = date(2026, 7, 11)
    await _settled_accrual(db_session, 310, acc.id, d, Decimal("5"), state="paid")
    await db_session.flush()

    # Attempt to upsert the SAME (account_id, accrual_date) with different money.
    await repo.upsert_accrual(db_session, **_row_kwargs(310, acc.id, d, Decimal("999"), 3))
    await db_session.flush()

    row = (
        await db_session.scalars(select(RebateAccrual).where(RebateAccrual.account_id == acc.id))
    ).one()
    assert row.amount_usdt == Decimal("5")  # unchanged — payout_id guard held
    assert row.effective_rank == 1
    assert row.state == "paid"
    assert row.payout_id is not None


async def test_expire_aged_grants_downtime_grace(db_session):
    """A row whose life overlapped a downtime window is not expired until now passes
    expires_at + overlap; with no downtime it expires exactly on time."""
    acc = await _user_with_account(db_session, 331)
    kw = _row_kwargs(331, acc.id, date(2026, 7, 11), Decimal("3"), 1)
    kw["available_at"] = datetime(2026, 7, 12, tzinfo=UTC)
    kw["expires_at"] = datetime(2026, 8, 10, tzinfo=UTC)
    await repo.upsert_accrual(db_session, **kw)
    # 5 days of downtime inside the row's live window [07-12, 08-10]
    await repo.record_downtime(
        db_session,
        down_from=datetime(2026, 8, 5, tzinfo=UTC),
        down_to=datetime(2026, 8, 10, tzinfo=UTC),
        reason="missed_cycles",
    )
    await db_session.flush()

    # Just past nominal expiry, but grace defers it -> still available.
    n = await repo.expire_aged(db_session, datetime(2026, 8, 11, tzinfo=UTC))
    await db_session.flush()
    assert n == 0
    row = (await db_session.scalars(select(RebateAccrual))).one()
    assert row.state == "available"

    # Past expires_at + 5-day grace -> now it expires.
    n = await repo.expire_aged(db_session, datetime(2026, 8, 16, tzinfo=UTC))
    await db_session.flush()
    assert n == 1
    row = (await db_session.scalars(select(RebateAccrual))).one()
    assert row.state == "expired"


async def test_heartbeat_and_downtime_roundtrip(db_session):
    assert await repo.get_heartbeat(db_session) is None  # unset initially
    t1 = datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
    await repo.set_heartbeat(db_session, t1)
    await db_session.flush()
    assert await repo.get_heartbeat(db_session) == t1

    t2 = datetime(2026, 7, 15, 0, 0, tzinfo=UTC)
    await repo.set_heartbeat(db_session, t2)  # upsert overwrites
    await db_session.flush()
    assert await repo.get_heartbeat(db_session) == t2

    await repo.record_downtime(db_session, down_from=t1, down_to=t2, reason="missed_cycles")
    # an open window (down_to NULL) must be excluded from closed_downtime_windows
    from bot.db.models.ops import ServiceAvailability

    db_session.add(ServiceAvailability(down_from=t2, down_to=None, reason="open"))
    await db_session.flush()
    windows = await repo.closed_downtime_windows(db_session)
    assert windows == [(t1, t2)]


async def test_finalized_amounts_filters_by_range(db_session):
    acc = await _user_with_account(db_session, 320)
    await repo.upsert_accrual(
        db_session, **_row_kwargs(320, acc.id, date(2026, 7, 10), Decimal("5"), 1)
    )
    await repo.upsert_accrual(
        db_session, **_row_kwargs(320, acc.id, date(2026, 7, 11), Decimal("8"), 1)
    )
    # outside the queried [start, before) range -> must be excluded
    await repo.upsert_accrual(
        db_session, **_row_kwargs(320, acc.id, date(2026, 7, 12), Decimal("99"), 1)
    )
    await db_session.flush()

    amounts = await repo.finalized_amounts(db_session, acc.id, date(2026, 7, 10), date(2026, 7, 12))
    assert amounts == {
        date(2026, 7, 10): Decimal("5"),
        date(2026, 7, 11): Decimal("8"),
    }


async def test_finalized_amounts_ignores_unfinalized_rows(db_session):
    acc = await _user_with_account(db_session, 321)
    kw = _row_kwargs(321, acc.id, date(2026, 7, 10), Decimal("5"), 1)
    db_session.add(RebateAccrual(**kw))  # finalized left at its server default (false)
    await db_session.flush()

    amounts = await repo.finalized_amounts(db_session, acc.id, date(2026, 7, 1), date(2026, 8, 1))
    assert amounts == {}


async def test_expire_aged_ignores_accruing_rows(db_session):
    acc = await _user_with_account(db_session, 312)

    kw_available = _row_kwargs(312, acc.id, date(2026, 7, 11), Decimal("3"), 1)
    kw_available["expires_at"] = datetime(2026, 8, 10, tzinfo=UTC)
    await repo.upsert_accrual(db_session, **kw_available)

    kw_accruing = _row_kwargs(312, acc.id, date(2026, 7, 12), Decimal("4"), 1)
    kw_accruing["state"] = "accruing"
    kw_accruing["expires_at"] = datetime(2026, 8, 10, tzinfo=UTC)
    db_session.add(RebateAccrual(**kw_accruing))
    await db_session.flush()

    n = await repo.expire_aged(db_session, datetime(2026, 8, 11, tzinfo=UTC))
    await db_session.flush()
    assert n == 1

    rows = {
        r.accrual_date: r
        for r in (
            await db_session.scalars(
                select(RebateAccrual).where(RebateAccrual.account_id == acc.id)
            )
        ).all()
    }
    assert rows[date(2026, 7, 11)].state == "expired"
    assert rows[date(2026, 7, 12)].state == "accruing"  # never touched by age-expiry
