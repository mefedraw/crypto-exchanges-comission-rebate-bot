# bot/admin/repository.py
"""Async DB access for the admin panel: system flags + pending-release reads.

Money/state-changing operations reuse ``bot.botui.repository.write_audit``
rather than duplicating it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.accrual.repository import get_downline_snapshot
from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.ledger import RebateAccrual
from bot.db.models.ops import SystemFlag
from bot.db.models.partners import PartnerEarning
from bot.db.models.payouts import Payout
from bot.partners.service import source_is_funded
from bot.payout.ranks import fee_pct

PAYOUTS_PAUSED_KEY = "payouts_paused"


async def get_flag(session: AsyncSession, key: str) -> dict[str, Any] | None:
    """Return the JSON value stored under ``key``, or ``None`` if unset."""
    flag = await session.get(SystemFlag, key)
    return flag.value if flag is not None else None


async def set_flag(session: AsyncSession, key: str, value: dict[str, Any], by: int) -> None:
    """Upsert a system flag (insert or overwrite ``value``/``updated_by``/``updated_at``)."""
    stmt = pg_insert(SystemFlag).values(key=key, value=value, updated_by=by)
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={
            "value": stmt.excluded.value,
            "updated_by": stmt.excluded.updated_by,
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)
    await session.flush()


async def is_payouts_paused(session: AsyncSession) -> bool:
    """True iff the kill-switch flag is set and its ``paused`` field is truthy."""
    flag = await get_flag(session, PAYOUTS_PAUSED_KEY)
    return bool((flag or {}).get("paused"))


async def admin_overview(
    session: AsyncSession, *, month_start: date, month_before: date
) -> dict[str, Any]:
    """Snapshot counts/sums for the admin «📊 Статистика» view. Read-only, Decimal sums.

    Earnings are business-wide (the affiliate program is a single account; there is no
    per-admin attribution). They come from the FROZEN per-accrual fields over ALL states
    — the owner earned the exchange's commission when it accrued, regardless of whether
    the user later withdrew or let it expire:
      * earned  = Σ amount_usdt      (gross affiliate commission from the exchanges)
      * rebate  = Σ net_usdt         (what is owed/paid out to referred users)
      * profit  = Σ commission_usdt  (= earned − rebate, the platform fee kept)
    ``month_*`` bounds filter by ``accrual_date`` for the current-month block.
    """

    async def _scalar(stmt: Any) -> Any:
        return await session.scalar(stmt)

    def _sum(col: Any, *conds: Any) -> Any:
        stmt = select(func.coalesce(func.sum(col), 0))
        return stmt.where(*conds) if conds else stmt

    earned_total = await _scalar(_sum(RebateAccrual.amount_usdt))
    rebate_total = await _scalar(_sum(RebateAccrual.net_usdt))
    profit_total = await _scalar(_sum(RebateAccrual.commission_usdt))
    month_conds = (
        RebateAccrual.accrual_date >= month_start,
        RebateAccrual.accrual_date < month_before,
    )
    earned_month = await _scalar(_sum(RebateAccrual.amount_usdt, *month_conds))
    rebate_month = await _scalar(_sum(RebateAccrual.net_usdt, *month_conds))
    profit_month = await _scalar(_sum(RebateAccrual.commission_usdt, *month_conds))
    partner_total = Decimal(await _scalar(_sum(PartnerEarning.amount_usdt)) or 0)
    partner_month = Decimal(
        await _scalar(
            select(func.coalesce(func.sum(PartnerEarning.amount_usdt), 0))
            .join(RebateAccrual, RebateAccrual.id == PartnerEarning.source_accrual_id)
            .where(*month_conds)
        )
        or 0
    )

    users_total = await _scalar(select(func.count()).select_from(User))
    users_blocked = await _scalar(
        select(func.count()).select_from(User).where(User.status == "blocked")
    )
    active_accounts = await _scalar(
        select(func.count()).select_from(ExchangeAccount).where(ExchangeAccount.status == "active")
    )
    available_net = await _scalar(
        select(func.coalesce(func.sum(RebateAccrual.net_usdt), 0)).where(
            RebateAccrual.state == "available"
        )
    )
    reserved = await _scalar(
        select(func.count()).select_from(Payout).where(Payout.status == "reserved")
    )
    pending_admin = await _scalar(
        select(func.count()).select_from(Payout).where(Payout.status == "pending_admin")
    )
    in_flight = await _scalar(
        select(func.count()).select_from(Payout).where(Payout.status.in_(("submitting", "sent")))
    )
    paid_count = await _scalar(
        select(func.count()).select_from(Payout).where(Payout.status == "paid")
    )
    paid_sum = await _scalar(
        select(func.coalesce(func.sum(Payout.net_usdt), 0)).where(Payout.status == "paid")
    )
    # All-time wallet payouts split by asset: MNT is paid in native MNT, everything
    # else is USDT. USDT part = total - MNT part, so a legacy NULL-asset payout (paid
    # before the asset column existed) still counts as USDT rather than being dropped.
    paid_mnt_native = await _scalar(
        select(func.coalesce(func.sum(Payout.net_native), 0)).where(
            Payout.status == "paid", Payout.asset == "MNT"
        )
    )
    paid_mnt_usdt = await _scalar(
        select(func.coalesce(func.sum(Payout.net_usdt), 0)).where(
            Payout.status == "paid", Payout.asset == "MNT"
        )
    )
    return {
        "earned_total": Decimal(earned_total or 0),
        "rebate_total": Decimal(rebate_total or 0),
        "profit_total": Decimal(profit_total or 0),
        "earned_month": Decimal(earned_month or 0),
        "rebate_month": Decimal(rebate_month or 0),
        "profit_month": Decimal(profit_month or 0),
        "partner_total": partner_total,
        "partner_month": partner_month,
        "owner_profit_total": Decimal(profit_total or 0) - partner_total,
        "owner_profit_month": Decimal(profit_month or 0) - partner_month,
        "users_total": int(users_total or 0),
        "users_blocked": int(users_blocked or 0),
        "active_accounts": int(active_accounts or 0),
        "available_net": Decimal(available_net or 0),
        "reserved": int(reserved or 0),
        "pending_admin": int(pending_admin or 0),
        "in_flight": int(in_flight or 0),
        "paid_count": int(paid_count or 0),
        "paid_sum": Decimal(paid_sum or 0),
        "paid_mnt_native": Decimal(paid_mnt_native or 0),
        "paid_mnt_usdt": Decimal(paid_mnt_usdt or 0),
        # USDT part of all-time payouts = total - MNT part (keeps legacy NULL-asset as USDT).
        "paid_usdt": Decimal(paid_sum or 0) - Decimal(paid_mnt_usdt or 0),
        "paused": await is_payouts_paused(session),
        # Whole-downline rebate snapshot (all referrals incl. unregistered), refreshed
        # by the accrual worker. None until the first cycle has run.
        "downline": await get_downline_snapshot(session),
    }


async def list_pending_admin(session: AsyncSession) -> list[Payout]:
    """Payouts awaiting a manual admin release/reject decision, oldest first."""
    stmt = select(Payout).where(Payout.status == "pending_admin").order_by(Payout.requested_at)
    return list((await session.scalars(stmt)).all())


async def release_payout(session: AsyncSession, payout_id: int, admin_id: int) -> bool:
    """Approve a `pending_admin` payout: flip it back to `reserved` for the executor.

    Stamps `approved_by`/`approved_at` so the executor's auto-limit gate
    (`process_next`) can tell an admin-released payout apart from a freshly
    `reserved` one and skip re-bouncing it to `pending_admin` on the next tick
    (see FINDING 1 — over-limit released payouts must not ping-pong forever).
    Only a payout currently `pending_admin` is affected; returns whether a row changed.
    """
    res = cast(
        "CursorResult[None]",
        await session.execute(
            update(Payout)
            .where(Payout.id == payout_id, Payout.status == "pending_admin")
            .values(status="reserved", approved_by=admin_id, approved_at=func.now())
        ),
    )
    await session.flush()
    return (res.rowcount or 0) > 0


async def reject_payout(session: AsyncSession, payout_id: int) -> bool:
    """Reject a `pending_admin` payout: cancel it and return its reserved accruals.

    Accruals reserved for this payout go back to `available` (payout_id cleared) so
    they re-enter the live pool for a future withdrawal. Returns whether the payout
    was actually `pending_admin` (no-op otherwise).
    """
    res = cast(
        "CursorResult[None]",
        await session.execute(
            update(Payout)
            .where(Payout.id == payout_id, Payout.status == "pending_admin")
            .values(status="cancelled")
        ),
    )
    ok = (res.rowcount or 0) > 0
    if ok:
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
    await session.flush()
    return ok


async def reduce_payout_commission(
    session: AsyncSession, payout_id: int, new_commission: Decimal, by: int, reason: str
) -> bool:
    """One-time, only-down commission reduction on a `pending_admin` payout.

    `new_commission` must be within `[0, commission_usdt]` — it can only lower the
    fee taken (raising the user's net), never raise it. The original
    `commission_usdt` is left untouched; the reduced value is stored separately in
    `commission_override_usdt` alongside who did it and why. `net_usdt` is
    recomputed from `gross_usdt` so it only ever goes up. Returns whether applied.
    """
    payout = await session.scalar(select(Payout).where(Payout.id == payout_id).with_for_update())
    if payout is None or payout.status != "pending_admin":
        return False
    if new_commission > payout.commission_usdt or new_commission < 0:
        return False  # only-down, non-negative
    if new_commission < payout.commission_usdt and await session.scalar(
        select(
            select(PartnerEarning.id)
            .join(RebateAccrual, RebateAccrual.id == PartnerEarning.source_accrual_id)
            .where(RebateAccrual.payout_id == payout_id, PartnerEarning.amount_usdt > 0)
            .exists()
        )
    ):
        return False
    gas = payout.gas_usdt or Decimal(0)
    new_net = payout.gross_usdt - new_commission - gas
    await session.execute(
        update(Payout)
        .where(Payout.id == payout_id)
        .values(
            commission_override_usdt=new_commission,
            overridden_by=by,
            override_reason=reason,
            net_usdt=new_net,
        )
    )
    await session.flush()
    return True


async def set_commission_override(
    session: AsyncSession, user_id: int, pct: Decimal, since: date
) -> None:
    """Set a persistent, only-down personal fee override effective from ``since``.

    The accrual worker (Task 2, ``bot.accrual.worker``) reads this via
    ``bot.accrual.repository.get_commission_override`` and applies it through
    ``bot.payout.commission.effective_fee`` at freeze time — this function only
    persists the two columns; it never touches already-frozen accrual rows (the
    freeze rule holds regardless of when an override is set).
    """
    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(commission_override_pct=pct, commission_override_since=since)
    )
    await session.flush()


async def recompute_current_month_unpaid(
    session: AsyncSession,
    user_id: int,
    pct: Decimal | None,
    m_start: date,
    m_before: date,
) -> int:
    """Re-freeze fee/commission/net on this user's still-unpaid current-month accruals.

    Called right after ``set_commission_override``/``clear_commission_override`` so a
    freshly (un)set personal rate is reflected immediately instead of waiting for the
    next accrual-worker tick. Only rows within ``[m_start, m_before)`` in state
    ``available`` or ``accruing`` with a payable ``effective_rank`` (>= 1) are
    touched — ``reserved``/``paid``/``expired`` rows keep whatever was frozen at the
    time (the freeze rule, PAYOUT_BUSINESS_LOGIC.md §5.2), and rank-0 rows are
    non-payable so they're left at their (zero) fee. ``pct is None`` means "no
    override" — fall back to the rank's default fee. Returns the number of rows
    updated.
    """
    rows = (
        await session.scalars(
            select(RebateAccrual)
            .where(
                RebateAccrual.user_id == user_id,
                RebateAccrual.accrual_date >= m_start,
                RebateAccrual.accrual_date < m_before,
                RebateAccrual.state.in_(("available", "accruing")),
                RebateAccrual.correction_key.is_(None),
                RebateAccrual.effective_rank >= 1,
            )
            .with_for_update()
        )
    ).all()
    n = 0
    for r in rows:
        # The source row is locked first; this separate read also observes an
        # allocation committed while the SELECT FOR UPDATE was waiting.
        if await source_is_funded(session, r.id):
            continue
        fee = pct if pct is not None else fee_pct(r.effective_rank)
        r.fee_pct = fee
        r.commission_usdt = r.amount_usdt * fee
        r.net_usdt = r.amount_usdt - r.commission_usdt
        r.net_native = r.amount_native * (Decimal(1) - fee)
        n += 1
    await session.flush()
    return n


async def clear_commission_override(session: AsyncSession, user_id: int) -> None:
    """Remove a user's personal fee override; future accruals fall back to the rank fee."""
    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(commission_override_pct=None, commission_override_since=None)
    )
    await session.flush()


async def set_user_status(session: AsyncSession, user_id: int, status: str) -> None:
    """Set a user's account status (``active`` / ``blocked`` / ``manual_review``)."""
    await session.execute(update(User).where(User.id == user_id).values(status=status))
    await session.flush()


async def reassign_uid(session: AsyncSession, exchange: str, uid: str, new_user_id: int) -> bool:
    """Resolve a UID dispute by moving the existing ``(exchange, uid)`` account's ownership.

    ``uq_exchange_uid`` (``bot.db.models.identity.ExchangeAccount``) is a bare
    ``UniqueConstraint("exchange", "uid")`` with **no** status predicate: two rows can
    never share the same ``(exchange, uid)`` pair regardless of status. So this cannot
    archive the old row and insert a fresh active one for the new owner (that insert
    would collide with the still-existing archived row on the same constraint).
    Instead it UPDATEs the single existing row in place — reassigns ``user_id`` and
    resets ``status='active'`` (a previously blocked/archived account becomes active
    again under the new owner). Only future accruals are affected; anything already
    frozen/paid stays attributed to whoever it was frozen under. Returns ``False`` if
    no account exists for ``(exchange, uid)`` at all.
    """
    res = cast(
        "CursorResult[None]",
        await session.execute(
            update(ExchangeAccount)
            .where(ExchangeAccount.exchange == exchange, ExchangeAccount.uid == uid)
            .values(user_id=new_user_id, status="active")
        ),
    )
    await session.flush()
    return (res.rowcount or 0) > 0


async def block_uid(session: AsyncSession, exchange: str, uid: str) -> bool:
    """Block the currently-active ``(exchange, uid)`` account; future accruals for it stop.

    Returns ``False`` if there's no active account for that pair (already
    blocked/archived, or never existed).
    """
    res = cast(
        "CursorResult[None]",
        await session.execute(
            update(ExchangeAccount)
            .where(
                ExchangeAccount.exchange == exchange,
                ExchangeAccount.uid == uid,
                ExchangeAccount.status == "active",
            )
            .values(status="blocked")
        ),
    )
    await session.flush()
    return (res.rowcount or 0) > 0
