from decimal import Decimal

import pytest

from bot.payout.ranks import (
    COOLDOWN_DAYS,
    FEE_PCT,
    cooldown_days,
    effective_rank,
    fee_pct,
    is_payable,
    rank_for_gross,
    rank_label,
)


@pytest.mark.parametrize(
    "rank, expected",
    [(0, "Манки 0"), (1, "Манки 1"), (2, "Манки 2"), (3, "Манки 3"), (4, "Манки 4")],
)
def test_rank_label(rank: int, expected: str) -> None:
    assert rank_label(rank) == expected


@pytest.mark.parametrize(
    "gross, rank",
    [
        ("0", 0),
        ("9.99", 0),
        ("10", 1),
        ("100", 1),
        ("100.01", 2),
        ("1000", 2),
        ("1000.01", 3),
        ("10000", 3),
        ("10000.01", 4),
        ("999999", 4),
    ],
)
def test_rank_for_gross_boundaries(gross, rank):
    assert rank_for_gross(Decimal(gross)) == rank


def test_fee_pct_values():
    assert FEE_PCT == {
        1: Decimal("0.10"),
        2: Decimal("0.07"),
        3: Decimal("0.03"),
        4: Decimal("0"),
    }
    assert fee_pct(1) == Decimal("0.10")


def test_cooldown_values():
    assert COOLDOWN_DAYS == {1: 30, 2: 14, 3: 7, 4: 1}
    assert cooldown_days(3) == 7


def test_is_payable():
    assert not is_payable(0)
    assert is_payable(1)
    assert is_payable(4)


def test_fee_and_cooldown_reject_rank_zero():
    with pytest.raises(ValueError):
        fee_pct(0)
    with pytest.raises(ValueError):
        cooldown_days(0)


def test_effective_rank_is_max_of_window():
    assert effective_rank(1, 0, 0) == 1
    assert effective_rank(1, 2, 0) == 2  # protection from prev month
    assert effective_rank(3, 1, 2) == 3  # immediate upgrade


def test_effective_rank_business_example_1():
    # achieved months: M1=2, M2=1, M3=3, M4=1, M5=1, M6=1
    assert effective_rank(2, 0, 0) == 2  # M1
    assert effective_rank(1, 2, 0) == 2  # M2 (protected by M1)
    assert effective_rank(3, 1, 2) == 3  # M3 (upgrade)
    assert effective_rank(1, 3, 1) == 3  # M4 (protected by M3)
    assert effective_rank(1, 1, 3) == 3  # M5 (last protected)
    assert effective_rank(1, 1, 1) == 1  # M6 (protection expired)


def test_effective_rank_business_example_2():
    # achieved months: M1=3, M2=1, M3=1, M4=1
    assert effective_rank(3, 0, 0) == 3  # M1
    assert effective_rank(1, 3, 0) == 3  # M2
    assert effective_rank(1, 1, 3) == 3  # M3
    assert effective_rank(1, 1, 1) == 1  # M4 (expired -> R1)
