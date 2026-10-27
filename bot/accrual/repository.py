# bot/accrual/repository.py
"""Async DB access for the accrual worker. Thin wrappers over the ledger models;
all money math is done by the caller (worker) with Decimal. See DB_SCHEMA.md §2.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models.identity import ExchangeAccount, User
from bot.db.models.ledger import RankHistory, RebateAccrual
from bot.db.models.ops import ServiceAvailability, SystemFlag
from bot.db.models.partners import PartnerEarning
from bot.partners.service import capture_accrual

# Heartbeat: the worker stamps "I ran at T" here each cycle so a later cycle can
# detect a gap (the service was down) and record a downtime window for expiry grace.
_HEARTBEAT_KEY = "accrual_heartbeat"

# Whole-downline monthly rebate snapshot (all referrals per exchange, incl. users
# not registered in the bot). Refreshed by the worker; read by the admin stats. A
# system_flags row keyed here holds the JSON payload built by save_downline_snapshot.
DOWNLINE_TOTALS_KEY = "downline_totals"


async def active_accounts_by_user(session: AsyncSession) -> dict[int, list[ExchangeAccount]]:
    """All active exchange accounts, grouped by owner user_id."""
    stmt = (
        select(ExchangeAccount)
        .where(ExchangeAccount.status == "active")
        .order_by(ExchangeAccount.user_id, ExchangeAccount.id)
    )
    grouped: dict[int, list[ExchangeAccount]] = {}
    for acc in (await session.scalars(stmt)).all():
        grouped.setdefault(acc.user_id, []).append(acc)
    return grouped


async def last_finalized_day(session: AsyncSession, account_id: int) -> date | None:
    """Most recent finalized accrual day for an account, or None if none yet."""
    stmt = select(func.max(RebateAccrual.accrual_date)).where(
        RebateAccrual.account_id == account_id,
        RebateAccrual.finalized.is_(True),
        RebateAccrual.correction_key.is_(None),
    )
    return cast("date | None", await session.scalar(stmt))


async def last_gate_query_end(session: AsyncSession, account_id: int) -> datetime | None:
    """Resume beyond exact coverage, or the legacy UTC day, without a cutover overlap."""
    stmt = (
        select(RebateAccrual.accrual_date, RebateAccrual.query_to)
        .where(
            RebateAccrual.account_id == account_id,
            RebateAccrual.finalized.is_(True),
            RebateAccrual.correction_key.is_(None),
        )
        .order_by(RebateAccrual.accrual_date.desc(), RebateAccrual.query_to.desc().nulls_last())
        .limit(1)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    if row.query_to is not None:
        return cast("datetime", row.query_to)
    # VERIFIED: pre-fix Gate used UTC calendar dates, even for UTC+8 worker inputs.
    return datetime.combine(row.accrual_date + timedelta(days=1), time.min, UTC) - timedelta(
        seconds=1
    )


async def finalized_amounts(
    session: AsyncSession, account_id: int, start: date, before: date
) -> dict[date, Decimal]:
    """Frozen USDT-equivalent gross for ``account_id`` over ``[start, before)``, keyed
    by day and SUMMED ACROSS ASSETS.

    A day may now be frozen as two rows (USDT + MNT, ``uq_accrual_day`` is keyed on
    ``(account_id, accrual_date, asset)``); the summing (GROUP BY day) is essential —
    without it, a naive one-row-per-day dict would silently drop one asset's amount
    whenever both exist for the same day. Lets the worker reuse an already-finalized
    day's total instead of re-polling the exchange and overwriting a frozen row (the
    freeze guarantee)."""
    stmt = (
        select(RebateAccrual.accrual_date, func.sum(RebateAccrual.amount_usdt))
        .where(
            RebateAccrual.account_id == account_id,
            RebateAccrual.accrual_date >= start,
            RebateAccrual.accrual_date < before,
            RebateAccrual.finalized.is_(True),
            RebateAccrual.correction_key.is_(None),
        )
        .group_by(RebateAccrual.accrual_date)
    )
    rows = cast("list[tuple[date, Decimal]]", (await session.execute(stmt)).all())
    return dict(rows)


async def user_month_gross(
    session: AsyncSession, user_id: int, start: date, before: date
) -> Decimal:
    """Sum of a user's accrued gross for ``start <= accrual_date < before``.

    Counts rows in ANY state (paid/expired included) so month rank reflects real
    volume even when the money later expired (business §3.2)."""
    stmt = select(func.coalesce(func.sum(RebateAccrual.amount_usdt), 0)).where(
        RebateAccrual.user_id == user_id,
        RebateAccrual.accrual_date >= start,
        RebateAccrual.accrual_date < before,
    )
    return Decimal(await session.scalar(stmt) or 0)


async def get_commission_override(
    session: AsyncSession, user_id: int
) -> tuple[Decimal | None, date | None]:
    """The user's per-user commission override, or ``(None, None)`` if unset.

    Applied by the worker at accrual-freeze time (only-down, gated by
    ``since``); see bot.payout.commission.effective_fee."""
    stmt = select(User.commission_override_pct, User.commission_override_since).where(
        User.id == user_id
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None, None
    return row[0], row[1]


async def get_achieved_rank(session: AsyncSession, user_id: int, period_month: date) -> int | None:
    stmt = select(RankHistory.achieved_rank).where(
        RankHistory.user_id == user_id,
        RankHistory.period_month == period_month,
    )
    return cast("int | None", await session.scalar(stmt))


async def upsert_accrual(
    session: AsyncSession,
    *,
    user_id: int,
    account_id: int,
    accrual_date: date,
    amount_usdt: Decimal,
    effective_rank: int,
    fee_pct: Decimal,
    commission_usdt: Decimal,
    net_usdt: Decimal,
    state: str,
    available_at: datetime,
    expires_at: datetime,
    raw_records_count: int,
    adapter_notes: str | None,
    asset: str = "USDT",
    amount_native: Decimal | None = None,
    net_native: Decimal | None = None,
    fx_rate: Decimal = Decimal(1),
    query_from: datetime | None = None,
    query_to: datetime | None = None,
) -> None:
    """Insert or overwrite the (account_id, accrual_date, asset) accrual row. Finalized.

    ``asset``/``amount_native``/``net_native``/``fx_rate`` support the MNT accrual
    path (bot/accrual/worker.py): a Bybit account can freeze both a USDT row and an
    MNT row for the same day (distinguished by ``asset``; see ``uq_accrual_day``).
    Existing USDT-only callers are unaffected: ``asset`` defaults to ``"USDT"`` and
    ``amount_native``/``net_native`` default to the USDT amounts themselves
    (native == usdt, fx_rate == 1) when omitted.
    """
    if (query_from is None) != (query_to is None):
        raise ValueError("Both query bounds must be supplied together.")
    if query_from is not None and query_to is not None and query_from > query_to:
        raise ValueError("Query end precedes query start.")
    if amount_native is None:
        amount_native = amount_usdt
    if net_native is None:
        net_native = net_usdt
    values = {
        "user_id": user_id,
        "account_id": account_id,
        "accrual_date": accrual_date,
        "asset": asset,
        "amount_usdt": amount_usdt,
        "effective_rank": effective_rank,
        "fee_pct": fee_pct,
        "commission_usdt": commission_usdt,
        "net_usdt": net_usdt,
        "amount_native": amount_native,
        "net_native": net_native,
        "fx_rate": fx_rate,
        "state": state,
        "available_at": available_at,
        "expires_at": expires_at,
        "raw_records_count": raw_records_count,
        "adapter_notes": adapter_notes,
        "query_from": query_from,
        "query_to": query_to,
        "finalized": True,
        "fetched_at": func.now(),
    }
    stmt = pg_insert(RebateAccrual).values(**values)
    # Never overwrite a row already tied to a payout (paid/reserved) — those are settled.
    stmt = stmt.on_conflict_do_update(
        constraint="uq_accrual_day",
        set_={
            k: stmt.excluded[k]
            for k in (
                "amount_usdt",
                "effective_rank",
                "fee_pct",
                "commission_usdt",
                "net_usdt",
                "amount_native",
                "net_native",
                "fx_rate",
                "state",
                "available_at",
                "expires_at",
                "raw_records_count",
                "adapter_notes",
                "query_from",
                "query_to",
                "finalized",
                "fetched_at",
            )
        },
        where=(
            RebateAccrual.payout_id.is_(None)
            & ~select(PartnerEarning.id)
            .where(
                PartnerEarning.source_accrual_id == RebateAccrual.id,
                (PartnerEarning.amount_usdt > 0) | (PartnerEarning.amount_native > 0),
            )
            .correlate(RebateAccrual)
            .exists()
        ),
    )
    accrual = await session.scalar(
        stmt.returning(RebateAccrual).execution_options(populate_existing=True)
    )
    if accrual is not None:
        await capture_accrual(session, accrual)


async def write_or_bump_rank_history(
    session: AsyncSession,
    *,
    user_id: int,
    period_month: date,
    achieved_rank: int,
    gross_month_usdt: Decimal,
) -> bool:
    """Persist a closed month's achieved rank. Returns True iff freshly inserted
    (i.e. this is the month's FIRST close). On a re-close, gross is refreshed and
    rank only ever moves up."""
    existing = await get_achieved_rank(session, user_id, period_month)
    if existing is None:
        session.add(
            RankHistory(
                user_id=user_id,
                period_month=period_month,
                achieved_rank=achieved_rank,
                gross_month_usdt=gross_month_usdt,
            )
        )
        await session.flush()
        return True
    stmt = (
        update(RankHistory)
        .where(
            RankHistory.user_id == user_id,
            RankHistory.period_month == period_month,
        )
        .values(
            gross_month_usdt=gross_month_usdt,
            achieved_rank=func.greatest(RankHistory.achieved_rank, achieved_rank),
        )
    )
    await session.execute(stmt)
    return False


async def get_heartbeat(session: AsyncSession) -> datetime | None:
    """Timestamp of the worker's last recorded cycle, or None if never stamped."""
    flag = await session.get(SystemFlag, _HEARTBEAT_KEY)
    if flag is None:
        return None
    raw = flag.value.get("at")
    return datetime.fromisoformat(raw) if isinstance(raw, str) else None


async def set_heartbeat(session: AsyncSession, now: datetime) -> None:
    """Stamp the worker's liveness. ``updated_by`` is left NULL (not a user action)."""
    stmt = pg_insert(SystemFlag).values(key=_HEARTBEAT_KEY, value={"at": now.isoformat()})
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={"value": stmt.excluded.value, "updated_at": func.now()},
    )
    await session.execute(stmt)


async def save_downline_snapshot(session: AsyncSession, payload: dict[str, Any]) -> None:
    """Upsert the whole-downline rebate snapshot (see DOWNLINE_TOTALS_KEY). ``payload``
    is stored verbatim as JSON; Decimals must already be stringified by the caller.
    ``updated_by`` is left NULL (a worker action, not a user's)."""
    stmt = pg_insert(SystemFlag).values(key=DOWNLINE_TOTALS_KEY, value=payload)
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"],
        set_={"value": stmt.excluded.value, "updated_at": func.now()},
    )
    await session.execute(stmt)


async def get_downline_snapshot(session: AsyncSession) -> dict[str, Any] | None:
    """The last saved whole-downline rebate snapshot payload, or None if never run."""
    flag = await session.get(SystemFlag, DOWNLINE_TOTALS_KEY)
    return dict(flag.value) if flag is not None else None


async def record_downtime(
    session: AsyncSession, *, down_from: datetime, down_to: datetime, reason: str
) -> None:
    """Record a closed service-downtime window; consumed by ``expire_aged`` for grace."""
    session.add(ServiceAvailability(down_from=down_from, down_to=down_to, reason=reason))


async def closed_downtime_windows(session: AsyncSession) -> list[tuple[datetime, datetime]]:
    """All completed downtime windows (``down_to`` set), oldest first."""
    stmt = (
        select(ServiceAvailability.down_from, ServiceAvailability.down_to)
        .where(ServiceAvailability.down_to.is_not(None))
        .order_by(ServiceAvailability.down_from)
    )
    return cast("list[tuple[datetime, datetime]]", (await session.execute(stmt)).all())


def _overlap(a1: datetime, a2: datetime, b1: datetime, b2: datetime) -> timedelta:
    """Length of the overlap between intervals [a1,a2] and [b1,b2] (0 if disjoint)."""
    lo, hi = max(a1, b1), min(a2, b2)
    return hi - lo if hi > lo else timedelta(0)


async def expire_aged(session: AsyncSession, now: datetime) -> int:
    """Transition available rows past their expiry to expired — but grant GRACE for
    any service downtime that overlapped a row's live window [available_at, expires_at].

    A row is only expired once ``now >= expires_at + downtime_overlap``, so a day
    cannot burn purely because the bot/exchange was down while the user could not
    withdraw (business §6.2). Grace only ever DEFERS expiry (never advances it) and
    is measured over the row's nominal life, so a row that lived its full 30 days
    with no downtime expires exactly on time. Idempotent."""
    windows = await closed_downtime_windows(session)
    candidates = cast(
        "list[tuple[int, datetime, datetime]]",
        (
            await session.execute(
                select(
                    RebateAccrual.id, RebateAccrual.available_at, RebateAccrual.expires_at
                ).where(RebateAccrual.state == "available", RebateAccrual.expires_at <= now)
            )
        ).all(),
    )
    if not candidates:
        return 0
    if not windows:
        ids = [rid for rid, _, _ in candidates]
    else:
        ids = []
        for rid, avail_at, expires_at in candidates:
            grace = sum(
                (_overlap(avail_at, expires_at, wf, wt) for wf, wt in windows),
                timedelta(0),
            )
            if expires_at + grace <= now:
                ids.append(rid)
    if not ids:
        return 0
    await session.execute(
        update(RebateAccrual).where(RebateAccrual.id.in_(ids)).values(state="expired")
    )
    return len(ids)
