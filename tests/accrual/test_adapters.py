from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from bot.accrual.adapters import MappingAdapterProvider, usdt_of
from bot.exchanges.base import CommissionLine, CommissionResult


def _result(**kw) -> CommissionResult:
    return CommissionResult(
        exchange="bybit",
        uid="u",
        date_from=datetime(2026, 7, 11, tzinfo=UTC),
        date_to=datetime(2026, 7, 11, tzinfo=UTC),
        **kw,
    )


def test_usdt_of_prefers_total_usdt():
    assert usdt_of(_result(total_usdt=Decimal("12.5"))) == Decimal("12.5")


def test_usdt_of_sums_usdt_lines_only():
    r = _result(
        lines=[
            CommissionLine(asset="USDT", amount=Decimal("3")),
            CommissionLine(asset="USDT", amount=Decimal("4")),
            CommissionLine(asset="BTC", amount=Decimal("1")),
        ]
    )
    assert usdt_of(r) == Decimal("7")


def test_usdt_of_empty_is_zero():
    assert usdt_of(_result()) == Decimal("0")


def test_mapping_provider_returns_registered_adapter():
    sentinel = object()
    provider = MappingAdapterProvider({"bybit": sentinel})  # type: ignore[dict-item]
    assert provider.get("bybit") is sentinel
