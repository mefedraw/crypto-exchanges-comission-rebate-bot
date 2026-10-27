"""Monthly activity uses the normal rank's gross threshold, never a carried rank."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models.identity import ExchangeAccount
from bot.db.models.invites import InviteAttribution
from bot.db.models.ledger import RebateAccrual
from bot.partners.ranks import THRESHOLDS, PartnerRank, rank_for_counts
from bot.payout.expiry import PAYOUT_TZ
from bot.payout.rank_timing import month_start, next_month_start
from bot.payout.ranks import rank_for_gross


@dataclass(frozen=True)
class ActivitySnapshot:
    month: date
    total: int
    active: int
    counts: tuple[int, int, int, int]
    missing_accounts: tuple[int, ...] = ()

    @property
    def qualified_rank(self) -> PartnerRank:
        return rank_for_counts(self.counts)

    @property
    def below_30_percent(self) -> bool:
        return self.active * 100 < self.total * 30


def active_counts(rows: list[tuple[Decimal, Decimal]]) -> tuple[int, tuple[int, int, int, int]]:
    qualifying = [lifetime for lifetime, monthly in rows if rank_for_gross(monthly) >= 1]
    return len(qualifying), (
        sum(gross >= THRESHOLDS[0] for gross in qualifying),
        sum(gross >= THRESHOLDS[1] for gross in qualifying),
        sum(gross >= THRESHOLDS[2] for gross in qualifying),
        sum(gross >= THRESHOLDS[3] for gross in qualifying),
    )


async def load_activity(
    session: AsyncSession, user_id: int, month: date, now: datetime, *, check_coverage: bool = False
) -> ActivitySnapshot:
    if now.tzinfo is None:
        raise ValueError("Activity timestamps must be timezone-aware.")
    month = month_start(month)
    local = now.astimezone(PAYOUT_TZ)
    if month > month_start(local.date()):
        raise ValueError("A future activity month cannot be evaluated.")
    if check_coverage and month >= month_start(local.date()):
        raise ValueError("An open month cannot have final coverage.")
    before = next_month_start(month)
    observed_before = min(before, local.date() + timedelta(days=1))
    attribution_before = min(datetime.combine(before, time.min, tzinfo=PAYOUT_TZ), now)
    referees = select(InviteAttribution.invitee_id).where(
        InviteAttribution.referrer_id == user_id,
        InviteAttribution.created_at < attribution_before,
    )
    gross = (
        select(
            RebateAccrual.user_id,
            func.sum(RebateAccrual.amount_usdt).label("lifetime"),
            func.sum(
                case((RebateAccrual.accrual_date >= month, RebateAccrual.amount_usdt), else_=0)
            ).label("monthly"),
        )
        .where(
            RebateAccrual.finalized.is_(True),
            RebateAccrual.accrual_date < observed_before,
            RebateAccrual.user_id.in_(referees),
        )
        .group_by(RebateAccrual.user_id)
        .subquery()
    )
    records = (
        await session.execute(
            select(
                InviteAttribution.invitee_id,
                func.coalesce(gross.c.lifetime, 0),
                func.coalesce(gross.c.monthly, 0),
            )
            .outerjoin(gross, gross.c.user_id == InviteAttribution.invitee_id)
            .where(
                InviteAttribution.referrer_id == user_id,
                InviteAttribution.created_at < attribution_before,
            )
        )
    ).all()
    active, counts = active_counts(
        [(Decimal(lifetime), Decimal(monthly)) for _, lifetime, monthly in records]
    )
    missing = []
    if check_coverage and records:
        # Every normal account-day must be frozen, including zero days. A missing
        # day (e.g. an API outage) is unknown data, never evidence of inactivity.
        coverage = (
            await session.execute(
                select(
                    ExchangeAccount.id,
                    ExchangeAccount.accrual_since,
                    func.count(func.distinct(RebateAccrual.accrual_date)).filter(
                        RebateAccrual.finalized.is_(True)
                    ),
                    func.count().filter(RebateAccrual.finalized.is_(False)),
                )
                .outerjoin(
                    RebateAccrual,
                    and_(
                        RebateAccrual.account_id == ExchangeAccount.id,
                        RebateAccrual.accrual_date
                        >= func.greatest(month, ExchangeAccount.accrual_since),
                        RebateAccrual.accrual_date < before,
                        RebateAccrual.correction_key.is_(None),
                    ),
                )
                .where(
                    ExchangeAccount.user_id.in_(referees),
                    ExchangeAccount.status == "active",
                    ExchangeAccount.accrual_since < before,
                    ExchangeAccount.created_at
                    < datetime.combine(before, time.min, tzinfo=PAYOUT_TZ),
                )
                .group_by(ExchangeAccount.id, ExchangeAccount.accrual_since)
                .order_by(ExchangeAccount.id)
            )
        ).all()
        for account_id, since, days, unfinalized in coverage:
            if int(days) < (before - max(month, since)).days or unfinalized:
                missing.append(int(account_id))
    return ActivitySnapshot(month, len(records), active, counts, tuple(missing))
