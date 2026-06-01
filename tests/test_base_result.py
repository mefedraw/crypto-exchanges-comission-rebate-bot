from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from bot.exchanges.base import CommissionResult


def _result() -> CommissionResult:
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    return CommissionResult(exchange="Gate", uid="123", date_from=now, date_to=now)


def test_add_amount_sums_same_asset_and_source():
    r = _result()
    r.add_amount("USDT", Decimal("1.5"))
    r.add_amount("USDT", Decimal("2.5"))
    r.add_amount("BTC", Decimal("0.001"))
    r.finalize()

    by_asset = {(line.asset, line.source): line.amount for line in r.lines}
    assert by_asset[("USDT", None)] == Decimal("4.0")
    assert by_asset[("BTC", None)] == Decimal("0.001")


def test_add_amount_keeps_sources_separate():
    r = _result()
    r.add_amount("USDT", Decimal("1"), source="SPOT")
    r.add_amount("USDT", Decimal("2"), source="FUTURES")
    r.finalize()
    assert len(r.lines) == 2


def test_finalize_sorts_lines():
    r = _result()
    r.add_amount("USDT", Decimal("1"))
    r.add_amount("BTC", Decimal("1"))
    r.finalize()
    assert [line.asset for line in r.lines] == ["BTC", "USDT"]


def test_is_empty():
    r = _result()
    assert r.finalize().is_empty
    r.add_amount("USDT", Decimal("1"))
    assert not r.finalize().is_empty
