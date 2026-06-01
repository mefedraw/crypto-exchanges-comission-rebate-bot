"""Stub adapter that returns deterministic fake data.

Used only in DEMO_MODE to develop and test the FSM/UI without real credentials.
The numbers are derived from the inputs so they are stable across runs but
obviously synthetic.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from bot.exchanges.base import CommissionResult, ExchangeAdapter


class StubAdapter(ExchangeAdapter):
    """Fake adapter for UX testing. Returns plausible-looking commission."""

    def __init__(self, code: str, name: str) -> None:
        self.code = code
        self.name = name

    async def get_commission(
        self, uid: str, date_from: datetime, date_to: datetime
    ) -> CommissionResult:
        days = max((date_to - date_from).days + 1, 1)
        # Deterministic synthetic amounts seeded from the uid digits.
        seed = sum(int(ch) for ch in uid if ch.isdigit()) or 1

        result = self._new_result(uid, date_from, date_to)
        result.add_amount("USDT", Decimal(seed * days) / Decimal(100))
        result.add_amount("BTC", Decimal(seed) / Decimal(1_000_000))
        result.raw_records_count = days
        result.settlement_note = "Демо-данные: ничего не запрашивалось у биржи."
        result.notes.append("DEMO_MODE — это фейковые значения, не реальная комиссия.")
        return result.finalize()
