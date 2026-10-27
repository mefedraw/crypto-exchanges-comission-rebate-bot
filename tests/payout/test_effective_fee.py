from __future__ import annotations

from decimal import Decimal

from bot.payout.commission import effective_fee

D = Decimal


def test_no_override_uses_rank_fee():
    assert effective_fee(1, None) == D("0.10")
    assert effective_fee(2, None) == D("0.07")


def test_override_only_lowers():
    assert effective_fee(1, D("0.05")) == D("0.05")  # 5% < 10% rank -> 5%
    assert effective_fee(1, D("0.20")) == D("0.10")  # 20% > 10% -> capped at rank (only down)
    assert effective_fee(4, D("0.05")) == D("0")  # rank4 fee 0 -> min stays 0
