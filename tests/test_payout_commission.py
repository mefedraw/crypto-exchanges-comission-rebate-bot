# tests/test_payout_commission.py
from decimal import Decimal

import pytest

from bot.payout.commission import commission_for, net_for, totals


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
