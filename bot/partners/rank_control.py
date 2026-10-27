"""Persistent ranks: three calendar months of protection and one step per closed month."""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, time

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.db.models.identity import User
from bot.db.models.invites import InviteAttribution
from bot.db.models.ops import AuditEvent, Notification
from bot.db.models.partners import PartnerAccount, PartnerProgram, PartnerRankReview
from bot.partners.activity import ActivitySnapshot, load_activity
from bot.partners.repository import lock_account
from bot.payout.expiry import PAYOUT_TZ
from bot.payout.rank_timing import month_start, next_month_start

RANK_CHANGED = "partner_rank_changed"
log = structlog.get_logger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


def protection_end(promoted_at: datetime) -> datetime:
    if promoted_at.tzinfo is None:
        raise ValueError("Rank timestamps must be timezone-aware.")
    local = promoted_at.astimezone(PAYOUT_TZ)
    number = local.year * 12 + local.month - 1 + 3
    year, month0 = divmod(number, 12)
    month = month0 + 1
    day = min(local.day, calendar.monthrange(year, month)[1])
    return local.replace(year=year, month=month, day=day).astimezone(UTC)


def reviewed_rank(current: int, qualified: int, protected: bool) -> int:
    if not (0 <= current <= 7 and 0 <= qualified <= 7):
        raise ValueError("Unknown partner rank.")
    if qualified > current:
        return qualified
    if protected or qualified == current:
        return current
    return current - 1


def _changed(
    session: AsyncSession,
    account: PartnerAccount,
    previous: int,
    reason: str,
    now: datetime,
    month: date | None = None,
) -> None:
    detail: dict[str, int | str] = {
        "previous_rank": previous,
        "rank": account.current_rank,
        "reason": reason,
    }
    if month is not None:
        detail["month"] = month.isoformat()
    session.add(
        AuditEvent(
            actor="partner-ranks",
            action="partner_rank_changed",
            entity_type="partner_account",
            entity_id=str(account.user_id),
            old_value={"rank": previous},
            new_value=detail,
            created_at=now,
        )
    )
    session.add(
        Notification(
            recipient_chat_id=account.user_id, kind=RANK_CHANGED, payload=detail, created_at=now
        )
    )


def _promote(
    session: AsyncSession,
    account: PartnerAccount,
    rank: int,
    now: datetime,
    *,
    month: date | None = None,
) -> None:
    previous = account.current_rank
    account.current_rank = rank
    account.promoted_at = now
    account.protected_until = protection_end(now)
    if account.first_qualified_month is None:
        account.first_qualified_month = month_start(now.astimezone(PAYOUT_TZ).date())
    _changed(session, account, previous, "promoted", now, month)


async def _review_month(
    session: AsyncSession, account: PartnerAccount, month: date, now: datetime
) -> bool:
    review = await session.scalar(
        select(PartnerRankReview)
        .where(PartnerRankReview.user_id == account.user_id, PartnerRankReview.month == month)
        .execution_options(populate_existing=True)
    )
    if review is not None and review.status == "applied":
        account.last_review_month = month
        return True
    activity = await load_activity(session, account.user_id, month, now, check_coverage=True)
    details = {
        "counts": dict(zip((str(n) for n in (50, 200, 1000, 10000)), activity.counts, strict=True)),
        "missing_accounts": list(activity.missing_accounts),
    }
    if review is None:
        review = PartnerRankReview(user_id=account.user_id, month=month)
        session.add(review)
    previous = account.current_rank
    review.previous_rank = previous
    review.qualified_rank = activity.qualified_rank.level
    review.new_rank = previous
    review.total, review.active = activity.total, activity.active
    review.details = details
    review.checked_at = now
    if activity.missing_accounts:
        review.status, review.reason = "pending", "awaiting_data"
        review.applied_at = None
        await session.flush()
        return False
    boundary = datetime.combine(next_month_start(month), time.min, tzinfo=PAYOUT_TZ)
    protected = account.protected_until is not None and boundary < account.protected_until
    target = reviewed_rank(previous, activity.qualified_rank.level, protected)
    if target > previous:
        _promote(session, account, target, now, month=month)
        reason = "promoted"
    elif target < previous:
        account.current_rank = target
        _changed(session, account, previous, "downgraded", now, month)
        reason = "downgraded"
    else:
        reason = (
            "protected" if protected and activity.qualified_rank.level < previous else "qualified"
        )
    review.new_rank = account.current_rank
    review.status, review.reason = "applied", reason
    review.applied_at = now
    account.last_review_month = month
    await session.flush()
    return True


async def refresh_locked(
    session: AsyncSession, account: PartnerAccount, program: PartnerProgram, now: datetime
) -> ActivitySnapshot:
    current_month = month_start(now.astimezone(PAYOUT_TZ).date())
    if account.first_qualified_month is not None:
        period = (
            next_month_start(account.last_review_month)
            if account.last_review_month is not None
            else account.first_qualified_month
        )
        period = max(
            period,
            month_start(program.starts_on),
            month_start(program.monthly_enabled_at.astimezone(PAYOUT_TZ).date()),
        )
        while period < current_month:
            if not await _review_month(session, account, period, now):
                break
            period = next_month_start(period)
    activity = await load_activity(session, account.user_id, current_month, now)
    if activity.qualified_rank.level > account.current_rank:
        _promote(session, account, activity.qualified_rank.level, now)
    await session.flush()
    return activity


async def refresh_rank(
    session: AsyncSession, user_id: int, now: datetime | None = None
) -> PartnerAccount | None:
    program = await session.get(PartnerProgram, 1)
    if program is None or program.version < 2:
        return None
    account = await lock_account(session, user_id)
    await refresh_locked(session, account, program, now or utc_now())
    return account


async def refresh_all(sessionmaker: async_sessionmaker[AsyncSession], now: datetime) -> list[int]:
    async with sessionmaker() as session:
        program = await session.get(PartnerProgram, 1)
        if program is None or program.version < 2:
            return []
        invited = select(InviteAttribution.referrer_id)
        existing = select(PartnerAccount.user_id)
        user_ids = list((await session.scalars(invited.union(existing))).all())
    failed = []
    for user_id in user_ids:
        try:
            async with sessionmaker() as session:
                if await session.scalar(select(User.id).where(User.id == user_id)) is None:
                    continue
                await refresh_rank(session, user_id, now)
                await session.commit()
        except Exception:
            log.exception("partner_rank_refresh_failed", user_id=user_id)
            failed.append(user_id)
    return failed
