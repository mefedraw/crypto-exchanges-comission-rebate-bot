"""Per-day rank/fee freezing (option C) and month/day date helpers.

Ranks are computed per USER (summed across all their UIDs). The rank frozen on a
day combines three floors — see PAYOUT_BUSINESS_LOGIC.md §4.3-4.4 and the
2026-07-13 owner decision (option C) in
docs/superpowers/specs/2026-07-12-accrual-worker-design.md:

  1. intra-month tier from cumulative gross BEFORE this day (upgrades between paid
     ranks take effect the NEXT day, §4.3);
  2. base rank 1 as soon as any rebate flows in the month (option C) — the first
     earning day is rank 1, never rank 0;
  3. carry-over floor from the two previous months' achieved ranks (§4.3), which
     applies from day 1 with no next-day delay.

A month whose gross closes < $10 is achieved-rank 0: it is NOT destroyed, it is just
non-withdrawable (the withdrawal rank-gate + $10 minimum block dust earners) and
expires on the normal 30-day schedule. This function still freezes rank ≥ 1 on the
earning days for fee purposes (base floor); withdrawal eligibility uses the month's
achieved rank separately.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

from bot.payout.ranks import rank_for_gross


def month_start(d: date) -> date:
    """First day of ``d``'s calendar month."""
    return d.replace(day=1)


def prev_month_start(month: date) -> date:
    """First day of the month before ``month`` (``month`` must be first-of-month)."""
    if month.month == 1:
        return date(month.year - 1, 12, 1)
    return date(month.year, month.month - 1, 1)


def next_month_start(month: date) -> date:
    """First day of the month after ``month`` (``month`` must be first-of-month)."""
    if month.month == 12:
        return date(month.year + 1, 1, 1)
    return date(month.year, month.month + 1, 1)


def iter_days(start: date, last: date) -> Iterator[date]:
    """Yield every date from ``start`` to ``last`` inclusive (empty if last < start)."""
    d = start
    while d <= last:
        yield d
        d += timedelta(days=1)


def day_effective_rank(
    cum_before: Decimal,
    day_amount: Decimal,
    carry_prev1: int,
    carry_prev2: int,
) -> int:
    """Frozen rank (0-4) for one day's accrual — the money-critical function.

    ``cum_before`` is the user's month-to-date gross through the PREVIOUS day.
    ``day_amount`` is this day's user-gross. See module docstring for the rules.
    Guarantee: ``day_amount > 0`` always yields rank ≥ 1 (base floor), so callers
    never hit the non-payable rank-0 fee lookup for a paying day.
    """
    intra = rank_for_gross(cum_before)
    base_floor = 1 if (cum_before + day_amount) > 0 else 0
    return max(intra, base_floor, carry_prev1, carry_prev2)
