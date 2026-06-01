from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from bot.exchanges.base import CommissionResult
from bot.rendering import DISCLAIMER, render_result, usdt_total


def _result() -> CommissionResult:
    return CommissionResult(
        exchange="Gate",
        uid="123",
        date_from=datetime(2026, 5, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 5, 31, tzinfo=timezone.utc),
    )


def test_combines_usdt_sources_two_decimals():
    r = _result()
    r.add_amount("USDT", Decimal("27.0183237145"), source="FUTURES")
    r.add_amount("USDT", Decimal("0.0048078371"), source="SPOT")
    r.finalize()

    assert usdt_total(r) == Decimal("27.0231315516")
    out = render_result(r)
    assert "27.02 USDT" in out  # combined, 2 decimals
    assert DISCLAIMER in out
    assert "01.05.2026 — 31.05.2026" in out  # DD.MM.YYYY period


def test_excludes_non_usdt_coins():
    r = _result()
    r.add_amount("USDT", Decimal("10"))
    r.add_amount("MNT", Decimal("1000"))
    r.add_amount("USDC", Decimal("5"))
    r.finalize()

    assert usdt_total(r) == Decimal("10")
    out = render_result(r)
    assert "10.00 USDT" in out
    assert "MNT" not in out and "USDC" not in out


def test_empty_shows_zero_and_hint():
    r = _result().finalize()
    out = render_result(r)
    assert "0.00 USDT" in out
    assert "не найдено" in out
    assert DISCLAIMER in out


def test_escapes_uid():
    r = _result()
    r.uid = "<script>"
    out = render_result(r.finalize())
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_notes_are_rendered():
    r = _result()
    r.add_amount("USDT", Decimal("1"))
    r.notes.append("тестовое предупреждение")
    out = render_result(r.finalize())
    assert "тестовое предупреждение" in out
