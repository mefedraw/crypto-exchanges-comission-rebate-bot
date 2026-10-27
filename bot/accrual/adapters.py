"""Adapter lookup for the accrual worker, decoupled from the exchange registry.

The worker only needs to fetch an :class:`ExchangeAdapter` by exchange code; how
that adapter is built (from encrypted credentials via the registry) is a wiring
concern injected at the edge. Tests inject a plain mapping of fakes.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Protocol

from bot.exchanges.base import CommissionResult, ExchangeAdapter


class AdapterProvider(Protocol):
    """Resolves an exchange code (e.g. ``"bybit"``) to a ready adapter."""

    def get(self, exchange_code: str) -> ExchangeAdapter: ...


class MappingAdapterProvider:
    """An :class:`AdapterProvider` backed by an in-memory mapping."""

    def __init__(self, adapters: Mapping[str, ExchangeAdapter]) -> None:
        self._adapters = dict(adapters)

    def get(self, exchange_code: str) -> ExchangeAdapter:
        return self._adapters[exchange_code]


def usdt_of(result: CommissionResult) -> Decimal:
    """USDT total for one day's commission result (never sums non-USDT assets)."""
    if result.total_usdt is not None:
        return result.total_usdt
    return sum(
        (line.amount for line in result.lines if line.asset == "USDT"),
        Decimal(0),
    )
