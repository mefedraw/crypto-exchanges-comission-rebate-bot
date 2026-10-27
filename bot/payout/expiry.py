"""Accrual availability, expiry and withdrawal-cooldown date math.

Day boundaries are in PAYOUT_TZ (UTC+8). A day-D accrual is available from
the start of D+1 and expires at the start of D+30 (+grace). All returned datetimes
are UTC-aware for storage. See PAYOUT_BUSINESS_LOGIC.md §3.1, §5.1.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone

from bot.payout.ranks import cooldown_days

PAYOUT_TZ = timezone(timedelta(hours=8))  # UTC+8
LIFETIME_DAYS = 30


def _day_start_utc(d: date) -> datetime:
    """Start of calendar day ``d`` in PAYOUT_TZ, as a UTC-aware datetime."""
    return datetime.combine(d, time.min, tzinfo=PAYOUT_TZ).astimezone(UTC)


def available_at(accrual_date: date) -> datetime:
    """When a day-D accrual becomes withdrawable: start of D+1 (UTC+8), in UTC."""
    return _day_start_utc(accrual_date + timedelta(days=1))


def expires_at(accrual_date: date, grace_days: int = 0) -> datetime:
    """When a day-D accrual expires: start of D+30+grace (UTC+8), in UTC."""
    return _day_start_utc(accrual_date + timedelta(days=LIFETIME_DAYS + grace_days))


def next_withdrawal_allowed(last_success_at: datetime | None, rank: int) -> datetime | None:
    """Earliest next withdrawal = last successful withdrawal + cooldown(rank).

    Returns None when there is no prior successful withdrawal (allowed immediately
    once there is enough available balance — enforced elsewhere). Rank must be 1-4.
    """
    days = cooldown_days(rank)  # raises ValueError for rank 0
    if last_success_at is None:
        return None
    return last_success_at + timedelta(days=days)
