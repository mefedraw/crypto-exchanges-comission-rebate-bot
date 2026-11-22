# tests/test_payout_commission.py
from datetime import date
from decimal import Decimal

import pytest

from bot.payout.commission import accrual_fee, commission_for, net_for, totals


@pytest.mark.parametrize(
    ("rank", "override", "code", "day", "expected"),
    [
        (1, None, "binance", 22, ".10"),
        (1, None, "binance", 23, ".11"),
        (1, ".02", "binance", 23, ".03"),
        (1, ".99", "binance", 23, ".11"),
        (2, None, "binance", 23, ".08"),
        (3, None, "binance", 23, ".04"),
        (4, None, "binance", 23, ".01"),
        (1, "0", "binance", 23, ".01"),
        (0, None, "binance", 23, "0"),
        (1, None, "blofin", 23, ".10"),
        (4, None, "bybit", 23, "0"),
    ],
)
def test_exchange_fee_by_earning_day(rank, override, code, day, expected):
    assert accrual_fee(
        rank, Decimal(override) if override is not None else None, code, date(2026, 11, day)
    ) == Decimal(expected)


def test_commission_and_net_per_rank():
    assert commission_for(Decimal("100"), 1) == Decimal("10.00")
    assert net_for(Decimal("100"), 1) == Decimal("90.00")
    assert commission_for(Decimal("200"), 2) == Decimal("14.00")
    assert net_for(Decimal("200"), 2) == Decimal("186.00")
    assert commission_for(Decimal("50"), 4) == Decimal("0")


def test_commission_rejects_rank_zero():
    with pytest.raises(ValueError):
        commission_for(Decimal("5"), 0)


def test_totals_owner_example():
    # $100 @ R1 (10%) + $200 @ R2 (7%) => commission $24, net $276.
    gross, commission, net = totals([(Decimal("100"), 1), (Decimal("200"), 2)])
    assert gross == Decimal("300")
    assert commission == Decimal("24.00")
    assert net == Decimal("276.00")


def test_totals_skips_rank_zero_tranches():
    gross, commission, net = totals([(Decimal("5"), 0), (Decimal("100"), 1)])
    assert gross == Decimal("100")  # rank-0 tranche excluded (non-payable)
    assert commission == Decimal("10.00")
    assert net == Decimal("90.00")
