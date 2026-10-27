"""Rendering of the whole-downline rebate block in the admin stats (_format_downline)."""

from __future__ import annotations

from bot.admin.router import _format_downline


def test_format_downline_none_shows_pending() -> None:
    assert "ещё не посчитано" in _format_downline(None)
    assert "ещё не посчитано" in _format_downline({})


def test_format_downline_renders_total_and_per_exchange() -> None:
    snap = {
        "month": "2026-07",
        "as_of": "2026-07-20T03:00:00+00:00",
        "per_exchange": {"gate": "400", "okx": "300.5"},
        "errors": ["weex"],
        "unsupported": ["bitget"],
        "total_usdt": "700.5",
    }
    text = _format_downline(snap)
    assert "700.50 USDT" in text  # format_2dp on the total
    # concrete period, not a vague "за месяц": 1st of month .. as_of date (UTC+8)
    assert "с 01.07.2026 по 20.07.2026" in text
    assert "gate 400.00" in text
    assert "okx 300.50" in text
    # both failed + unsupported exchanges are surfaced as "no data"
    assert "Нет данных" in text
    assert "bitget" in text and "weex" in text


def test_format_downline_tolerates_bad_as_of() -> None:
    # A malformed as_of must not raise — the block still renders.
    text = _format_downline({"as_of": "not-a-date", "per_exchange": {}, "total_usdt": "0"})
    assert "0.00 USDT" in text


def test_format_downline_period_ends_at_through_date() -> None:
    # When `through` (last CLOSED day covered) is present, the period end is that
    # date — NOT the as_of update time (which is later, still the current day).
    snap = {
        "month": "2026-07",
        "as_of": "2026-07-21T02:00:00+00:00",  # updated on the 21st (UTC+8: 10:00)
        "through": "2026-07-20",  # but data covers only through the 20th
        "per_exchange": {"okx": "202.57"},
        "total_usdt": "202.57",
    }
    text = _format_downline(snap)
    assert "с 01.07.2026 по 20.07.2026" in text
    assert "202.57 USDT" in text


def test_format_downline_flags_guarded_exchange() -> None:
    # An exchange whose reading was rejected as an outlier is surfaced so the admin
    # knows the shown value is the previous one, not a fresh reading.
    snap = {
        "month": "2026-07",
        "as_of": "2026-07-21T02:00:00+00:00",
        "through": "2026-07-20",
        "per_exchange": {"okx": "202.57"},
        "guarded": ["okx"],
        "total_usdt": "202.57",
    }
    text = _format_downline(snap)
    assert "okx" in text
    # a caveat mentioning the prior/held value is shown
    assert "прошл" in text.lower() or "аномал" in text.lower()
