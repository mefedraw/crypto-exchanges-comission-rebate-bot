"""The single abstraction the bot depends on: :class:`ExchangeAdapter`.

The bot knows nothing exchange-specific — it asks an adapter for commission over a
``(uid, date_from, date_to)`` window and renders the :class:`CommissionResult`.
Concrete adapters live alongside this module, one file per exchange.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


class ExchangeApiError(Exception):
    """Base class for adapter failures the handler layer can present to the user."""


class ExchangeAuthError(ExchangeApiError):
    """Invalid/expired key, bad signature, or insufficient permissions (401/403)."""


class ExchangeRateLimitError(ExchangeApiError):
    """The exchange throttled us (429) and retries were exhausted."""


class ExchangeUnavailableError(ExchangeApiError):
    """Network failure, timeout, or 5xx after retries."""


@dataclass(slots=True)
class CommissionLine:
    """One currency's worth of commission, optionally broken down by source."""

    asset: str  # e.g. "USDT"
    amount: Decimal
    source: str | None = None  # SPOT / FUTURES / ... when the exchange reports it


@dataclass(slots=True)
class CommissionResult:
    """Aggregated commission for one uid over one period, plus honesty caveats."""

    exchange: str
    uid: str
    date_from: datetime
    date_to: datetime
    lines: list[CommissionLine] = field(default_factory=list)
    total_usdt: Decimal | None = None  # set only when the exchange aggregates in USDT
    raw_records_count: int = 0
    notes: list[str] = field(default_factory=list)  # per-exchange warnings/caveats
    settlement_note: str | None = None  # accrued-vs-settled disclaimer

    # Internal accumulator: (asset, source) -> amount. Kept private so adapters use
    # add_amount() and never have to worry about merging duplicate currency rows.
    _totals: dict[tuple[str, str | None], Decimal] = field(
        default_factory=dict, repr=False
    )

    def add_amount(self, asset: str, amount: Decimal, source: str | None = None) -> None:
        """Accumulate ``amount`` for a currency (and optional source) with Decimal math."""
        key = (asset, source)
        self._totals[key] = self._totals.get(key, Decimal(0)) + amount

    def finalize(self) -> CommissionResult:
        """Materialize accumulated totals into sorted :class:`CommissionLine` rows.

        Adapters call this once after summing all pages/windows. Returns ``self`` for
        fluent use.
        """
        self.lines = [
            CommissionLine(asset=asset, amount=amount, source=source)
            for (asset, source), amount in sorted(
                self._totals.items(), key=lambda item: (item[0][0], item[0][1] or "")
            )
        ]
        return self

    @property
    def is_empty(self) -> bool:
        return not self.lines and not self.total_usdt


class ExchangeAdapter(ABC):
    """Uniform interface every exchange adapter implements.

    Implementations of :meth:`get_commission` must:
      1. slice the period into windows of at most :attr:`max_window_days`,
      2. page through each window's results,
      3. sum amounts per currency using :class:`Decimal` (never float),
      4. return a finalized :class:`CommissionResult` with any notes/disclaimers.
    """

    #: Human-facing name, e.g. "Gate".
    name: str
    #: Stable lookup code matching ExchangeSpec.code, e.g. "gate".
    code: str
    #: Largest date range the API accepts in one request.
    max_window_days: int = 30
    #: Whether the API can filter to a single referral uid.
    supports_uid_filter: bool = True
    #: Whether the API supports arbitrary date ranges (False => only rolling windows).
    supports_date_range: bool = True

    @abstractmethod
    async def get_commission(
        self, uid: str, date_from: datetime, date_to: datetime
    ) -> CommissionResult:
        """Fetch and aggregate commission for ``uid`` over ``[date_from, date_to]``."""
        raise NotImplementedError

    def _new_result(
        self, uid: str, date_from: datetime, date_to: datetime
    ) -> CommissionResult:
        """Factory for a result pre-stamped with this adapter's identity."""
        return CommissionResult(
            exchange=self.name,
            uid=uid,
            date_from=date_from,
            date_to=date_to,
        )
