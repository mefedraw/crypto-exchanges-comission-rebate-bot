# bot/executor/repository.py
"""Atomic payout state transitions + accrual finalization for the payout executor.

Every transition is a status-guarded UPDATE (WHERE id=... AND status='<from>') so a
transition only applies if the row is still in the expected state — cheap optimistic
concurrency without an extra SELECT ... FOR UPDATE round trip for the simple cases.
`claim_next_reserved` uses FOR UPDATE SKIP LOCKED to hand out at most one `reserved`
payout per concurrent claimer.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models.ledger import RebateAccrual
from bot.db.models.partners import PartnerEarning
from bot.db.models.payouts import Payout

_IN_FLIGHT = ("submitting", "sent")


async def payouts_in_flight(session: AsyncSession) -> list[Payout]:
    stmt = select(Payout).where(Payout.status.in_(_IN_FLIGHT)).order_by(Payout.requested_at)
    return list((await session.scalars(stmt)).all())


async def claim_next_reserved(session: AsyncSession) -> Payout | None:
    stmt = (
        select(Payout)
        .where(Payout.status == "reserved")
        .order_by(Payout.requested_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    return cast("Payout | None", await session.scalar(stmt))


async def last_nonce(session: AsyncSession, asset: str) -> int | None:
    """Highest nonce ever used for ``asset``'s chain.

    Scoped per asset so BSC (USDT) and Mantle (MNT) nonce sequences — separate chains,
    separate wallets' nonce counters even though the address is the same — never mix.
    """
    return cast(
        "int | None",
        await session.scalar(select(func.max(Payout.nonce)).where(Payout.asset == asset)),
    )


async def mark_pending_admin(session: AsyncSession, payout_id: int) -> None:
    await session.execute(
        update(Payout)
        .where(Payout.id == payout_id, Payout.status == "reserved")
        .values(status="pending_admin")
    )


async def mark_submitting(
    session: AsyncSession,
    payout_id: int,
    nonce: int,
    *,
    gas_bnb: Decimal | None = None,
    gas_usdt: Decimal | None = None,
    bnb_usdt_rate: Decimal | None = None,
) -> None:
    values: dict[str, Any] = {"status": "submitting", "nonce": nonce}
    if gas_usdt is not None:
        values["gas_usdt"] = gas_usdt
    if gas_bnb is not None:
        values["gas_bnb"] = gas_bnb
    if bnb_usdt_rate is not None:
        values["bnb_usdt_rate"] = bnb_usdt_rate
    await session.execute(
        update(Payout).where(Payout.id == payout_id, Payout.status == "reserved").values(**values)
    )


async def requeue_submitting(session: AsyncSession, payout_id: int) -> None:
    """Put a never-landed ``submitting`` payout back to ``reserved`` for a clean re-send.

    Called by reconcile ONLY after ``is_nonce_used`` has proven the tx never landed, so
    this can never double-spend. Clears ``nonce`` and ``txhash`` so ``process_next``
    recomputes a fresh, correct nonce (``last_nonce`` ignores the now-null row, avoiding
    a nonce gap). Guarded on the current status being ``submitting``."""
    await session.execute(
        update(Payout)
        .where(Payout.id == payout_id, Payout.status == "submitting")
        .values(status="reserved", nonce=None, txhash=None)
    )


async def set_txhash(session: AsyncSession, payout_id: int, txhash: str) -> None:
    """Persist the deterministic txhash while still ``submitting`` — called BEFORE
    broadcast so a crash in the broadcast window leaves the hash on the payout and
    reconcile can resolve it from the DB alone (#3)."""
    await session.execute(
        update(Payout)
        .where(Payout.id == payout_id, Payout.status == "submitting")
        .values(txhash=txhash)
    )


async def mark_sent(session: AsyncSession, payout_id: int, txhash: str, now: datetime) -> None:
    await session.execute(
        update(Payout)
        .where(Payout.id == payout_id, Payout.status == "submitting")
        .values(status="sent", txhash=txhash, sent_at=now)
    )


async def finalize_paid(session: AsyncSession, payout_id: int, now: datetime) -> None:
    changed = await session.scalar(
        update(Payout)
        .where(Payout.id == payout_id, Payout.status.in_(("sent", "confirmed")))
        .values(status="paid", confirmed_at=now, paid_at=now)
        .returning(Payout.id)
    )
    if changed is None:
        return
    await session.execute(
        update(RebateAccrual)
        .where(RebateAccrual.payout_id == payout_id, RebateAccrual.state == "reserved")
        .values(state="paid")
    )
    await session.execute(
        update(PartnerEarning)
        .where(PartnerEarning.payout_id == payout_id, PartnerEarning.state == "reserved")
        .values(state="paid")
    )


async def finalize_failed(session: AsyncSession, payout_id: int, reason: str) -> None:
    changed = await session.scalar(
        update(Payout)
        .where(Payout.id == payout_id, Payout.status.in_(("reserved", "submitting", "sent")))
        .values(status="failed", failed_reason=reason)
        .returning(Payout.id)
    )
    if changed is None:
        return
    await session.execute(
        update(RebateAccrual)
        .where(RebateAccrual.payout_id == payout_id, RebateAccrual.state == "reserved")
        .values(state="available", payout_id=None)
    )
    await session.execute(
        update(PartnerEarning)
        .where(PartnerEarning.payout_id == payout_id, PartnerEarning.state == "reserved")
        .values(state="available", payout_id=None)
    )
