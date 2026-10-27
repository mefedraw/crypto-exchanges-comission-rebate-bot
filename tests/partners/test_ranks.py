from decimal import Decimal

import pytest

from bot.executor.chain import exact_18_to_wei
from bot.partners.ranks import rank_for_counts
from bot.partners.service import share_amount


@pytest.mark.parametrize(
    "counts,level",
    [
        ((0, 0, 0, 0), 0),
        ((2, 0, 0, 0), 0),
        ((3, 0, 0, 0), 1),
        ((6, 0, 0, 0), 1),
        ((7, 0, 0, 0), 2),
        ((9, 0, 0, 0), 2),
        ((10, 0, 0, 0), 3),
        ((19, 0, 0, 0), 3),
        ((20, 0, 0, 0), 4),
        ((49, 0, 0, 0), 4),
        ((50, 0, 0, 0), 5),
        ((79, 0, 0, 0), 5),
        ((80, 0, 0, 0), 6),
        ((99, 0, 0, 0), 6),
        ((100, 0, 0, 0), 7),
        ((3, 3, 0, 0), 3),
        ((7, 7, 0, 0), 4),
        ((15, 15, 0, 0), 5),
        ((20, 20, 0, 0), 6),
        ((25, 25, 0, 0), 7),
        ((5, 5, 5, 0), 5),
        ((8, 8, 8, 0), 6),
        ((10, 10, 10, 0), 7),
        ((1, 1, 1, 1), 7),
    ],
)
def test_alternative_conditions_and_exact_boundaries(counts, level):
    rank = rank_for_counts(counts)
    assert rank.level == level
    assert rank.share == (Decimal(0) if level == 0 else Decimal(".15") + level * Decimal(".05"))


def test_negative_counts_rejected():
    with pytest.raises(ValueError):
        rank_for_counts((-1, 0, 0, 0))


@pytest.mark.parametrize(
    "fee,share",
    [
        ("-1", ".2"),
        ("1", "-.1"),
        ("1", ".51"),
        ("NaN", ".2"),
        ("Infinity", ".2"),
        ("1", "NaN"),
    ],
)
def test_invalid_money_rejected(fee, share):
    with pytest.raises(ValueError):
        share_amount(Decimal(fee), Decimal(share))


def test_precision_and_rounding_never_exceed_owner_funding():
    fee = Decimal("99999999999999999999.123456789012345679")
    assert share_amount(fee, Decimal(".5")) == Decimal("49999999999999999999.561728394506172839")
    assert share_amount(Decimal(".000000000000000001"), Decimal(".5")) == 0


def test_partner_transfers_preserve_subcent_and_18_digit_precision():
    assert exact_18_to_wei(Decimal("20.002469")) == 20002469000000000000
    assert exact_18_to_wei(Decimal(".000000000000000001")) == 1
    assert exact_18_to_wei(Decimal("99999999999999999999.123456789012345678")) == (
        99999999999999999999123456789012345678
    )
