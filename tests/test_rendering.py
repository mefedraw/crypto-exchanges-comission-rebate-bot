from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from bot.exchanges.base import CommissionResult
from bot.rendering import DISCLAIMER, render_confirm, render_result


def _result(empty: bool = False) -> CommissionResult:
    r = CommissionResult(
        exchange="Gate",
        uid="123",
        date_from=datetime(2026, 5, 1, tzinfo=timezone.utc),
        date_to=datetime(2026, 5, 31, tzinfo=timezone.utc),
    )
    if not empty:
        r.add_amount("USDT", Decimal("12.45"))
        r.raw_records_count = 3
    return r.finalize()


def test_render_result_always_has_disclaimer():
    out = render_result(_result())
    assert DISCLAIMER in out
    assert "12.45 USDT" in out
    assert "Записей обработано: 3" in out


def test_render_result_empty_period():
    out = render_result(_result(empty=True))
    assert "не найдено" in out
    assert DISCLAIMER in out


def test_render_result_escapes_html():
    r = _result()
    r.uid = "<script>"
    out = render_result(r)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_render_confirm():
    out = render_confirm("Gate", "123", date(2026, 5, 1), date(2026, 5, 31))
    assert "Gate" in out and "123" in out
    assert "2026-05-01" in out and "2026-05-31" in out
