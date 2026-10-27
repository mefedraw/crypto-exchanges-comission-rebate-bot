"""Atomic source-fee allocation. Does not trade, broadcast, or rewrite rebates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal, localcontext

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models.invites import InviteAttribution
from bot.db.models.ledger import RebateAccrual
from bot.db.models.ops import AuditEvent
from bot.db.models.partners import PartnerAccount, PartnerEarning, PartnerProgram, PartnerRankReview
from bot.partners import rank_control
from bot.partners.activity import ActivitySnapshot, load_activity
from bot.partners.ranks import THRESHOLDS, PartnerRank, rank_by_level, rank_for_counts
from bot.partners.repository import lock_account
from bot.payout.expiry import PAYOUT_TZ
from bot.payout.rank_timing import month_start

ZERO = Decimal(0)
UNIT = Decimal("0.000000000000000001")


def share_amount(fee: Decimal, share: Decimal) -> Decimal:
    if (
        not fee.is_finite()
        or fee < 0
        or not share.is_finite()
        or not ZERO <= share <= Decimal("0.5")
    ):
        raise ValueError("Invalid funded share.")
    with localcontext() as ctx:
        ctx.prec = 80
        return (fee * share).quantize(UNIT, rounding=ROUND_DOWN)


async def qualification_counts(session: AsyncSession, user_id: int) -> tuple[int, int, int, int]:
    # Group by the historical accrual owner, not today's mutable UID assignment.
    gross = (
        select(RebateAccrual.user_id, func.sum(RebateAccrual.amount_usdt).label("gross"))
        .join(InviteAttribution, InviteAttribution.invitee_id == RebateAccrual.user_id)
        .where(InviteAttribution.referrer_id == user_id, RebateAccrual.finalized.is_(True))
        .group_by(RebateAccrual.user_id)
        .subquery()
    )
    row = (
        await session.execute(
            select(*(func.count().filter(gross.c.gross >= n) for n in THRESHOLDS)).select_from(
                gross
            )
        )
    ).one()
    return int(row[0]), int(row[1]), int(row[2]), int(row[3])


async def capture_accrual(session: AsyncSession, accrual: RebateAccrual) -> PartnerEarning | None:
    program = await session.get(PartnerProgram, 1)
    if (
        program is None
        or not accrual.finalized
        or accrual.accrual_date < program.starts_on
        or accrual.correction_key is not None
    ):
        return None
    attribution = await session.get(InviteAttribution, accrual.user_id)
    if attribution is None or attribution.created_at > accrual.fetched_at:
        return None
    account = await lock_account(session, attribution.referrer_id)
    previous = await session.scalar(
        select(PartnerEarning).where(PartnerEarning.source_accrual_id == accrual.id)
    )
    if previous is not None:
        return previous
    if program.version >= 2:
        await rank_control.refresh_locked(session, account, program, rank_control.utc_now())
        rank = rank_by_level(account.current_rank)
    else:
        rank = rank_for_counts(await qualification_counts(session, attribution.referrer_id))
    fee = accrual.commission_usdt
    with localcontext() as ctx:
        ctx.prec = 80
        native_fee = fee if accrual.asset == "USDT" else accrual.amount_native - accrual.net_native
    if (
        not fee.is_finite()
        or not native_fee.is_finite()
        or fee < 0
        or native_fee < 0
        or accrual.asset not in {"USDT", "MNT"}
    ):
        raise ValueError("Source fee cannot fund a partner allocation.")
    earning = PartnerEarning(
        source_accrual_id=accrual.id,
        referrer_id=attribution.referrer_id,
        invitee_id=accrual.user_id,
        policy_version=program.version,
        rank=rank.level,
        share=rank.share,
        asset=accrual.asset,
        owner_fee_usdt=fee,
        owner_fee_native=native_fee,
        amount_usdt=share_amount(fee, rank.share),
        amount_native=share_amount(native_fee, rank.share),
        state="available",
    )
    session.add(earning)
    await session.flush()
    # Zero allocations also mark the source as processed: a future rank must never
    # turn an earlier rank-0 fee into a retroactive reward on a retry.
    session.add(
        AuditEvent(
            actor="accrual",
            action="partner_income_accrued",
            entity_type="partner_earning",
            entity_id=str(earning.id),
            amount_usdt=earning.amount_usdt,
            new_value={"source_id": accrual.id, "rank": rank.level, "asset": accrual.asset},
        )
    )
    return earning


async def source_is_funded(session: AsyncSession, accrual_id: int) -> bool:
    return bool(
        await session.scalar(
            select(
                select(PartnerEarning.id)
                .where(
                    PartnerEarning.source_accrual_id == accrual_id,
                    (PartnerEarning.amount_usdt > ZERO) | (PartnerEarning.amount_native > ZERO),
                )
                .exists()
            )
        )
    )


@dataclass(frozen=True)
class PartnerBalance:
    available: Decimal = ZERO
    available_native: Decimal = ZERO
    reserved: Decimal = ZERO
    paid: Decimal = ZERO


@dataclass(frozen=True)
class PartnerSummary:
    counts: tuple[int, int, int, int]
    rank: PartnerRank
    balances: dict[str, PartnerBalance]
    activity: ActivitySnapshot | None = None
    protected_until: datetime | None = None
    pending_month: str | None = None


async def summary(
    session: AsyncSession, user_id: int, now: datetime | None = None
) -> PartnerSummary:
    program = await session.get(PartnerProgram, 1)
    activity = None
    protected_until = None
    pending_month = None
    if program is not None and program.version >= 2:
        now = now or rank_control.utc_now()
        activity = await load_activity(
            session, user_id, month_start(now.astimezone(PAYOUT_TZ).date()), now
        )
        counts = activity.counts
        account = await session.get(PartnerAccount, user_id)
        rank = rank_by_level(account.current_rank if account is not None else 0)
        if (
            account is not None
            and account.protected_until is not None
            and account.protected_until > now
        ):
            protected_until = account.protected_until
        pending = await session.scalar(
            select(PartnerRankReview)
            .where(PartnerRankReview.user_id == user_id, PartnerRankReview.status == "pending")
            .order_by(PartnerRankReview.month)
            .limit(1)
        )
        if pending is not None:
            pending_month = pending.month.strftime("%m.%Y")
    else:
        counts = await qualification_counts(session, user_id)
        rank = rank_for_counts(counts)
    rows = (
        await session.execute(
            select(
                PartnerEarning.asset,
                PartnerEarning.state,
                func.sum(PartnerEarning.amount_usdt),
                func.sum(PartnerEarning.amount_native),
            )
            .where(PartnerEarning.referrer_id == user_id)
            .group_by(PartnerEarning.asset, PartnerEarning.state)
        )
    ).all()
    grouped = {
        (asset, state): (Decimal(amount), Decimal(native)) for asset, state, amount, native in rows
    }
    balances = {}
    for asset in ("USDT", "MNT"):
        available, native = grouped.get((asset, "available"), (ZERO, ZERO))
        balances[asset] = PartnerBalance(
            available=available,
            available_native=native,
            reserved=grouped.get((asset, "reserved"), (ZERO, ZERO))[0],
            paid=grouped.get((asset, "paid"), (ZERO, ZERO))[0],
        )
    return PartnerSummary(counts, rank, balances, activity, protected_until, pending_month)
