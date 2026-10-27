# tests/executor/test_repository.py
from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models.identity import ExchangeAccount, User, Wallet
from bot.db.models.ledger import RebateAccrual
from bot.db.models.payouts import Payout
from bot.executor import repository as repo

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _user_with_wallet(
    session: AsyncSession, user_id: int, uid: str = "U"
) -> tuple[ExchangeAccount, Wallet]:
    session.add(User(id=user_id, role="user", status="active"))
    await session.flush()
    wallet = Wallet(
        user_id=user_id,
        address_encrypted=b"enc",
        address_hash=f"hash{user_id}".encode(),
        address_masked="0x1234…abcd",
    )
    session.add(wallet)
    acc = ExchangeAccount(
        user_id=user_id,
        exchange="bybit",
        uid=f"{uid}{user_id}",
        status="active",
        accrual_since=date(2026, 7, 1),
    )
    session.add(acc)
    await session.flush()
    return acc, wallet


async def _payout(
    session: AsyncSession,
    user_id: int,
    wallet_id: int,
    status: str,
    *,
    nonce: int | None = None,
    requested_at: datetime | None = None,
    asset: str = "USDT",
) -> Payout:
    payout = Payout(
        user_id=user_id,
        wallet_id=wallet_id,
        address_encrypted=b"x",
        address_masked="0x..abcd",
        covered_from=date(2026, 7, 1),
        covered_to=date(2026, 7, 31),
        gross_usdt=Decimal("10"),
        commission_usdt=Decimal("1"),
        net_usdt=Decimal("9"),
        status=status,
        nonce=nonce,
        asset=asset,
        idempotency_key=f"idem-{user_id}-{status}-{nonce}-{requested_at}",
    )
    if requested_at is not None:
        payout.requested_at = requested_at
    session.add(payout)
    await session.flush()
    return payout


def _accrual(
    user_id: int,
    account_id: int,
    d: date,
    amount: Decimal,
    state: str,
    payout_id: int | None,
) -> RebateAccrual:
    fee = Decimal("0.10")
    commission = amount * fee
    return RebateAccrual(
        user_id=user_id,
        account_id=account_id,
        accrual_date=d,
        amount_usdt=amount,
        effective_rank=1,
        fee_pct=fee,
        commission_usdt=commission,
        net_usdt=amount - commission,
        state=state,
        payout_id=payout_id,
        available_at=datetime(2026, 7, 12, tzinfo=UTC),
        expires_at=datetime(2026, 8, 10, tzinfo=UTC),
        raw_records_count=1,
        adapter_notes=None,
    )


async def test_finalize_paid_flips_payout_and_accruals(db_session: AsyncSession):
    session = db_session
    acc, wallet = await _user_with_wallet(session, 701)
    payout = await _payout(session, 701, wallet.id, "sent", nonce=1)
    payout_id = payout.id
    session.add(_accrual(701, acc.id, date(2026, 7, 5), Decimal("30"), "reserved", payout_id))
    session.add(_accrual(701, acc.id, date(2026, 7, 6), Decimal("30"), "reserved", payout_id))
    await session.flush()

    now = datetime(2026, 7, 20, tzinfo=UTC)
    await repo.finalize_paid(session, payout_id, now)
    await session.flush()
    session.expire_all()

    refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
    assert refreshed is not None
    assert refreshed.status == "paid"
    assert refreshed.confirmed_at == now
    assert refreshed.paid_at == now

    accruals = (
        await session.scalars(select(RebateAccrual).where(RebateAccrual.user_id == 701))
    ).all()
    assert len(accruals) == 2
    for a in accruals:
        assert a.state == "paid"
        assert a.payout_id == payout_id


async def test_finalize_failed_returns_accruals_to_available(db_session: AsyncSession):
    session = db_session
    acc, wallet = await _user_with_wallet(session, 702)
    payout = await _payout(session, 702, wallet.id, "submitting", nonce=1)
    payout_id = payout.id
    session.add(_accrual(702, acc.id, date(2026, 7, 5), Decimal("30"), "reserved", payout_id))
    await session.flush()

    await repo.finalize_failed(session, payout_id, "broadcast_uncertain")
    await session.flush()
    session.expire_all()

    refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.failed_reason == "broadcast_uncertain"

    accruals = (
        await session.scalars(select(RebateAccrual).where(RebateAccrual.user_id == 702))
    ).all()
    assert len(accruals) == 1
    assert accruals[0].state == "available"
    assert accruals[0].payout_id is None


async def test_claim_next_reserved_returns_oldest(db_session: AsyncSession):
    session = db_session
    _acc, wallet = await _user_with_wallet(session, 703)
    older = await _payout(
        session, 703, wallet.id, "reserved", requested_at=datetime(2026, 7, 1, tzinfo=UTC)
    )
    await _payout(
        session, 703, wallet.id, "reserved", requested_at=datetime(2026, 7, 10, tzinfo=UTC)
    )

    claimed = await repo.claim_next_reserved(session)
    assert claimed is not None
    assert claimed.id == older.id
    assert claimed.status == "reserved"


async def test_last_nonce_returns_max(db_session: AsyncSession):
    session = db_session
    _acc, wallet = await _user_with_wallet(session, 704)
    await _payout(session, 704, wallet.id, "sent", nonce=3)
    await _payout(session, 704, wallet.id, "paid", nonce=7)

    result = await repo.last_nonce(session, "USDT")
    assert result == 7


async def test_last_nonce_is_scoped_per_asset(db_session: AsyncSession):
    """BSC (USDT) and Mantle (MNT) are separate chains with separate nonce sequences —
    a high USDT nonce must never leak into the MNT max, and vice versa (Task 9)."""
    session = db_session
    _acc, wallet = await _user_with_wallet(session, 708)
    await _payout(session, 708, wallet.id, "sent", nonce=50, asset="USDT")
    await _payout(session, 708, wallet.id, "paid", nonce=2, asset="MNT")

    assert await repo.last_nonce(session, "USDT") == 50
    assert await repo.last_nonce(session, "MNT") == 2


async def test_last_nonce_none_when_asset_has_no_rows(db_session: AsyncSession):
    session = db_session
    _acc, wallet = await _user_with_wallet(session, 709)
    await _payout(session, 709, wallet.id, "sent", nonce=1, asset="USDT")

    assert await repo.last_nonce(session, "MNT") is None


async def test_payouts_in_flight_returns_only_submitting_and_sent(db_session: AsyncSession):
    session = db_session
    _acc, wallet = await _user_with_wallet(session, 705)
    await _payout(session, 705, wallet.id, "reserved")
    submitting = await _payout(session, 705, wallet.id, "submitting", nonce=1)
    sent = await _payout(session, 705, wallet.id, "sent", nonce=2)
    await _payout(session, 705, wallet.id, "paid", nonce=3)

    in_flight = await repo.payouts_in_flight(session)
    ids = {p.id for p in in_flight}
    assert ids == {submitting.id, sent.id}


async def test_mark_pending_admin_transitions_reserved(db_session: AsyncSession):
    session = db_session
    _acc, wallet = await _user_with_wallet(session, 706)
    payout = await _payout(session, 706, wallet.id, "reserved")
    payout_id = payout.id

    await repo.mark_pending_admin(session, payout_id)
    await session.flush()
    session.expire_all()
    refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
    assert refreshed is not None
    assert refreshed.status == "pending_admin"


async def test_mark_submitting_and_mark_sent_transitions(db_session: AsyncSession):
    session = db_session
    _acc, wallet = await _user_with_wallet(session, 707)
    payout = await _payout(session, 707, wallet.id, "reserved")
    payout_id = payout.id

    await repo.mark_submitting(session, payout_id, 42)
    await session.flush()
    session.expire_all()
    refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
    assert refreshed is not None
    assert refreshed.status == "submitting"
    assert refreshed.nonce == 42

    now = datetime(2026, 7, 21, tzinfo=UTC)
    await repo.mark_sent(session, payout_id, "0xtxhash", now)
    await session.flush()
    session.expire_all()
    refreshed = await session.scalar(select(Payout).where(Payout.id == payout_id))
    assert refreshed is not None
    assert refreshed.status == "sent"
    assert refreshed.txhash == "0xtxhash"
    assert refreshed.sent_at == now
