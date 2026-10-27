# tests/payout/test_rank_timing.py
from __future__ import annotations

from datetime import date
from decimal import Decimal

from bot.payout.rank_timing import (
    day_effective_rank,
    iter_days,
    month_start,
    next_month_start,
    prev_month_start,
)

D = Decimal


def test_month_helpers_handle_year_boundary():
    assert month_start(date(2026, 7, 15)) == date(2026, 7, 1)
    assert prev_month_start(date(2026, 1, 1)) == date(2025, 12, 1)
    assert next_month_start(date(2026, 12, 1)) == date(2027, 1, 1)


def test_iter_days_is_inclusive():
    days = list(iter_days(date(2026, 7, 10), date(2026, 7, 12)))
    assert days == [date(2026, 7, 10), date(2026, 7, 11), date(2026, 7, 12)]
    assert list(iter_days(date(2026, 7, 12), date(2026, 7, 10))) == []


def test_first_rebate_day_is_rank_1_not_0():
    # cum_before 0, first earning day -> base floor rank 1 (option C), never 0.
    assert day_effective_rank(D("0"), D("5"), 0, 0) == 1


def test_zero_day_with_no_history_is_rank_0():
    assert day_effective_rank(D("0"), D("0"), 0, 0) == 0


def test_upgrade_takes_effect_next_day():
    # Cumulative crosses 100 (rank-2 territory) on this day, but the day itself
    # still uses cum_before (through prev day) -> stays rank 1. Upgrade is next day.
    assert day_effective_rank(D("90"), D("70"), 0, 0) == 1  # cum_before=90 -> R1
    assert day_effective_rank(D("160"), D("10"), 0, 0) == 2  # next day: cum_before=160 -> R2


def test_rank_1_boundary_is_inclusive_100():
    # $100 exactly is still rank 1; 100.01 before-day would be rank 2.
    assert day_effective_rank(D("100"), D("0"), 0, 0) == 1
    assert day_effective_rank(D("100.01"), D("0"), 0, 0) == 2


def test_carry_over_floor_applies_from_day_one():
    # Previous month achieved rank 3 -> this month's first day is already rank 3.
    assert day_effective_rank(D("0"), D("5"), 3, 0) == 3
    assert day_effective_rank(D("0"), D("5"), 0, 2) == 2


def test_rank_is_max_of_all_floors():
    # intra=2 (cum_before 500), carry=3 -> 3 wins.
    assert day_effective_rank(D("500"), D("10"), 3, 1) == 3
    # intra=4 (cum_before 20000) beats carry=2.
    assert day_effective_rank(D("20000"), D("1"), 2, 0) == 4
